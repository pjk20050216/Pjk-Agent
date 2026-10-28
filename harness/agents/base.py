"""子 Agent 基类 —— 独立上下文执行任务，只回传结构化结论。

上下文隔离策略：
- 子 Agent 只收到「任务描述 + 相关长期记忆 + 技能摘要」，不继承主 Agent 的完整对话；
- 工具产生的大结果写入 VFS，模型消息里只保留截断后的观察；
- 结束时强制生成 ``SubAgentResult``（summary ≤ 配置上限 + 文件引用），
  主 Agent 的上下文只增长“结论 + 链接”，不增长原始日志。

LLM 不可用时自动切换到 ``heuristic()`` 确定性实现，保证离线可演示、可测试。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from harness.config import HarnessConfig, get_harness_config
from harness.contracts import (
    ArtifactRef,
    HookPoint,
    MemoryRecord,
    RunEventType,
    SubAgentResult,
    TaskItem,
    ToolCallRecord,
    utcnow,
)
from harness.llm import estimate_tokens, llm_available, truncate
from harness.memory.manager import MemoryManager
from harness.middleware.base import HookDenied, MiddlewareChain, build_context
from harness.observability.tracing import recorder
from harness.sandbox.manager import SandboxManager
from harness.skills.registry import SkillRegistry
from harness.tools.base import HarnessTool, ToolRegistry
from harness.vfs.store import VirtualFileSystem
from server.services.llm_service import llm_service
from server.utils.logger import logger

EmitFn = Callable[[RunEventType, dict], Awaitable[None]]


@dataclass
class AgentContext:
    run_id: str
    objective: str
    task: TaskItem
    user_id: int | None
    vfs: VirtualFileSystem
    memory: MemoryManager
    sandbox: SandboxManager
    skills: SkillRegistry
    tools: ToolRegistry
    middleware: MiddlewareChain
    services: dict[str, Any]
    memory_context: list[MemoryRecord] = field(default_factory=list)
    emit: EmitFn | None = None
    config: HarnessConfig = field(default_factory=get_harness_config)

    async def publish(self, event: RunEventType, payload: dict) -> None:
        if self.emit is not None:
            await self.emit(event, payload)


class BaseAgent:
    name: str = "base"
    description: str = ""
    toolset: set[str] = set()
    preferred_skills: list[str] = []

    # ------------------------------------------------------------------ entry
    async def execute(self, ctx: AgentContext) -> SubAgentResult:
        started = utcnow()
        started_perf = time.perf_counter()
        artifacts: list[ArtifactRef] = []
        tool_records: list[ToolCallRecord] = []
        tokens_in = tokens_out = 0

        await ctx.publish(
            RunEventType.AGENT_DELEGATED,
            {"agent": self.name, "task_id": ctx.task.id, "title": ctx.task.title},
        )

        try:
            if llm_available():
                summary, findings, artifacts, tool_records, tokens_in, tokens_out = (
                    await self._llm_execute(ctx)
                )
                if not summary:
                    summary, findings, artifacts = await self.heuristic(ctx)
            else:
                summary, findings, artifacts = await self.heuristic(ctx)
            success = True
            error = None
        except HookDenied as exc:
            summary = f"任务被拦截：{exc.reason}"
            findings = [f"拦截码：{exc.code}"]
            success = False
            error = exc.reason
        except Exception as exc:  # 子 Agent 失败不应拖垮整条运行
            logger.exception(f"[Agent:{self.name}] task {ctx.task.id} failed")
            summary = f"任务执行失败：{type(exc).__name__}: {exc}"
            findings = []
            success = False
            error = str(exc)

        finished = utcnow()
        duration_ms = int((time.perf_counter() - started_perf) * 1000)
        summary = truncate(summary or "", ctx.config.context.summary_max_chars)

        if success and not artifacts:
            path = await self._fallback_artifact(ctx, summary, findings)
            if path:
                artifacts.append(path)

        result = SubAgentResult(
            task_id=ctx.task.id,
            agent=self.name,
            success=success,
            summary=summary,
            findings=findings[:8],
            artifacts=artifacts,
            next_steps=[],
            confidence=0.72 if not llm_available() else 0.65,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tool_calls=tool_records,
            error=error,
            started_at=started,
            finished_at=finished,
            duration_ms=duration_ms,
        )
        await ctx.publish(
            RunEventType.AGENT_COMPLETED,
            {
                "agent": self.name,
                "task_id": ctx.task.id,
                "success": success,
                "summary": summary,
                "artifacts": [a.path for a in artifacts],
                "duration_ms": duration_ms,
            },
        )
        return result

    # -------------------------------------------------------------------- llm
    async def _llm_execute(
        self, ctx: AgentContext
    ) -> tuple[str, list[str], list[ArtifactRef], list[ToolCallRecord], int, int]:
        messages = self.build_messages(ctx)
        artifacts: list[ArtifactRef] = []
        records: list[ToolCallRecord] = []
        tokens_in = tokens_out = 0
        max_iterations = ctx.config.max_tool_iterations
        openai_tools = ctx.tools.openai_tools()

        for iteration in range(max_iterations):
            response = await self._model_call(ctx, messages, openai_tools)
            if response is None:
                return "", [], artifacts, records, tokens_in, tokens_out
            content = response.get("content") or ""
            tool_calls = response.get("tool_calls") or []
            tokens_in += estimate_tokens(json.dumps(messages, ensure_ascii=False))
            tokens_out += estimate_tokens(content)

            if not tool_calls:
                summary, findings = self._parse_final(content)
                return summary, findings, artifacts, records, tokens_in, tokens_out

            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": content or "",
                "tool_calls": [
                    {
                        "id": call.get("id") or f"call_{iteration}_{index}",
                        "type": "function",
                        "function": {
                            "name": call.get("name", ""),
                            "arguments": call.get("arguments", "{}"),
                        },
                    }
                    for index, call in enumerate(tool_calls)
                ],
            }
            messages.append(assistant_message)

            for index, call in enumerate(tool_calls):
                name = call.get("name", "")
                arguments = _loads(call.get("arguments"))
                observation, record, new_artifacts = await self._tool_call(
                    ctx, name, arguments
                )
                records.append(record)
                artifacts.extend(new_artifacts)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or f"call_{iteration}_{index}",
                        "content": truncate(
                            json.dumps(observation, ensure_ascii=False, default=str),
                            6000,
                        ),
                    }
                )

        # 轮次用尽：要求模型基于已有观察给结论（不再提供工具）
        messages.append(
            {
                "role": "user",
                "content": "工具调用轮次已用尽。请直接基于以上观察给出结构化结论，不要再请求工具。",
            }
        )
        response = await self._model_call(ctx, messages, [])
        content = (response or {}).get("content", "")
        summary, findings = self._parse_final(content)
        return summary, findings, artifacts, records, tokens_in, tokens_out

    async def _model_call(
        self,
        ctx: AgentContext,
        messages: list[dict],
        tools: list[dict],
    ) -> dict | None:
        mw = build_context(
            ctx.run_id,
            HookPoint.BEFORE_MODEL,
            payload={
                "messages": messages,
                "tools": tools,
                "model": llm_service.model,
                "cacheable": True,
                "max_attempts": 2,
            },
            user_id=ctx.user_id,
            agent=self.name,
            task_id=ctx.task.id,
            services=ctx.services,
        )
        try:
            if tools:
                result = await ctx.middleware.run(
                    mw,
                    lambda: llm_service.chat_with_tools(
                        messages, tools, temperature=0.2, max_tokens=2048
                    ),
                )
                return result if isinstance(result, dict) else None
            # 轮次用尽后的总结调用不再带工具；部分 OpenAI 兼容端点拒绝空 tools 数组
            content = await ctx.middleware.run(
                mw,
                lambda: llm_service.chat(
                    messages, temperature=0.2, max_tokens=2048
                ),
            )
            return {"content": content, "tool_calls": []}
        except HookDenied:
            raise
        except Exception as exc:
            logger.warning(f"[Agent:{self.name}] model call failed: {exc}")
            return None

    # ------------------------------------------------------------------ tools
    async def _tool_call(
        self,
        ctx: AgentContext,
        name: str,
        arguments: dict,
    ) -> tuple[Any, ToolCallRecord, list[ArtifactRef]]:
        started = time.perf_counter()
        tool = ctx.tools.get(name)
        if tool is None:
            record = ToolCallRecord(
                name=name, arguments=arguments, success=False,
                error=f"tool not available: {name}",
            )
            return {"error": record.error}, record, []

        args = _clean_arguments(tool, arguments)
        spec = tool.spec()
        target = str(
            args.get("path")
            or args.get("rule_id")
            or args.get("name")
            or args.get("query")
            or name
        )
        mw = build_context(
            ctx.run_id,
            HookPoint.BEFORE_TOOL,
            payload={
                "tool_name": name,
                "arguments": args,
                "tool_spec": spec,
                "target": target,
                "cacheable": tool.cacheable,
                "max_attempts": 2,
                "duration_ms": 0,
            },
            user_id=ctx.user_id,
            agent=self.name,
            task_id=ctx.task.id,
            services=ctx.services,
        )
        await ctx.publish(
            RunEventType.TOOL_STARTED,
            {"tool": name, "agent": self.name, "task_id": ctx.task.id, "arguments": args},
        )

        success = True
        error: str | None = None
        output: Any = None
        try:
            output = await ctx.middleware.run(mw, lambda: tool.run(**args))
        except HookDenied:
            # 权限/审批拒绝必须让任务失败，不能继续让模型“编”一个成功结论
            raise
        except Exception as exc:
            success = False
            error = f"{type(exc).__name__}: {exc}"
            output = {"error": error}
            logger.warning(f"[Agent:{self.name}] tool {name} failed: {exc}")

        duration_ms = int((time.perf_counter() - started) * 1000)
        artifacts = _extract_artifacts(output)
        record = ToolCallRecord(
            name=name,
            arguments=args,
            success=success,
            duration_ms=duration_ms,
            risk=spec.risk,
            output_preview=truncate(
                json.dumps(_jsonable(output), ensure_ascii=False, default=str), 400
            ),
            error=error,
        )
        await ctx.publish(
            RunEventType.TOOL_COMPLETED,
            {
                "tool": name,
                "agent": self.name,
                "task_id": ctx.task.id,
                "success": success,
                "duration_ms": duration_ms,
                "artifacts": [a.path for a in artifacts],
                "approval_id": mw.payload.get("approval_id"),
            },
        )
        return output, record, artifacts

    # -------------------------------------------------------------- overrides
    def build_messages(self, ctx: AgentContext) -> list[dict]:
        skill_hint = ctx.skills.catalog_prompt()
        memory_hint = _format_memories(ctx.memory_context)
        system = self.system_prompt(ctx, skill_hint, memory_hint)
        user = self.task_prompt(ctx)
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def system_prompt(self, ctx: AgentContext, skill_hint: str, memory_hint: str) -> str:
        return (
            f"你是风控策略运维 Harness 中的「{self.name}」子 Agent。{self.description}\n"
            "你只能看到当前任务，不继承主 Agent 的完整对话。\n"
            "工作方式：优先调用工具获取事实，不要臆造数据；大结果写入虚拟文件系统，"
            "最终只回传简洁结论。\n"
            "可用技能目录（需要时调用 skill_view 加载正文）：\n"
            f"{skill_hint or '（无）'}\n\n"
            f"历史经验（仅供参考，需验证）：\n{memory_hint or '（无）'}\n\n"
            "最终回复格式：\n"
            "1. 先用 3-6 行给出结论摘要；\n"
            "2. 再列出关键发现（每行以 `- ` 开头）；\n"
            "3. 如需主 Agent 关注，列出下一步建议。"
        )

    def task_prompt(self, ctx: AgentContext) -> str:
        task = ctx.task
        return (
            f"总目标：{ctx.objective}\n"
            f"当前任务：{task.title}\n"
            f"任务说明：{task.description or '（无）'}\n"
            f"任务输入：{json.dumps(task.inputs, ensure_ascii=False)}\n"
            f"验收标准：{json.dumps(task.acceptance, ensure_ascii=False)}\n"
            f"运行 ID：{ctx.run_id}；任务 ID：{task.id}"
        )

    async def heuristic(
        self, ctx: AgentContext
    ) -> tuple[str, list[str], list[ArtifactRef]]:
        """离线确定性实现；子类必须覆盖。"""
        raise NotImplementedError

    # ---------------------------------------------------------------- helpers
    def _parse_final(self, content: str) -> tuple[str, list[str]]:
        if not content:
            return "", []
        findings = [
            line.strip()[2:].strip()
            for line in content.splitlines()
            if line.strip().startswith(("- ", "* "))
        ]
        summary = content.strip()
        if findings:
            # 摘要取第一段非列表文本，避免把整篇长文塞回主上下文
            paragraphs = [
                p.strip()
                for p in content.split("\n\n")
                if p.strip() and not p.strip().startswith(("- ", "* ", "#"))
            ]
            if paragraphs:
                summary = paragraphs[0]
        return summary, findings[:8]

    async def _fallback_artifact(
        self,
        ctx: AgentContext,
        summary: str,
        findings: list[str],
    ) -> ArtifactRef | None:
        try:
            path = f"/reports/{self.name}_{ctx.task.seq}_{ctx.task.id}.md"
            body = (
                f"# {ctx.task.title}\n\n"
                f"- 任务 ID：{ctx.task.id}\n"
                f"- Agent：{self.name}\n"
                f"- 生成时间：{utcnow().isoformat()}\n\n"
                f"## 结论\n\n{summary}\n\n"
                f"## 关键发现\n\n" + "\n".join(f"- {f}" for f in findings)
            )
            entry = await ctx.vfs.write(path, body, author=self.name, comment="task result")
            return ArtifactRef(
                path=entry.path,
                kind="report",
                summary=truncate(summary, 120),
                size=entry.size,
                sha256=entry.sha256,
                created_by=self.name,
            )
        except Exception as exc:
            logger.debug(f"[Agent:{self.name}] fallback artifact failed: {exc}")
            return None


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _loads(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _clean_arguments(tool: HarnessTool, arguments: dict) -> dict:
    """只保留 schema 声明的参数，忽略模型幻觉出的多余字段。"""
    properties = (tool.parameters or {}).get("properties", {})
    if not properties:
        return {}
    return {key: value for key, value in arguments.items() if key in properties}


def _extract_artifacts(output: Any) -> list[ArtifactRef]:
    artifacts: list[ArtifactRef] = []
    if not isinstance(output, dict):
        return artifacts
    path = output.get("path")
    if isinstance(path, str) and path.startswith("/"):
        artifacts.append(
            ArtifactRef(
                path=path,
                kind=str(output.get("kind", "artifact")),
                summary=str(output.get("comment", ""))[:200],
                size=int(output.get("size", 0) or 0),
                sha256=str(output.get("sha256", "")),
            )
        )
    report = output.get("report_path")
    if isinstance(report, str) and report.startswith("/"):
        artifacts.append(ArtifactRef(path=report, kind="report", summary="backtest report"))
    for key in ("artifact", "record", "approval_material", "snapshot"):
        value = output.get(key)
        if isinstance(value, str) and value.startswith("/") and not any(
            a.path == value for a in artifacts
        ):
            artifacts.append(ArtifactRef(path=value, kind=key, summary=key))
    return artifacts


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _format_memories(records: list[MemoryRecord]) -> str:
    if not records:
        return ""
    lines = []
    for record in records[:5]:
        tags = f"（{'/'.join(record.tags[:4])}）" if record.tags else ""
        lines.append(f"- [{record.kind}]{tags} {truncate(record.text, 240)}")
    return "\n".join(lines)


def build_agent_context(
    *,
    run_id: str,
    objective: str,
    task: TaskItem,
    user_id: int | None,
    vfs: VirtualFileSystem,
    memory: MemoryManager,
    sandbox: SandboxManager,
    skills: SkillRegistry,
    tools: ToolRegistry,
    middleware: MiddlewareChain,
    services: dict,
    memory_context: list[MemoryRecord] | None = None,
    emit: EmitFn | None = None,
    config: HarnessConfig | None = None,
) -> AgentContext:
    return AgentContext(
        run_id=run_id,
        objective=objective,
        task=task,
        user_id=user_id,
        vfs=vfs,
        memory=memory,
        sandbox=sandbox,
        skills=skills,
        tools=tools,
        middleware=middleware,
        services=services,
        memory_context=memory_context or [],
        emit=emit,
        config=config or get_harness_config(),
    )
