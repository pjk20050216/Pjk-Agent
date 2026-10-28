"""Agent Harness 运行时 —— 任务规划、异步子 Agent 委派、状态追踪与结果汇总。

生命周期::

    create_run() -> start() -> [planning] -> [running: 依赖分层 + 并发子 Agent]
                 -> [waiting_approval: 高风险动作挂起] -> [completed|failed|cancelled]

关键设计：
- ``start()`` 立即返回，真正的执行在 ``asyncio.Task`` 中进行（异步 Agent）；
- 每个任务按依赖分层，同层任务用信号量限制并发；
- 每次状态变更都落库 + 发事件，支持 SSE 断线重放与跨会话恢复；
- 子 Agent 只回传摘要，大结果留 VFS，主上下文不随日志膨胀。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from harness.agents.base import AgentContext, build_agent_context
from harness.agents.orchestrator import Orchestrator
from harness.agents.subagents import get_agent
from harness.approvals import ApprovalManager, approvals as default_approvals
from harness.config import HarnessConfig, get_harness_config
from harness.contracts import (
    ArtifactRef,
    HookPoint,
    MemoryRecord,
    RiskLevel,
    RunEventType,
    RunSnapshot,
    RunStatus,
    SubAgentResult,
    TaskPlan,
    TaskStatus,
    TaskItem,
    utcnow,
)
from harness.events import EventBus, bus as default_bus
from harness.integrations.java_bridge import JavaToolBridge, java_bridge as default_java
from harness.llm import truncate
from harness.memory.manager import MemoryManager, memory_manager as default_memory
from harness.middleware.base import MiddlewareChain, build_context
from harness.middleware.builtin import build_default_chain
from harness.observability.tracing import TraceRecorder, recorder as default_recorder
from harness.sandbox.manager import SandboxManager, sandbox_manager as default_sandbox
from harness.skills.registry import SkillRegistry, skills as default_skills
from harness.store import HarnessStore, store as default_store
from harness.tools import SUBAGENT_TOOLSETS, build_registry
from harness.tools.context import ToolContext
from harness.vfs.store import VirtualFileSystem
from server.utils.logger import logger

TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


class AgentHarness:
    def __init__(
        self,
        config: HarnessConfig | None = None,
        *,
        store: HarnessStore | None = None,
        memory: MemoryManager | None = None,
        skills: SkillRegistry | None = None,
        sandbox: SandboxManager | None = None,
        approvals: ApprovalManager | None = None,
        recorder: TraceRecorder | None = None,
        bus: EventBus | None = None,
        java_bridge: JavaToolBridge | None = None,
        orchestrator: Orchestrator | None = None,
    ) -> None:
        self.config = config or get_harness_config()
        self.store = store or default_store
        self.memory = memory or default_memory
        self.skills = skills or default_skills
        self.sandbox = sandbox or default_sandbox
        self.approvals = approvals or default_approvals
        self.recorder = recorder or default_recorder
        self.bus = bus or default_bus
        self.java_bridge = java_bridge or default_java
        self.orchestrator = orchestrator or Orchestrator(self.config)
        self._jobs: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._persist_lock = asyncio.Lock()

    # ------------------------------------------------------------------ create
    async def create_run(
        self,
        objective: str,
        *,
        user_id: int | None = None,
        mode: str = "auto",
        metadata: dict | None = None,
    ) -> RunSnapshot:
        snapshot = RunSnapshot(
            objective=objective,
            user_id=user_id,
            mode=mode,
            metadata=metadata or {},
        )
        await self._persist(snapshot)
        await self._emit(snapshot)(
            RunEventType.RUN_CREATED,
            {"run_id": snapshot.id, "objective": objective, "mode": mode},
        )
        return snapshot

    async def start(self, run_id: str) -> RunSnapshot:
        snapshot = await self.get(run_id)
        if snapshot is None:
            raise KeyError(f"run not found: {run_id}")
        if RunStatus(snapshot.status) in TERMINAL_RUN_STATUSES:
            return snapshot
        async with self._lock:
            existing = self._jobs.get(run_id)
            if existing is not None and not existing.done():
                return snapshot
            job = asyncio.create_task(self._run_job(snapshot), name=f"harness:{run_id}")
            self._jobs[run_id] = job
            job.add_done_callback(lambda _: self._jobs.pop(run_id, None))
        return snapshot

    async def run_stream(
        self,
        objective: str,
        *,
        user_id: int | None = None,
        mode: str = "auto",
        metadata: dict | None = None,
    ) -> AsyncIterator:
        snapshot = await self.create_run(
            objective, user_id=user_id, mode=mode, metadata=metadata
        )
        await self.start(snapshot.id)
        return self.bus.stream(snapshot.id)

    async def resume(self, run_id: str) -> RunSnapshot | None:
        snapshot = await self.get(run_id)
        if snapshot is None:
            return None
        if RunStatus(snapshot.status) in TERMINAL_RUN_STATUSES:
            return snapshot
        if RunStatus(snapshot.status) == RunStatus.WAITING_APPROVAL:
            pending = self.approvals.pending(run_id)
            if pending:
                return snapshot
        # 失败/阻塞/中断的任务允许重试；已完成任务跳过。
        # in_progress 出现在恢复场景中，说明上次进程在任务执行中被中断，
        # 必须重置为 pending，否则依赖分层会把它当作“永远不完成”而上游阻塞。
        if snapshot.plan:
            for task in snapshot.plan.tasks:
                current = TaskStatus(task.status)
                if current in {
                    TaskStatus.FAILED,
                    TaskStatus.BLOCKED,
                    TaskStatus.IN_PROGRESS,
                    TaskStatus.WAITING_APPROVAL,
                }:
                    task.status = TaskStatus.PENDING
                    task.error = None
                    if current in {TaskStatus.FAILED, TaskStatus.BLOCKED}:
                        task.attempts = 0
        snapshot.status = RunStatus.RUNNING
        await self._persist(snapshot)
        await self._emit(snapshot)(RunEventType.RUN_RESUMED, {"run_id": run_id})
        return await self.start(run_id)

    def is_running(self, run_id: str) -> bool:
        job = self._jobs.get(run_id)
        return job is not None and not job.done()

    async def mark_failed(self, run_id: str, error: str) -> RunSnapshot | None:
        """外部决策（如审批拒绝）导致运行终止时使用。"""
        snapshot = await self.get(run_id)
        if snapshot is None:
            return None
        snapshot.status = RunStatus.FAILED
        snapshot.error = error
        snapshot.finished_at = utcnow()
        await self._persist(snapshot)
        await self._emit(snapshot)(RunEventType.RUN_FAILED, {"error": error})
        return snapshot

    async def cancel(self, run_id: str) -> RunSnapshot | None:
        snapshot = await self.get(run_id)
        if snapshot is None:
            return None
        job = self._jobs.get(run_id)
        if job is not None and not job.done():
            job.cancel()
        snapshot.status = RunStatus.CANCELLED
        snapshot.finished_at = utcnow()
        await self._persist(snapshot)
        await self._emit(snapshot)(RunEventType.RUN_CANCELLED, {"run_id": run_id})
        return snapshot

    # -------------------------------------------------------------------- read
    async def get(self, run_id: str) -> RunSnapshot | None:
        return await asyncio.to_thread(self.store.get_run, run_id)

    async def list_runs(
        self, user_id: int | None = None, limit: int = 50
    ) -> list[RunSnapshot]:
        return await asyncio.to_thread(self.store.list_runs, user_id, limit)

    async def wait(self, run_id: str, timeout: float | None = None) -> RunSnapshot | None:
        job = self._jobs.get(run_id)
        if job is not None:
            try:
                await asyncio.wait_for(asyncio.shield(job), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            return await self.get(run_id)
        # 跨进程：轮询快照状态
        deadline = time.monotonic() + (timeout or self.config.run_timeout_seconds)
        while time.monotonic() < deadline:
            snapshot = await self.get(run_id)
            if snapshot and RunStatus(snapshot.status) in TERMINAL_RUN_STATUSES:
                return snapshot
            await asyncio.sleep(1.0)
        return await self.get(run_id)

    def events(self, run_id: str, after_seq: int = 0):
        return self.bus.stream(run_id, after_seq)

    def status(self) -> dict:
        return {
            "enabled": self.config.enabled,
            "running_jobs": len(self._jobs),
            "max_parallel_subagents": self.config.max_parallel_subagents,
            "memory": self.memory.status(),
            "sandbox": self.sandbox.status(),
            "approvals": self.approvals.status(),
            "kafka": self._kafka_status(),
            "java_bridge": self.java_bridge.status(),
            "skills": len(self.skills),
        }

    def _kafka_status(self) -> dict:
        try:
            from harness.integrations.kafka import sink

            return sink.status
        except Exception:
            return {"enabled": False}

    # ------------------------------------------------------------------- jobs
    async def _run_job(self, snapshot: RunSnapshot) -> None:
        try:
            await asyncio.wait_for(
                self._execute(snapshot), timeout=self.config.run_timeout_seconds
            )
        except asyncio.CancelledError:
            snapshot.status = RunStatus.CANCELLED
            snapshot.finished_at = utcnow()
            snapshot.error = "cancelled by user"
            await self._persist(snapshot)
            await self._emit(snapshot)(RunEventType.RUN_CANCELLED, {"run_id": snapshot.id})
            raise
        except asyncio.TimeoutError:
            snapshot.status = RunStatus.FAILED
            snapshot.error = f"run timeout after {self.config.run_timeout_seconds}s"
            snapshot.finished_at = utcnow()
            await self._persist(snapshot)
            await self._emit(snapshot)(
                RunEventType.RUN_FAILED, {"error": snapshot.error}
            )
        except Exception as exc:
            logger.exception(f"[Harness] run {snapshot.id} failed")
            snapshot.status = RunStatus.FAILED
            snapshot.error = f"{type(exc).__name__}: {exc}"
            snapshot.finished_at = utcnow()
            await self._persist(snapshot)
            await self._emit(snapshot)(
                RunEventType.RUN_FAILED, {"error": snapshot.error}
            )
        finally:
            try:
                await self.sandbox.stop(snapshot.id)
            except Exception:
                pass
            await self._persist(snapshot)

    async def _execute(self, snapshot: RunSnapshot) -> None:
        vfs = VirtualFileSystem(snapshot.id, self.config)
        emit = self._emit(snapshot)
        set_status = self._status_setter(snapshot, emit)
        services: dict[str, Any] = {
            "approvals": self.approvals,
            "set_run_status": set_status,
            "vfs": vfs,
            "memory": self.memory,
            "sandbox": self.sandbox,
            "skills": self.skills,
            "user_role": snapshot.metadata.get("user_role", "operator"),
        }
        chain = build_default_chain(services)
        run_ctx = build_context(
            snapshot.id,
            HookPoint.RUN_START,
            payload={"objective": snapshot.objective},
            user_id=snapshot.user_id,
            agent="main_agent",
            services=services,
        )
        await chain.run(run_ctx, lambda: self._execute_core(snapshot, vfs, chain, services, emit))

    async def _execute_core(
        self,
        snapshot: RunSnapshot,
        vfs: VirtualFileSystem,
        chain: MiddlewareChain,
        services: dict[str, Any],
        emit,
    ) -> None:
        snapshot.status = RunStatus.RUNNING
        snapshot.started_at = snapshot.started_at or utcnow()
        await self._persist(snapshot)
        await emit(
            RunEventType.RUN_STARTED,
            {"run_id": snapshot.id, "objective": snapshot.objective},
        )

        await vfs.seed_manifest(
            {
                "run_id": snapshot.id,
                "objective": snapshot.objective,
                "user_id": snapshot.user_id,
                "mode": snapshot.mode,
                "created_at": snapshot.created_at.isoformat(),
            }
        )

        # 1) 召回长期经验（短期记忆已由 store 持久化）
        async with self.recorder.span(
            snapshot.id, "memory.recall", "memory", {"query": snapshot.objective}
        ):
            memory_context = await self.memory.recall(
                snapshot.objective, user_id=snapshot.user_id, top_k=5
            )
        await emit(
            RunEventType.MEMORY_RECALLED,
            {
                "count": len(memory_context),
                "items": [
                    {"kind": m.kind, "text": truncate(m.text, 160), "score": m.score}
                    for m in memory_context
                ],
            },
        )

        # 2) 任务规划
        async with self.recorder.span(
            snapshot.id, "agent.plan", "agent", {"objective": snapshot.objective}
        ):
            plan = await self.orchestrator.plan(snapshot.objective, memory_context)
        # 允许 API 通过 metadata 直接指定采集 URL/查询，避免用户必须把长 URL 写进目标文本
        for task in plan.tasks:
            if task.agent != "data_collection":
                continue
            for key in ("urls", "queries", "max_sources"):
                if key in snapshot.metadata and key not in task.inputs:
                    task.inputs[key] = snapshot.metadata[key]

        snapshot.plan = plan
        await self._persist(snapshot)
        await emit(
            RunEventType.PLAN_CREATED,
            {
                "strategy": plan.strategy,
                "planner": plan.metadata.get("planner", "unknown"),
                "tasks": [
                    {
                        "id": t.id,
                        "seq": t.seq,
                        "title": t.title,
                        "agent": t.agent,
                        "depends_on": t.depends_on,
                        "risk": t.risk,
                    }
                    for t in plan.tasks
                ],
            },
        )
        await vfs.seed_manifest(
            {
                "run_id": snapshot.id,
                "objective": snapshot.objective,
                "plan": plan.model_dump(mode="json"),
            }
        )

        # 3) 依赖分层 + 并发执行
        # 子 Agent 的文件操作走带中间件的代理，真正触发文件读写 Hook
        from harness.vfs.hooked import HookedFileSystem

        hooked_vfs = HookedFileSystem(
            vfs,
            chain,
            services,
            run_id=snapshot.id,
            user_id=snapshot.user_id,
            agent="subagent",
        )
        semaphore = asyncio.Semaphore(max(1, self.config.max_parallel_subagents))
        tool_ctx = ToolContext(
            run_id=snapshot.id,
            user_id=snapshot.user_id,
            vfs=hooked_vfs,  # type: ignore[arg-type]
            memory=self.memory,
            sandbox=self.sandbox,
            skills=self.skills,
            approvals=self.approvals,
            java_bridge=self.java_bridge,
            plan_provider=lambda: snapshot.plan,
        )
        results: list[SubAgentResult] = self._prior_results(plan)

        while True:
            pending = [
                task for task in plan.tasks if TaskStatus(task.status) == TaskStatus.PENDING
            ]
            if not pending:
                break
            ready = [task for task in pending if self._deps_satisfied(task, plan)]
            if not ready:
                for task in pending:
                    await self._transition(
                        snapshot,
                        task,
                        TaskStatus.BLOCKED,
                        chain,
                        services,
                        emit,
                        error="依赖任务未完成（上游失败或被阻塞）",
                    )
                break
            level_results = await asyncio.gather(
                *(
                    self._run_task(
                        snapshot, task, chain, services, emit, tool_ctx, semaphore, memory_context
                    )
                    for task in ready
                )
            )
            results.extend([r for r in level_results if r is not None])

        # 4) 汇总
        async with self.recorder.span(
            snapshot.id, "agent.synthesize", "agent", {"tasks": len(plan.tasks)}
        ):
            content, report_path = await self.orchestrator.synthesize(
                run_id=snapshot.id,
                objective=snapshot.objective,
                plan=plan,
                results=results,
                vfs=vfs,
            )

        artifact_paths = list(
            dict.fromkeys(
                [report_path, *(a.path for r in results for a in r.artifacts)]
            )
        )
        findings = [f for r in results for f in r.findings][:20]
        completed = sum(
            1 for t in plan.tasks if TaskStatus(t.status) == TaskStatus.COMPLETED
        )
        failed = sum(
            1 for t in plan.tasks if TaskStatus(t.status) == TaskStatus.FAILED
        )
        blocked = sum(
            1 for t in plan.tasks if TaskStatus(t.status) == TaskStatus.BLOCKED
        )
        snapshot.result = {
            "report_path": report_path,
            "summary": truncate(content, 4000),
            "artifact_paths": artifact_paths,
            "findings": findings,
            "task_results": [
                {
                    "task_id": r.task_id,
                    "agent": r.agent,
                    "success": r.success,
                    "summary": r.summary,
                    "artifacts": [a.path for a in r.artifacts],
                }
                for r in results
            ],
        }
        snapshot.counters = {
            "tasks_total": len(plan.tasks),
            "tasks_completed": completed,
            "tasks_failed": failed,
            "tasks_blocked": blocked,
            "tool_calls": sum(len(r.tool_calls) for r in results),
            "tokens_in": sum(r.tokens_in for r in results),
            "tokens_out": sum(r.tokens_out for r in results),
            "memories_recalled": len(memory_context),
        }
        snapshot.finished_at = utcnow()
        snapshot.status = RunStatus.COMPLETED if failed == 0 and blocked == 0 else RunStatus.FAILED
        if snapshot.status == RunStatus.FAILED:
            snapshot.error = f"{failed} task(s) failed, {blocked} blocked"
        await self._persist(snapshot)

        # 5) 复盘沉淀
        memories: list[MemoryRecord] = []
        if self.config.memory.distill_enabled:
            async with self.recorder.span(snapshot.id, "memory.distill", "memory"):
                memories = await self.memory.distill(snapshot, results)
            await emit(
                RunEventType.MEMORY_DISTILLED,
                {
                    "count": len(memories),
                    "items": [
                        {"kind": m.kind, "text": truncate(m.text, 160)} for m in memories
                    ],
                },
            )
        snapshot.counters["memories_distilled"] = len(memories)

        event_type = (
            RunEventType.RUN_COMPLETED
            if snapshot.status == RunStatus.COMPLETED
            else RunEventType.RUN_FAILED
        )
        await emit(
            event_type,
            {
                "run_id": snapshot.id,
                "status": snapshot.status,
                "report_path": report_path,
                "counters": snapshot.counters,
                "error": snapshot.error,
            },
        )

    async def _run_task(
        self,
        snapshot: RunSnapshot,
        task: TaskItem,
        chain: MiddlewareChain,
        services: dict[str, Any],
        emit,
        tool_ctx: ToolContext,
        semaphore: asyncio.Semaphore,
        memory_context: list[MemoryRecord],
    ) -> SubAgentResult | None:
        async with semaphore:
            agent = get_agent(task.agent)
            if agent is None:
                await self._transition(
                    snapshot, task, TaskStatus.FAILED, chain, services, emit,
                    error=f"unknown agent: {task.agent}",
                )
                return SubAgentResult(
                    task_id=task.id, agent=task.agent, success=False,
                    summary=f"未知子 Agent：{task.agent}", error="unknown agent",
                )

            tools = await build_registry(
                tool_ctx, SUBAGENT_TOOLSETS.get(task.agent)
            )
            last_result: SubAgentResult | None = None

            while task.attempts < max(1, task.max_attempts):
                task.attempts += 1
                await self._transition(
                    snapshot, task, TaskStatus.IN_PROGRESS, chain, services, emit,
                    error=None,
                )
                agent_ctx = build_agent_context(
                    run_id=snapshot.id,
                    objective=snapshot.objective,
                    task=task,
                    user_id=snapshot.user_id,
                    vfs=tool_ctx.vfs,
                    memory=self.memory,
                    sandbox=self.sandbox,
                    skills=self.skills,
                    tools=tools,
                    middleware=chain,
                    services=services,
                    memory_context=memory_context,
                    emit=emit,
                    config=self.config,
                )
                try:
                    async with self.recorder.span(
                        snapshot.id,
                        f"agent.{agent.name}",
                        "agent",
                        {"task_id": task.id, "attempt": task.attempts},
                    ):
                        result = await agent.execute(agent_ctx)
                except Exception as exc:
                    logger.exception(f"[Harness] task {task.id} crashed")
                    result = SubAgentResult(
                        task_id=task.id,
                        agent=task.agent,
                        success=False,
                        summary=f"任务异常：{type(exc).__name__}: {exc}",
                        error=str(exc),
                    )
                last_result = result

                if result.success:
                    await self._transition(
                        snapshot, task, TaskStatus.COMPLETED, chain, services, emit,
                        result_summary=result.summary,
                        artifacts=[a.path for a in result.artifacts],
                        error=None,
                        duration_ms=result.duration_ms,
                    )
                    return result

                if task.attempts < max(1, task.max_attempts):
                    logger.warning(
                        f"[Harness] task {task.id} failed (attempt {task.attempts}), retrying"
                    )
                    await self._transition(
                        snapshot, task, TaskStatus.PENDING, chain, services, emit,
                        error=result.error,
                    )
                    continue

                await self._transition(
                    snapshot, task, TaskStatus.FAILED, chain, services, emit,
                    error=result.error or result.summary,
                    result_summary=result.summary,
                    duration_ms=result.duration_ms,
                )
                return result

            return last_result

    # ------------------------------------------------------------- transitions
    async def _transition(
        self,
        snapshot: RunSnapshot,
        task: TaskItem,
        status: TaskStatus,
        chain: MiddlewareChain,
        services: dict[str, Any],
        emit,
        **fields: Any,
    ) -> None:
        from_status = task.status

        async def operation() -> None:
            task.status = status
            for key, value in fields.items():
                setattr(task, key, value)
            if status == TaskStatus.IN_PROGRESS:
                task.started_at = utcnow()
                task.finished_at = None
            if status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.SKIPPED}:
                task.finished_at = utcnow()
                if task.started_at:
                    task.duration_ms = int(
                        (task.finished_at - task.started_at).total_seconds() * 1000
                    )
            await self._persist(snapshot)
            payload = {
                "task_id": task.id,
                "seq": task.seq,
                "title": task.title,
                "agent": task.agent,
                "from_status": _status_value(from_status),
                "status": _status_value(status),
                "error": task.error,
            }
            await emit(RunEventType.TASK_UPDATED, payload)
            if status == TaskStatus.IN_PROGRESS:
                await emit(
                    RunEventType.TASK_STARTED,
                    {"task_id": task.id, "title": task.title, "agent": task.agent},
                )
            elif status == TaskStatus.COMPLETED:
                await emit(
                    RunEventType.TASK_COMPLETED,
                    {
                        "task_id": task.id,
                        "title": task.title,
                        "summary": task.result_summary,
                        "artifacts": task.artifacts,
                    },
                )
            elif status == TaskStatus.FAILED:
                await emit(
                    RunEventType.TASK_FAILED,
                    {"task_id": task.id, "title": task.title, "error": task.error},
                )

        mw = build_context(
            snapshot.id,
            HookPoint.BEFORE_TASK_STATE,
            payload={
                "task_id": task.id,
                "from_status": _status_value(from_status),
                "to_status": _status_value(status),
                "title": task.title,
            },
            user_id=snapshot.user_id,
            agent=task.agent,
            task_id=task.id,
            services=services,
        )
        await chain.run(mw, operation)

    # ------------------------------------------------------------------ helpers
    def _deps_satisfied(self, task: TaskItem, plan: TaskPlan) -> bool:
        for dep_id in task.depends_on:
            dep = plan.by_id(dep_id)
            if dep is None:
                continue
            if TaskStatus(dep.status) in {TaskStatus.COMPLETED, TaskStatus.SKIPPED}:
                continue
            return False
        return True

    def _prior_results(self, plan: TaskPlan) -> list[SubAgentResult]:
        results: list[SubAgentResult] = []
        for task in plan.tasks:
            if TaskStatus(task.status) != TaskStatus.COMPLETED:
                continue
            results.append(
                SubAgentResult(
                    task_id=task.id,
                    agent=task.agent,
                    success=True,
                    summary=task.result_summary or "（已完成，恢复运行）",
                    artifacts=[
                        ArtifactRef(path=path, kind="artifact")
                        for path in task.artifacts
                    ],
                    duration_ms=task.duration_ms or 0,
                )
            )
        return results

    async def _persist(self, snapshot: RunSnapshot) -> None:
        # SQLite 并发写容易触发 database is locked，进程内串行化持久化
        async with self._persist_lock:
            snapshot.updated_at = utcnow()
            try:
                await asyncio.to_thread(self.store.save_run, snapshot)
            except Exception as exc:
                logger.error(f"[Harness] persist failed: {exc}")
            try:
                await self.memory.save_run(snapshot)
            except Exception:
                pass

    def _emit(self, snapshot: RunSnapshot):
        async def emit(event_type: RunEventType | str, payload: dict) -> None:
            await self.bus.publish(
                snapshot.id,
                event_type,
                payload,
                trace_id=self.recorder.trace_id(snapshot.id),
            )

        return emit

    def _status_setter(self, snapshot: RunSnapshot, emit):
        async def set_status(status: str, **extra: Any) -> None:
            try:
                snapshot.status = RunStatus(status)
            except ValueError:
                return
            await self._persist(snapshot)
            if status == RunStatus.WAITING_APPROVAL.value:
                approval = extra.get("approval")
                await emit(
                    RunEventType.RUN_WAITING_APPROVAL,
                    {
                        "approval_id": getattr(approval, "id", ""),
                        "action": getattr(approval, "action", ""),
                        "risk": str(getattr(approval, "risk", "")),
                    },
                )
            elif status == RunStatus.RUNNING.value:
                await emit(RunEventType.RUN_RESUMED, {"run_id": snapshot.id})

        return set_status

    async def shutdown(self) -> None:
        jobs = [job for job in self._jobs.values() if not job.done()]
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        self._jobs.clear()
        await self.sandbox.close()
        try:
            await self.java_bridge.close()
        except Exception:
            pass
        try:
            from harness.integrations.kafka import sink

            await sink.close()
        except Exception:
            pass
        try:
            from harness.integrations.web import web_fetch_client

            await web_fetch_client.aclose()
        except Exception:
            pass


def _status_value(status: TaskStatus | str) -> str:
    return status.value if isinstance(status, TaskStatus) else str(status)


harness = AgentHarness()


def get_harness() -> AgentHarness:
    return harness
