"""Harness 领域契约 —— 运行时各层共享的数据结构。

这些模型是「跨层协议」：任务规划、虚拟文件系统、子 Agent 委派、审批、
记忆、链路追踪都围绕它们交换数据，避免各模块各自定义 dict 造成隐式契约。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str = "") -> str:
    """生成短 ID；prefix 用于可读性（如 ``run_``、``appr_``）。"""
    raw = uuid.uuid4().hex[:16]
    return f"{prefix}{raw}" if prefix else raw


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


TERMINAL_TASK_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.BLOCKED,
    TaskStatus.SKIPPED,
}


class RunStatus(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


RISK_ORDER = {
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class FilePlane(str, Enum):
    """双层文件访问平面：host = 虚拟文件系统；sandbox = 隔离沙箱工作区。"""

    HOST = "host"
    SANDBOX = "sandbox"


class HookPoint(str, Enum):
    RUN_START = "run_start"
    RUN_END = "run_end"
    BEFORE_MODEL = "before_model"
    AFTER_MODEL = "after_model"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    BEFORE_FILE_READ = "before_file_read"
    AFTER_FILE_READ = "after_file_read"
    BEFORE_FILE_WRITE = "before_file_write"
    AFTER_FILE_WRITE = "after_file_write"
    BEFORE_TASK_STATE = "before_task_state"
    AFTER_TASK_STATE = "after_task_state"
    BEFORE_DELEGATE = "before_delegate"
    AFTER_DELEGATE = "after_delegate"
    BEFORE_APPROVAL = "before_approval"
    AFTER_APPROVAL = "after_approval"
    ON_ERROR = "on_error"


class RunEventType(str, Enum):
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_WAITING_APPROVAL = "run.waiting_approval"
    RUN_RESUMED = "run.resumed"
    PLAN_CREATED = "plan.created"
    TASK_UPDATED = "task.updated"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    AGENT_DELEGATED = "agent.delegated"
    AGENT_COMPLETED = "agent.completed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    FILE_READ = "file.read"
    FILE_WRITTEN = "file.written"
    FILE_EDITED = "file.edited"
    FILE_PROMOTED = "file.promoted"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    CONTEXT_COMPACTED = "context.compacted"
    MEMORY_RECALLED = "memory.recalled"
    MEMORY_DISTILLED = "memory.distilled"
    SANDBOX_STARTED = "sandbox.started"
    SANDBOX_EXEC = "sandbox.exec"
    SANDBOX_STOPPED = "sandbox.stopped"
    AUDIT = "audit"
    ERROR = "error"


class TaskItem(BaseModel):
    """任务清单中的一项，状态由运行时持续维护。"""

    model_config = ConfigDict(use_enum_values=True, validate_assignment=True)

    id: str = Field(default_factory=lambda: new_id("task_"))
    seq: int = 0
    title: str
    description: str = ""
    agent: str = "feature_analysis"
    depends_on: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 50
    risk: RiskLevel = RiskLevel.LOW
    inputs: dict[str, Any] = Field(default_factory=dict)
    acceptance: list[str] = Field(default_factory=list)
    attempts: int = 0
    max_attempts: int = 2
    result_summary: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return TaskStatus(self.status) in TERMINAL_TASK_STATUSES


class TaskPlan(BaseModel):
    """主 Agent 生成的执行计划（任务清单 + 编排策略）。"""

    model_config = ConfigDict(use_enum_values=True)

    objective: str
    strategy: str = ""
    tasks: list[TaskItem] = Field(default_factory=list)
    revision: int = 1
    created_at: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def by_id(self, task_id: str) -> TaskItem | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def pending_tasks(self) -> list[TaskItem]:
        return [t for t in self.tasks if not t.is_terminal]


class ArtifactRef(BaseModel):
    """VFS 中可继续操作的工作资产引用。"""

    path: str
    kind: str = "report"          # report | log | policy | approval | note | script
    summary: str = ""
    size: int = 0
    sha256: str = ""
    created_by: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class ToolCallRecord(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    success: bool = True
    duration_ms: int = 0
    risk: RiskLevel = RiskLevel.LOW
    output_preview: str = ""
    error: str | None = None


class SubAgentResult(BaseModel):
    """子 Agent 回传主 Agent 的结构化结论（只保留摘要，详细内容在 VFS）。"""

    model_config = ConfigDict(use_enum_values=True)

    task_id: str
    agent: str
    success: bool = True
    summary: str = ""
    findings: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    """高风险动作的人工审批单。"""

    model_config = ConfigDict(use_enum_values=True, validate_assignment=True)

    id: str = Field(default_factory=lambda: new_id("appr_"))
    run_id: str
    task_id: str | None = None
    action: str
    target: str = ""
    risk: RiskLevel = RiskLevel.HIGH
    reason: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_by: str = "harness"
    decided_by: str | None = None
    comment: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    decided_at: datetime | None = None
    expires_at: datetime | None = None


class RunSnapshot(BaseModel):
    """一次 Harness 运行的完整可恢复快照。"""

    model_config = ConfigDict(use_enum_values=True, validate_assignment=True)

    id: str = Field(default_factory=lambda: new_id("run_"))
    user_id: int | None = None
    objective: str
    status: RunStatus = RunStatus.QUEUED
    mode: str = "auto"                    # auto | interactive
    plan: TaskPlan | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    context_summary: str = ""
    approvals: list[ApprovalRequest] = Field(default_factory=list)
    counters: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunEvent(BaseModel):
    """SSE / 审计 / 回放共用的运行事件。"""

    seq: int = 0
    run_id: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=utcnow)
    trace_id: str = ""
    span_id: str = ""


class TraceSpan(BaseModel):
    trace_id: str
    span_id: str = Field(default_factory=lambda: new_id("span_"))
    parent_span_id: str | None = None
    name: str
    kind: str = "internal"                # run | agent | tool | model | file | approval | memory
    status: str = "ok"                    # ok | error | cancelled
    start_ts: datetime = Field(default_factory=utcnow)
    end_ts: datetime | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)

    def finish(self, status: str = "ok", **attributes: Any) -> "TraceSpan":
        self.end_ts = utcnow()
        self.status = status
        self.attributes.update(attributes)
        return self


class FileEntry(BaseModel):
    path: str
    kind: str = "file"                    # file | dir
    size: int = 0
    sha256: str = ""
    version: int = 0
    author: str = ""
    updated_at: datetime = Field(default_factory=utcnow)
    plane: FilePlane = FilePlane.HOST
    preview: str = ""


class FileVersion(BaseModel):
    path: str
    version: int
    sha256: str
    size: int
    author: str = ""
    comment: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class SearchHit(BaseModel):
    path: str
    line_no: int
    line: str
    match: str = ""


class MemoryRecord(BaseModel):
    """长期记忆条目：经验、模板、偏好、历史案例。"""

    id: str = Field(default_factory=lambda: new_id("mem_"))
    kind: str = "experience"              # experience | template | preference | case
    text: str
    tags: list[str] = Field(default_factory=list)
    run_id: str | None = None
    user_id: int | None = None
    score: float = 0.0
    created_at: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolSpec(BaseModel):
    """对外暴露的工具描述（含风险等级与审批要求）。"""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    risk: RiskLevel = RiskLevel.LOW
    action: str = "tool.invoke"           # 审批动作标识，如 rule.publish
    source: str = "builtin"               # builtin | java | sandbox
    requires_approval: bool = False
    tags: list[str] = Field(default_factory=list)


class SandboxExecResult(BaseModel):
    success: bool
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    truncated: bool = False
    sandbox_id: str = ""
    provider: str = ""
    error: str | None = None


# 虚拟文件系统允许的顶层目录（其余路径一律拒绝）
ALLOWED_VFS_ROOTS = (
    "/workspace",
    "/reports",
    "/logs",
    "/policies",
    "/skills",
    "/memories",
    "/sandbox",
)

READONLY_VFS_ROOTS = ("/skills",)
