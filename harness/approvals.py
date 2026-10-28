"""人工审批 —— 高风险动作（规则发布、灰度、沙箱执行、文件晋升）的放行闸门。

两种模式：
- 同进程：审批 API 调用 ``decide()`` 后通过 ``asyncio.Event`` 立即唤醒等待者；
- 跨进程/跨会话：等待者按 2 秒轮询 SQLite，支持服务重启后继续等待。
超时自动置为 expired，避免运行永久挂起。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from harness.config import ApprovalConfig, HarnessConfig, get_harness_config
from harness.contracts import (
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
    RunEventType,
    utcnow,
)
from harness.events import bus
from harness.store import HarnessStore, store as default_store
from server.utils.logger import logger


class ApprovalManager:
    def __init__(
        self,
        config: HarnessConfig | None = None,
        store: HarnessStore | None = None,
    ) -> None:
        self.config = config or get_harness_config()
        self.approval_config: ApprovalConfig = self.config.approvals
        self.store = store or default_store
        self._waiters: dict[str, asyncio.Event] = {}

    async def request(
        self,
        *,
        run_id: str,
        action: str,
        target: str = "",
        risk: RiskLevel | str = RiskLevel.HIGH,
        reason: str = "",
        payload: dict | None = None,
        task_id: str | None = None,
        requested_by: str = "harness",
        force: bool = False,
    ) -> ApprovalRequest:
        approval = ApprovalRequest(
            run_id=run_id,
            task_id=task_id,
            action=action,
            target=target,
            risk=risk,
            reason=reason,
            payload=payload or {},
            requested_by=requested_by,
            expires_at=utcnow() + timedelta(seconds=self.approval_config.ttl_seconds),
        )

        auto_approved = action in self.approval_config.auto_approve_actions
        needs_approval = (force or self.approval_config.requires_approval(action)) and not auto_approved
        if not needs_approval:
            approval.status = ApprovalStatus.APPROVED
            approval.decided_by = "policy:auto"
            approval.comment = "action not in approval-required list"
            approval.decided_at = utcnow()
            await asyncio.to_thread(self.store.save_approval, approval)
            await bus.publish(
                run_id,
                RunEventType.APPROVAL_DECIDED,
                {"approval": approval.model_dump(mode="json"), "auto": True},
            )
            return approval

        await asyncio.to_thread(self.store.save_approval, approval)
        self._waiters.setdefault(approval.id, asyncio.Event())
        await bus.publish(
            run_id,
            RunEventType.APPROVAL_REQUESTED,
            {
                "approval_id": approval.id,
                "action": action,
                "target": target,
                "risk": _risk_value(risk),
                "reason": reason,
                "task_id": task_id,
            },
        )
        logger.info(
            f"[Approval] requested {approval.id} action={action} run={run_id} risk={risk}"
        )
        return approval

    async def decide(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str,
        comment: str = "",
    ) -> ApprovalRequest | None:
        approval = await asyncio.to_thread(self.store.get_approval, approval_id)
        if approval is None:
            return None
        if approval.status != ApprovalStatus.PENDING:
            return approval
        if approval.expires_at and _aware(approval.expires_at) < utcnow():
            approval.status = ApprovalStatus.EXPIRED
            approval.comment = "expired before decision"
        else:
            approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
            approval.decided_by = decided_by
            approval.comment = comment
            approval.decided_at = utcnow()
        await asyncio.to_thread(self.store.save_approval, approval)

        event = self._waiters.get(approval_id)
        if event is not None:
            event.set()
        await bus.publish(
            approval.run_id,
            RunEventType.APPROVAL_DECIDED,
            {"approval": approval.model_dump(mode="json")},
        )
        logger.info(
            f"[Approval] {approval.id} -> {approval.status} by {decided_by}"
        )
        return approval

    async def wait(self, approval_id: str, timeout: int | None = None) -> ApprovalRequest:
        timeout = timeout or self.approval_config.ttl_seconds
        event = self._waiters.setdefault(approval_id, asyncio.Event())
        deadline = asyncio.get_running_loop().time() + timeout

        while True:
            approval = await asyncio.to_thread(self.store.get_approval, approval_id)
            if approval is None:
                raise KeyError(f"approval not found: {approval_id}")
            if approval.status != ApprovalStatus.PENDING:
                return approval
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                approval.status = ApprovalStatus.EXPIRED
                approval.comment = "approval timeout"
                approval.decided_at = utcnow()
                await asyncio.to_thread(self.store.save_approval, approval)
                return approval
            try:
                await asyncio.wait_for(event.wait(), timeout=min(2.0, remaining))
                event.clear()
            except asyncio.TimeoutError:
                continue

    def pending(self, run_id: str | None = None, user_id: int | None = None) -> list[ApprovalRequest]:
        return self.store.list_approvals(
            run_id=run_id, status=ApprovalStatus.PENDING.value, user_id=user_id
        )

    def get(self, approval_id: str) -> ApprovalRequest | None:
        return self.store.get_approval(approval_id)

    def status(self) -> dict:
        return {
            "enabled": self.approval_config.enabled,
            "ttl_seconds": self.approval_config.ttl_seconds,
            "required_actions": list(self.approval_config.required_actions),
            "auto_approve_actions": list(self.approval_config.auto_approve_actions),
        }


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _risk_value(risk: RiskLevel | str) -> str:
    return risk.value if isinstance(risk, RiskLevel) else str(risk)


approvals = ApprovalManager()
