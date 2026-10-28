"""工具运行上下文 —— 把 run 级资源注入工具实现。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from harness.approvals import ApprovalManager
from harness.contracts import TaskPlan
from harness.memory.manager import MemoryManager
from harness.sandbox.manager import SandboxManager
from harness.skills.registry import SkillRegistry
from harness.vfs.store import VirtualFileSystem


@dataclass
class ToolContext:
    run_id: str
    user_id: int | None
    vfs: VirtualFileSystem
    memory: MemoryManager
    sandbox: SandboxManager
    skills: SkillRegistry
    approvals: ApprovalManager
    java_bridge: Any = None
    plan_provider: Callable[[], TaskPlan | None] | None = None
    extra: dict[str, Any] = field(default_factory=dict)
