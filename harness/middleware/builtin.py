"""内置中间件 —— 缓存、重试、权限、PII、敏感动作审批、链路追踪、审计上报。

所有中间件都是「可插拔」的：在 :func:`build_default_chain` 中按 order 组合，
业务侧也可以只挑选其中几个，或注册自定义 Middleware。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from typing import Any, Awaitable, Callable

from harness.contracts import (
    RISK_ORDER,
    ApprovalStatus,
    HookPoint,
    RiskLevel,
    RunEventType,
)
from harness.events import bus
from harness.integrations.kafka import audit_payload, sink
from harness.middleware.base import (
    ApprovalRequired,
    HookDenied,
    Middleware,
    MiddlewareChain,
    MiddlewareContext,
)
from harness.observability.tracing import recorder
from server.utils.logger import logger
from server.utils.redis_client import redis_client

# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #
CACHE_PREFIX = "harness:mw:cache:"


class CacheMiddleware(Middleware):
    """模型 / 只读工具结果缓存（Redis 优先，进程内 LRU 兜底）。"""

    name = "cache"
    order = 10
    before_hooks = {HookPoint.BEFORE_MODEL, HookPoint.BEFORE_TOOL}
    after_hooks = {HookPoint.AFTER_MODEL, HookPoint.AFTER_TOOL}

    def __init__(self) -> None:
        self._local: dict[str, tuple[float, Any]] = {}
        self._ttl = 900
        self._max_local = 256

    def _key(self, ctx: MiddlewareContext) -> str:
        material = {
            "hook": ctx.hook.value,
            # 用户与运行维度必须进入 key：知识检索/记忆搜索可能包含用户私有经验，
            # VFS 读取也可能随 run 变化，跨用户/跨运行复用会造成数据泄漏或脏读。
            "user_id": ctx.user_id,
            "run_id": ctx.run_id,
            "tool": ctx.payload.get("tool_name", ""),
            "args": ctx.payload.get("arguments", {}),
            "messages": ctx.payload.get("messages", []),
            "tools": ctx.payload.get("tools", []),
            "model": ctx.payload.get("model", ""),
        }
        digest = hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return f"{CACHE_PREFIX}{digest}"

    async def before(self, ctx: MiddlewareContext) -> None:
        if not ctx.payload.get("cacheable"):
            return
        key = self._key(ctx)
        ctx.state["cache_key"] = key

        client = redis_client.client
        if client is not None:
            try:
                raw = await client.get(key)
                if raw:
                    ctx.result = json.loads(raw)
                    ctx.short_circuit = True
                    ctx.metadata["cache_hit"] = "redis"
                    return
            except Exception:
                pass

        entry = self._local.get(key)
        if entry and entry[0] > time.time():
            ctx.result = entry[1]
            ctx.short_circuit = True
            ctx.metadata["cache_hit"] = "local"

    async def after(self, ctx: MiddlewareContext) -> None:
        if ctx.error is not None or ctx.short_circuit:
            return
        if not ctx.payload.get("cacheable"):
            return
        key = ctx.state.get("cache_key")
        if not key or ctx.result is None:
            return
        payload = _jsonable(ctx.result)
        client = redis_client.client
        if client is not None:
            try:
                await client.set(key, json.dumps(payload, ensure_ascii=False, default=str), ex=self._ttl)
                return
            except Exception:
                pass
        if len(self._local) >= self._max_local:
            self._local.pop(next(iter(self._local)))
        self._local[key] = (time.time() + self._ttl, payload)


# --------------------------------------------------------------------------- #
# 重试
# --------------------------------------------------------------------------- #
class RetryMiddleware(Middleware):
    """对瞬时故障重试；权限拒绝与审批拒绝不重试。"""

    name = "retry"
    order = 20
    before_hooks = {HookPoint.BEFORE_MODEL, HookPoint.BEFORE_TOOL}

    async def around(
        self,
        ctx: MiddlewareContext,
        call: Callable[[], Awaitable[Any]],
    ) -> Any:
        attempts = max(1, int(ctx.payload.get("max_attempts", 2)))
        last_error: BaseException | None = None
        for attempt in range(attempts):
            try:
                return await call()
            except (HookDenied, ApprovalRequired):
                raise
            except Exception as exc:
                last_error = exc
                if not _retryable(exc) or attempt == attempts - 1:
                    raise
                delay = 0.4 * (2 ** attempt)
                logger.warning(
                    f"[Retry] {ctx.hook.value} attempt {attempt + 1} failed ({exc}); "
                    f"retrying in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
        if last_error is not None:
            raise last_error
        raise RuntimeError("retry middleware reached unreachable state")


def _retryable(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    return any(token in name for token in ("timeout", "connection", "ratelimit", "api", "transport"))


# --------------------------------------------------------------------------- #
# 权限
# --------------------------------------------------------------------------- #
DEFAULT_ROLE_DENY: dict[str, set[str]] = {
    "viewer": {
        "rule.publish", "rule.gray_release", "policy.write", "policy.draft",
        "file.promote", "sandbox.exec", "sandbox.write",
        "java.tool.invoke", "file.write", "memory.write",
    },
    "operator": set(),
    "risk_admin": set(),
    "admin": set(),
}


class PermissionMiddleware(Middleware):
    """基于角色的动作权限校验（与审批正交：先有权限，再谈审批）。"""

    name = "permission"
    order = 30
    before_hooks = {HookPoint.BEFORE_TOOL, HookPoint.BEFORE_FILE_WRITE}

    def __init__(self) -> None:
        self._deny = self._load_policy()

    @staticmethod
    def _load_policy() -> dict[str, set[str]]:
        raw = os.environ.get("HARNESS_ROLE_DENY", "")
        if not raw:
            return DEFAULT_ROLE_DENY
        try:
            parsed = json.loads(raw)
            return {str(role): {str(a) for a in actions} for role, actions in parsed.items()}
        except Exception:
            return DEFAULT_ROLE_DENY

    async def before(self, ctx: MiddlewareContext) -> None:
        role = str(ctx.services.get("user_role", "operator"))
        if role in {"admin", "risk_admin"}:
            return
        if ctx.hook == HookPoint.BEFORE_FILE_WRITE:
            action = "file.write"
        else:
            spec = ctx.payload.get("tool_spec")
            action = getattr(spec, "action", "tool.invoke") if spec else "tool.invoke"
        denied = self._deny.get(role, set())
        if action in denied:
            raise HookDenied(
                f"role '{role}' is not permitted to perform '{action}'",
                code="permission_denied",
                risk=RiskLevel.HIGH,
            )


# --------------------------------------------------------------------------- #
# PII
# --------------------------------------------------------------------------- #
PII_PATTERNS = {
    "phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "id_card": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "bank_card": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
}


def _mask(value: str) -> str:
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}{'*' * max(3, len(value) - 5)}{value[-2:]}"


class PIIMiddleware(Middleware):
    """敏感信息检测与脱敏：命中即记录，默认脱敏后再送模型/落盘。"""

    name = "pii"
    order = 40
    before_hooks = {HookPoint.BEFORE_MODEL, HookPoint.BEFORE_FILE_WRITE}

    def __init__(self) -> None:
        self.redact = os.environ.get("HARNESS_PII_REDACT", "true").lower() in {"1", "true", "yes"}

    def _scan(self, text: str) -> tuple[str, dict[str, int]]:
        findings: dict[str, int] = {}
        result = text
        for kind, pattern in PII_PATTERNS.items():
            matches = pattern.findall(result)
            if not matches:
                continue
            findings[kind] = len(matches)
            if self.redact:
                result = pattern.sub(lambda m: _mask(m.group(0)), result)
        return result, findings

    async def before(self, ctx: MiddlewareContext) -> None:
        findings: dict[str, int] = {}
        if ctx.hook == HookPoint.BEFORE_MODEL:
            messages = ctx.payload.get("messages")
            if isinstance(messages, list):
                cleaned = []
                for message in messages:
                    if isinstance(message, dict) and isinstance(message.get("content"), str):
                        text, found = self._scan(message["content"])
                        for key, count in found.items():
                            findings[key] = findings.get(key, 0) + count
                        cleaned.append({**message, "content": text})
                    else:
                        cleaned.append(message)
                ctx.payload["messages"] = cleaned
        elif ctx.hook == HookPoint.BEFORE_FILE_WRITE:
            content = ctx.payload.get("content")
            if isinstance(content, str):
                text, findings = self._scan(content)
                ctx.payload["content"] = text
        if findings:
            ctx.metadata["pii_findings"] = findings
            logger.warning(f"[PII] detected in {ctx.hook.value}: {findings}")


# --------------------------------------------------------------------------- #
# 敏感动作审批
# --------------------------------------------------------------------------- #
class SensitiveActionMiddleware(Middleware):
    """高风险工具/动作在真正执行前挂起，等待人工审批。"""

    name = "sensitive_action"
    order = 50
    before_hooks = {HookPoint.BEFORE_TOOL}

    def __init__(self, services: dict | None = None) -> None:
        self.services = services or {}

    async def before(self, ctx: MiddlewareContext) -> None:
        spec = ctx.payload.get("tool_spec")
        if spec is None:
            return
        action = getattr(spec, "action", "tool.invoke")
        risk = getattr(spec, "risk", RiskLevel.LOW)
        requires = bool(getattr(spec, "requires_approval", False))
        manager = ctx.services.get("approvals") or self.services.get("approvals")
        if manager is None:
            return
        if not (requires or manager.approval_config.requires_approval(action)):
            return
        # RiskLevel 是 str 子类，直接比较会按字典序（critical < high），必须用序表
        if not requires and RISK_ORDER[RiskLevel(risk)] < RISK_ORDER[RiskLevel.HIGH]:
            return

        target = str(ctx.payload.get("target") or getattr(spec, "name", ""))
        reason = str(
            ctx.payload.get("approval_reason")
            or f"tool '{getattr(spec, 'name', action)}' performs high-risk action '{action}'"
        )
        approval = await manager.request(
            run_id=ctx.run_id,
            action=action,
            target=target,
            risk=risk,
            reason=reason,
            payload={
                "tool": getattr(spec, "name", ""),
                "arguments": _jsonable(ctx.payload.get("arguments", {})),
                "agent": ctx.agent,
            },
            task_id=ctx.task_id,
            force=requires,
        )
        ctx.payload["approval_id"] = approval.id

        set_status = ctx.services.get("set_run_status")
        if set_status is not None:
            await set_status("waiting_approval", approval=approval)
        decided = await manager.wait(approval.id)
        if set_status is not None:
            await set_status("running")

        ctx.payload["approval"] = decided.model_dump(mode="json")
        if decided.status != ApprovalStatus.APPROVED:
            raise HookDenied(
                f"approval {decided.id} {decided.status} for action '{action}'",
                code="approval_rejected",
                risk=risk,
            )


# --------------------------------------------------------------------------- #
# 链路追踪
# --------------------------------------------------------------------------- #
TRACE_BEFORE_HOOKS = {
    HookPoint.RUN_START,
    HookPoint.BEFORE_MODEL,
    HookPoint.BEFORE_TOOL,
    HookPoint.BEFORE_FILE_READ,
    HookPoint.BEFORE_FILE_WRITE,
    HookPoint.BEFORE_TASK_STATE,
    HookPoint.BEFORE_DELEGATE,
    HookPoint.BEFORE_APPROVAL,
}

TRACE_AFTER_HOOKS = {
    HookPoint.RUN_END,
    HookPoint.ON_ERROR,
    HookPoint.AFTER_MODEL,
    HookPoint.AFTER_TOOL,
    HookPoint.AFTER_FILE_READ,
    HookPoint.AFTER_FILE_WRITE,
    HookPoint.AFTER_TASK_STATE,
    HookPoint.AFTER_DELEGATE,
    HookPoint.AFTER_APPROVAL,
}


class TracingMiddleware(Middleware):
    name = "tracing"
    order = 80
    before_hooks = TRACE_BEFORE_HOOKS
    after_hooks = TRACE_AFTER_HOOKS

    async def before(self, ctx: MiddlewareContext) -> None:
        ctx.state[f"trace_start:{id(ctx)}"] = time.perf_counter()

    async def after(self, ctx: MiddlewareContext) -> None:
        started = ctx.state.pop(f"trace_start:{id(ctx)}", None)
        if started is None:
            return
        duration_ms = int((time.perf_counter() - started) * 1000)
        name, kind = _span_identity(ctx)
        await recorder.record_completed_span(
            ctx.run_id,
            name,
            kind,
            {
                "hook": ctx.hook.value,
                "agent": ctx.agent,
                "task_id": ctx.task_id,
                "short_circuit": ctx.short_circuit,
                "cache_hit": ctx.metadata.get("cache_hit", ""),
            },
            duration_ms=duration_ms,
            status="error" if ctx.error else "ok",
        )


def _span_identity(ctx: MiddlewareContext) -> tuple[str, str]:
    hook = ctx.hook
    if hook in {HookPoint.BEFORE_MODEL, HookPoint.AFTER_MODEL}:
        return "model.call", "model"
    if hook in {HookPoint.BEFORE_TOOL, HookPoint.AFTER_TOOL}:
        return f"tool.{ctx.payload.get('tool_name', 'unknown')}", "tool"
    if hook in {HookPoint.BEFORE_FILE_READ, HookPoint.AFTER_FILE_READ}:
        return "file.read", "file"
    if hook in {HookPoint.BEFORE_FILE_WRITE, HookPoint.AFTER_FILE_WRITE}:
        return "file.write", "file"
    if hook in {HookPoint.BEFORE_TASK_STATE, HookPoint.AFTER_TASK_STATE}:
        return "task.transition", "task"
    if hook in {HookPoint.BEFORE_DELEGATE, HookPoint.AFTER_DELEGATE}:
        return "agent.delegate", "agent"
    if hook in {HookPoint.BEFORE_APPROVAL, HookPoint.AFTER_APPROVAL}:
        return "approval", "approval"
    if hook in {HookPoint.RUN_START, HookPoint.RUN_END}:
        return "run.lifecycle", "run"
    return hook.value, "internal"


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #
class AuditMiddleware(Middleware):
    """把关键操作上报到 Kafka（不可用时落本地缓冲）并写结构化日志。"""

    name = "audit"
    order = 90
    after_hooks = {
        HookPoint.AFTER_TOOL,
        HookPoint.AFTER_FILE_WRITE,
        HookPoint.AFTER_TASK_STATE,
        HookPoint.AFTER_APPROVAL,
        HookPoint.AFTER_DELEGATE,
        HookPoint.RUN_END,
        HookPoint.ON_ERROR,
    }

    async def after(self, ctx: MiddlewareContext) -> None:
        event, action, target, risk = _audit_fields(ctx)
        payload = audit_payload(
            event=event,
            run_id=ctx.run_id,
            user_id=ctx.user_id,
            actor=ctx.agent or "harness",
            action=action,
            target=target,
            outcome="error" if ctx.error else "ok",
            risk=risk.value if isinstance(risk, RiskLevel) else str(risk),
            detail={
                "task_id": ctx.task_id,
                "duration_ms": ctx.payload.get("duration_ms"),
                "cache_hit": ctx.metadata.get("cache_hit"),
                "pii": ctx.metadata.get("pii_findings"),
                "error": str(ctx.error) if ctx.error else None,
                "approval_id": ctx.payload.get("approval_id"),
            },
            trace_id=recorder.trace_id(ctx.run_id),
        )
        try:
            await sink.emit("audit", payload)
        except Exception as exc:
            logger.debug(f"[Audit] sink failed: {exc}")
        logger.info(
            f"[Audit] {event} run={ctx.run_id} action={action} target={target} "
            f"outcome={payload['outcome']}"
        )


def _audit_fields(ctx: MiddlewareContext) -> tuple[str, str, str, RiskLevel]:
    hook = ctx.hook
    if hook == HookPoint.AFTER_TOOL:
        spec = ctx.payload.get("tool_spec")
        return (
            "tool.completed",
            getattr(spec, "action", "tool.invoke") if spec else "tool.invoke",
            str(ctx.payload.get("tool_name", "")),
            getattr(spec, "risk", RiskLevel.LOW) if spec else RiskLevel.LOW,
        )

    if hook == HookPoint.AFTER_FILE_WRITE:
        return "file.written", "file.write", str(ctx.payload.get("path", "")), RiskLevel.LOW
    if hook == HookPoint.AFTER_TASK_STATE:
        return (
            "task.transition",
            "task.transition",
            str(ctx.payload.get("task_id", "")),
            RiskLevel.LOW,
        )
    if hook == HookPoint.AFTER_APPROVAL:
        return "approval.decided", "approval.decide", str(ctx.payload.get("approval_id", "")), RiskLevel.HIGH
    if hook == HookPoint.AFTER_DELEGATE:
        return "agent.delegated", "agent.delegate", ctx.agent, RiskLevel.LOW
    if hook == HookPoint.RUN_END:
        return "run.completed", "run.end", ctx.run_id, RiskLevel.LOW
    return "error", "error", "", RiskLevel.LOW


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #
def build_default_chain(services: dict | None = None) -> MiddlewareChain:
    return MiddlewareChain(
        [
            CacheMiddleware(),
            RetryMiddleware(),
            PermissionMiddleware(),
            PIIMiddleware(),
            SensitiveActionMiddleware(services),
            TracingMiddleware(),
            AuditMiddleware(),
        ]
    )


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
