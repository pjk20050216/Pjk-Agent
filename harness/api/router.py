"""Harness API —— 异步运行、SSE 事件流、任务板、虚拟文件、审批、技能、记忆与追踪。

鉴权复用 ``server.dependencies.get_current_user``：既支持网关内部令牌，
也支持直接 Bearer JWT。所有运行资源按 user_id 隔离。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from harness.agents.subagents import AGENT_CATALOG
from harness.api.schemas import (
    ApprovalDecisionRequest,
    CreateRunRequest,
    FileWriteRequest,
    MemoryCreateRequest,
    ResumeRequest,
)
from harness.contracts import (
    ApprovalStatus,
    FilePlane,
    MemoryRecord,
    RunEventType,
    RunSnapshot,
    RunStatus,
)
from harness.integrations.kafka import audit_payload, sink
from harness.runtime import get_harness
from harness.tools import SUBAGENT_TOOLSETS, build_registry
from harness.tools.context import ToolContext
from harness.vfs.store import VFSNotFound, VirtualFileSystem
from server.database import get_db
from server.dependencies import get_current_user
from server.models.user import User
from server.security import intent_guard
from server.utils.logger import logger
from server.utils.sse import sse_event, sse_response

router = APIRouter(prefix="/api/harness", tags=["harness"])

harness = get_harness()


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _role_for(user: User) -> str:
    admins = {
        name.strip()
        for name in os.environ.get("HARNESS_ADMIN_USERS", "").split(",")
        if name.strip()
    }
    return "risk_admin" if user.username in admins else "operator"


async def _owned_run(run_id: str, user: User) -> RunSnapshot:
    snapshot = await harness.get(run_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="run not found")
    if snapshot.user_id is not None and snapshot.user_id != user.id:
        raise HTTPException(status_code=403, detail="run belongs to another user")
    return snapshot


def _vfs(run_id: str) -> VirtualFileSystem:
    return VirtualFileSystem(run_id, harness.config)


async def _audit(
    *,
    event: str,
    run_id: str,
    user: User,
    action: str,
    target: str,
    risk: str = "low",
    outcome: str = "ok",
    detail: dict | None = None,
) -> None:
    try:
        await sink.emit(
            "audit",
            audit_payload(
                event=event,
                run_id=run_id,
                user_id=user.id,
                actor=user.username,
                action=action,
                target=target,
                risk=risk,
                outcome=outcome,
                detail=detail,
                trace_id=harness.recorder.trace_id(run_id),
            ),
        )
    except Exception:
        pass


def _sse(event) -> str:
    return sse_event(event.type, event.model_dump(mode="json"))


# --------------------------------------------------------------------------- #
# 元信息
# --------------------------------------------------------------------------- #
@router.get("/status")
async def status(current_user: User = Depends(get_current_user)):
    """运行时状态：并行度、记忆/沙箱/审批/Kafka/Java 桥、技能数量。"""
    return {
        "harness": harness.status(),
        "role": _role_for(current_user),
        "workspace_root": str(harness.config.workspace_root),
        "skills_dir": str(harness.config.skills_dir),
        "context": {
            "budget_tokens": harness.config.context.budget_tokens,
            "summary_max_chars": harness.config.context.summary_max_chars,
        },
    }


@router.get("/agents")
async def list_agents(current_user: User = Depends(get_current_user)):
    return {
        "agents": [
            {
                **agent,
                "tools": sorted(SUBAGENT_TOOLSETS.get(agent["name"], set())),
            }
            for agent in AGENT_CATALOG
        ]
    }


@router.get("/tools")
async def list_tools(
    run_id: str | None = None,
    current_user: User = Depends(get_current_user),
):
    """工具目录。传入 run_id 可看到该运行实际可用的工具集。"""
    if run_id:
        await _owned_run(run_id, current_user)
        vfs = _vfs(run_id)
        allow = None
    else:
        vfs = None
        allow = None
    ctx = ToolContext(
        run_id=run_id or "__catalog__",
        user_id=current_user.id,
        vfs=vfs,  # type: ignore[arg-type]
        memory=harness.memory,
        sandbox=harness.sandbox,
        skills=harness.skills,
        approvals=harness.approvals,
        java_bridge=harness.java_bridge,
        plan_provider=None,
    )
    registry = await build_registry(ctx, allow)
    return {
        "tools": [spec.model_dump(mode="json") for spec in registry.specs()],
        "count": len(registry.list()),
    }


# --------------------------------------------------------------------------- #
# 运行
# --------------------------------------------------------------------------- #
@router.post("/runs")
async def create_run(
    req: CreateRunRequest,
    current_user: User = Depends(get_current_user),
):
    """创建并异步启动一次 Harness 运行，立即返回 run 快照。"""
    if not harness.config.enabled:
        raise HTTPException(status_code=503, detail="harness disabled")

    verdict = await intent_guard.check(req.objective)
    if verdict.blocked:
        raise HTTPException(status_code=403, detail=f"请求被拒绝：{verdict.reason}")

    metadata = dict(req.metadata or {})
    metadata.update(
        {"user_role": _role_for(current_user), "username": current_user.username}
    )
    snapshot = await harness.create_run(
        req.objective,
        user_id=current_user.id,
        mode=req.mode,
        metadata=metadata,
    )
    await harness.start(snapshot.id)
    await _audit(
        event="run.created",
        run_id=snapshot.id,
        user=current_user,
        action="run.create",
        target=req.objective[:200],
        detail={"mode": req.mode},
    )
    return snapshot.model_dump(mode="json")


@router.post("/runs/stream")
async def create_run_stream(
    req: CreateRunRequest,
    current_user: User = Depends(get_current_user),
):
    """创建并启动运行，直接返回 SSE 事件流（适合一次性调用）。"""
    if not harness.config.enabled:
        raise HTTPException(status_code=503, detail="harness disabled")
    verdict = await intent_guard.check(req.objective)
    if verdict.blocked:
        raise HTTPException(status_code=403, detail=f"请求被拒绝：{verdict.reason}")

    metadata = dict(req.metadata or {})
    metadata.update(
        {"user_role": _role_for(current_user), "username": current_user.username}
    )
    snapshot = await harness.create_run(
        req.objective, user_id=current_user.id, mode=req.mode, metadata=metadata
    )
    await harness.start(snapshot.id)

    async def generator():
        async for event in harness.events(snapshot.id):
            yield _sse(event)

    return sse_response(generator())


@router.get("/runs")
async def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
):
    runs = await harness.list_runs(current_user.id, limit=limit)
    return {
        "runs": [r.model_dump(mode="json") for r in runs],
        "total": len(runs),
    }


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    current_user: User = Depends(get_current_user),
):
    snapshot = await _owned_run(run_id, current_user)
    approvals = await asyncio.to_thread(
        harness.store.list_approvals, run_id, None, current_user.id
    )
    data = snapshot.model_dump(mode="json")
    data["approvals"] = [a.model_dump(mode="json") for a in approvals]
    data["trace_id"] = harness.recorder.trace_id(run_id)
    return data


@router.post("/runs/{run_id}/resume")
async def resume_run(
    run_id: str,
    req: ResumeRequest,
    current_user: User = Depends(get_current_user),
):
    await _owned_run(run_id, current_user)
    snapshot = await harness.resume(run_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="run not found")
    await _audit(
        event="run.resumed",
        run_id=run_id,
        user=current_user,
        action="run.resume",
        target=req.reason[:200],
    )
    return snapshot.model_dump(mode="json")


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    current_user: User = Depends(get_current_user),
):
    await _owned_run(run_id, current_user)
    snapshot = await harness.cancel(run_id)
    await _audit(
        event="run.cancelled",
        run_id=run_id,
        user=current_user,
        action="run.cancel",
        target=run_id,
    )
    return snapshot.model_dump(mode="json") if snapshot else {"error": "not found"}


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: str,
    after_seq: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """SSE：先回放历史事件，再实时推送；断线可用 after_seq 续传。"""
    snapshot = await _owned_run(run_id, current_user)

    # 已终态的运行只回放历史，不再等待后续事件（否则 after_seq 超过末尾会挂起）
    if RunStatus(snapshot.status) in {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }:
        async def replay():
            events = await asyncio.to_thread(harness.bus.read_events, run_id, after_seq)
            for event in events:
                yield _sse(event)

        return sse_response(replay())

    async def generator():
        async for event in harness.events(run_id, after_seq):
            yield _sse(event)

    return sse_response(generator())


@router.get("/runs/{run_id}/events/replay")
async def replay_events(
    run_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=1000, ge=1, le=5000),
    current_user: User = Depends(get_current_user),
):
    await _owned_run(run_id, current_user)
    events = await asyncio.to_thread(harness.bus.read_events, run_id, after_seq)
    return {
        "events": [e.model_dump(mode="json") for e in events[:limit]],
        "total": len(events),
    }


@router.get("/runs/{run_id}/tasks")
async def run_tasks(
    run_id: str,
    current_user: User = Depends(get_current_user),
):
    snapshot = await _owned_run(run_id, current_user)
    if snapshot.plan is None:
        return {"tasks": [], "plan": None}
    return {
        "plan": {
            "objective": snapshot.plan.objective,
            "strategy": snapshot.plan.strategy,
            "planner": snapshot.plan.metadata.get("planner", ""),
        },
        "tasks": [t.model_dump(mode="json") for t in snapshot.plan.tasks],
    }


@router.get("/runs/{run_id}/traces")
async def run_traces(
    run_id: str,
    current_user: User = Depends(get_current_user),
):
    await _owned_run(run_id, current_user)
    spans = await asyncio.to_thread(harness.recorder.read_spans, run_id)
    return {"run_id": run_id, "spans": spans, "count": len(spans)}


# --------------------------------------------------------------------------- #
# 虚拟文件系统
# --------------------------------------------------------------------------- #
@router.get("/runs/{run_id}/files")
async def list_files(
    run_id: str,
    path: str = Query(default="/"),
    plane: str = Query(default="host"),
    recursive: bool = Query(default=True),
    current_user: User = Depends(get_current_user),
):
    await _owned_run(run_id, current_user)
    vfs = _vfs(run_id)
    try:
        entries = await vfs.list_dir(
            path, plane=FilePlane(plane), recursive=recursive
        )
    except VFSNotFound:
        raise HTTPException(status_code=404, detail=f"path not found: {path}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "path": path,
        "plane": plane,
        "entries": [e.model_dump(mode="json") for e in entries],
    }


@router.get("/runs/{run_id}/file")
async def read_file(
    run_id: str,
    path: str,
    plane: str = Query(default="host"),
    current_user: User = Depends(get_current_user),
):
    await _owned_run(run_id, current_user)
    vfs = _vfs(run_id)
    try:
        content = await vfs.read(path, plane=FilePlane(plane))
        entry = await vfs.stat(path, plane=FilePlane(plane))
    except VFSNotFound:
        raise HTTPException(status_code=404, detail="file not found")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "entry": entry.model_dump(mode="json"),
        "content": content,
    }


@router.post("/runs/{run_id}/files")
async def write_file(
    run_id: str,
    req: FileWriteRequest,
    current_user: User = Depends(get_current_user),
):
    await _owned_run(run_id, current_user)
    vfs = _vfs(run_id)
    try:
        entry = await vfs.write(
            req.path,
            req.content,
            author=current_user.username,
            comment=req.comment or "api write",
            plane=FilePlane(req.plane),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await harness.bus.publish(
        run_id,
        RunEventType.FILE_WRITTEN,
        {"path": entry.path, "size": entry.size, "author": current_user.username},
        trace_id=harness.recorder.trace_id(run_id),
    )
    await _audit(
        event="file.written",
        run_id=run_id,
        user=current_user,
        action="file.write",
        target=entry.path,
        detail={"size": entry.size, "plane": req.plane},
    )
    return entry.model_dump(mode="json")


@router.get("/runs/{run_id}/file/history")
async def file_history(
    run_id: str,
    path: str,
    current_user: User = Depends(get_current_user),
):
    await _owned_run(run_id, current_user)
    versions = await _vfs(run_id).history(path)
    return {
        "path": path,
        "versions": [v.model_dump(mode="json") for v in versions],
    }


@router.get("/runs/{run_id}/file/diff")
async def file_diff(
    run_id: str,
    path: str,
    from_version: int,
    to_version: int,
    current_user: User = Depends(get_current_user),
):
    await _owned_run(run_id, current_user)
    try:
        diff = await _vfs(run_id).diff(path, from_version, to_version)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"path": path, "diff": diff}


# --------------------------------------------------------------------------- #
# 审批
# --------------------------------------------------------------------------- #
@router.get("/approvals")
async def list_approvals(
    status: str | None = Query(default="pending"),
    run_id: str | None = None,
    current_user: User = Depends(get_current_user),
):
    if run_id:
        await _owned_run(run_id, current_user)
    approvals = await asyncio.to_thread(
        harness.store.list_approvals,
        run_id,
        ApprovalStatus(status) if status else None,
        current_user.id,
    )
    return {
        "approvals": [a.model_dump(mode="json") for a in approvals],
        "count": len(approvals),
    }


@router.post("/approvals/{approval_id}/decision")
async def decide_approval(
    approval_id: str,
    req: ApprovalDecisionRequest,
    current_user: User = Depends(get_current_user),
):
    approval = await asyncio.to_thread(harness.store.get_approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    await _owned_run(approval.run_id, current_user)
    if approval.status != ApprovalStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"approval already {approval.status}")

    decided = await harness.approvals.decide(
        approval_id,
        approved=req.approved,
        decided_by=current_user.username,
        comment=req.comment,
    )
    await _audit(
        event="approval.decided",
        run_id=approval.run_id,
        user=current_user,
        action=approval.action,
        target=approval.target,
        risk=str(approval.risk),
        outcome="approved" if req.approved else "rejected",
        detail={"approval_id": approval_id, "comment": req.comment},
    )

    if decided is not None and not req.approved:
        if not harness.is_running(decided.run_id):
            await harness.mark_failed(
                decided.run_id, f"approval {approval_id} rejected"
            )
    elif req.approved:
        snapshot = await harness.get(approval.run_id)
        if snapshot and RunStatus(snapshot.status) == RunStatus.WAITING_APPROVAL:
            if not harness.is_running(approval.run_id):
                await harness.resume(approval.run_id)

    return decided.model_dump(mode="json") if decided else {"error": "not found"}


# --------------------------------------------------------------------------- #
# 技能 / 记忆 / 沙箱
# --------------------------------------------------------------------------- #
@router.get("/skills")
async def list_skills(current_user: User = Depends(get_current_user)):
    return {
        "skills": [m.model_dump(mode="json") for m in harness.skills.list_meta()],
        "count": len(harness.skills),
    }


@router.get("/skills/{name}")
async def get_skill(name: str, current_user: User = Depends(get_current_user)):
    skill = harness.skills.get(name)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return {
        "meta": skill.meta.model_dump(mode="json"),
        "body": skill.body,
    }


@router.get("/skills/{name}/resource")
async def get_skill_resource(
    name: str,
    path: str,
    current_user: User = Depends(get_current_user),
):
    try:
        content = harness.skills.load_resource(name, path)
    except KeyError:
        raise HTTPException(status_code=404, detail="skill not found")
    except (FileNotFoundError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"name": name, "path": path, "content": content}


@router.get("/memory/search")
async def search_memory(
    q: str = Query(min_length=1),
    kind: str | None = None,
    top_k: int = Query(default=5, ge=1, le=20),
    current_user: User = Depends(get_current_user),
):
    records = await harness.memory.recall(
        q, user_id=current_user.id, top_k=top_k, kind=kind
    )
    return {
        "query": q,
        "memories": [r.model_dump(mode="json") for r in records],
        "count": len(records),
    }


@router.get("/memory/recent")
async def recent_memory(
    limit: int = Query(default=20, ge=1, le=100),
    kind: str | None = None,
    current_user: User = Depends(get_current_user),
):
    records = await harness.memory.recent(limit=limit, kind=kind)
    return {"memories": [r.model_dump(mode="json") for r in records]}


@router.post("/memory")
async def create_memory(
    req: MemoryCreateRequest,
    current_user: User = Depends(get_current_user),
):
    record = MemoryRecord(
        kind=req.kind,
        text=req.text,
        tags=req.tags,
        user_id=current_user.id,
        metadata={"source": "api"},
    )
    stored = await harness.memory.remember([record])
    return {"stored": stored, "memory": record.model_dump(mode="json")}


@router.get("/sandbox/status")
async def sandbox_status(
    run_id: str | None = None,
    current_user: User = Depends(get_current_user),
):
    if run_id:
        await _owned_run(run_id, current_user)
    return harness.sandbox.status(run_id)
