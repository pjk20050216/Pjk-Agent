"""测试夹具：临时工作区、内存存储、离线 Harness 实例。"""
from __future__ import annotations

import copy
import tempfile
from pathlib import Path
from typing import Any

from harness.approvals import ApprovalManager
from harness.config import HarnessConfig, get_harness_config
from harness.contracts import ApprovalRequest, RunSnapshot
from harness.events import EventBus
from harness.integrations.java_bridge import JavaToolBridge
from harness.memory.manager import MemoryManager
from harness.observability.tracing import TraceRecorder
from harness.runtime import AgentHarness
from harness.sandbox.manager import SandboxManager
from harness.skills.registry import SkillRegistry

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class FakeStore:
    """与 HarnessStore 同接口的内存实现，避免测试写生产 SQLite。"""

    def __init__(self) -> None:
        self.runs: dict[str, RunSnapshot] = {}
        self.approvals: dict[str, ApprovalRequest] = {}

    def save_run(self, snapshot: RunSnapshot) -> None:
        self.runs[snapshot.id] = snapshot.model_copy(deep=True)

    def get_run(self, run_id: str) -> RunSnapshot | None:
        snapshot = self.runs.get(run_id)
        return snapshot.model_copy(deep=True) if snapshot else None

    def list_runs(self, user_id: int | None = None, limit: int = 50) -> list[RunSnapshot]:
        items = list(self.runs.values())
        if user_id is not None:
            items = [item for item in items if item.user_id == user_id]
        items.sort(key=lambda item: item.created_at, reverse=True)
        return [item.model_copy(deep=True) for item in items[:limit]]

    def delete_run(self, run_id: str) -> bool:
        return self.runs.pop(run_id, None) is not None

    def save_approval(self, approval: ApprovalRequest) -> None:
        self.approvals[approval.id] = approval.model_copy(deep=True)

    def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        approval = self.approvals.get(approval_id)
        return approval.model_copy(deep=True) if approval else None

    def list_approvals(
        self,
        run_id: str | None = None,
        status: Any = None,
        user_id: int | None = None,
        limit: int = 100,
    ) -> list[ApprovalRequest]:
        items = list(self.approvals.values())
        if run_id:
            items = [item for item in items if item.run_id == run_id]
        if status:
            status_value = status.value if hasattr(status, "value") else str(status)
            items = [item for item in items if item.status == status_value]
        if user_id is not None:
            allowed = {
                snapshot.id for snapshot in self.runs.values() if snapshot.user_id == user_id
            }
            items = [item for item in items if item.run_id in allowed]
        return [item.model_copy(deep=True) for item in items[:limit]]


def make_config(root: Path) -> HarnessConfig:
    cfg = get_harness_config().model_copy(deep=True)
    cfg.workspace_root = root / "workspace"
    cfg.skills_dir = PROJECT_ROOT / "skills"
    cfg.memory.fallback_dir = root / "memories"
    cfg.kafka.enabled = False
    cfg.kafka.buffer_dir = root / "audit"
    cfg.milvus.enabled = False
    cfg.sandbox.enabled = True
    cfg.sandbox.provider = "local"
    cfg.tracing.enabled = True
    cfg.tracing.jsonl_dir = root / "traces"
    cfg.approvals.auto_approve_actions = []
    cfg.approvals.required_actions = [
        "sandbox.exec",
        "rule.publish",
        "rule.gray_release",
        "file.promote",
        "policy.write",
        "java.tool.invoke",
    ]
    cfg.ensure_dirs()
    return cfg


def make_harness(root: Path | None = None) -> tuple[AgentHarness, FakeStore, HarnessConfig]:
    root = root or Path(tempfile.mkdtemp(prefix="harness-test-"))
    cfg = make_config(root)
    store = FakeStore()
    memory = MemoryManager(cfg)
    skills = SkillRegistry(cfg.skills_dir)
    sandbox = SandboxManager(cfg)
    approvals = ApprovalManager(cfg, store)  # type: ignore[arg-type]
    recorder = TraceRecorder(cfg)
    bus = EventBus(cfg)
    java = JavaToolBridge(cfg.java_bridge)
    harness = AgentHarness(
        cfg,
        store=store,  # type: ignore[arg-type]
        memory=memory,
        skills=skills,
        sandbox=sandbox,
        approvals=approvals,
        recorder=recorder,
        bus=bus,
        java_bridge=java,
    )
    return harness, store, cfg


async def noop_status(*args, **kwargs) -> None:
    return None


def deep_copy(value):
    return copy.deepcopy(value)
