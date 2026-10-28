"""Harness 持久化模型 —— 运行快照与审批单。

运行态（任务、计划、结果、计数器）整体以 JSON 快照存储，避免字段演进时频繁迁移；
审批单独立成表，便于跨运行查询待办与统计。事件流另有 JSONL 落盘（见 harness/events.py）。
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class HarnessRun(Base):
    """一次 Harness 运行（可跨会话恢复）。"""

    __tablename__ = "harness_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    mode: Mapped[str] = mapped_column(String(16), default="auto")
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    user = relationship("User", back_populates="harness_runs")
    approvals = relationship(
        "HarnessApproval", back_populates="run", cascade="all, delete-orphan"
    )


class HarnessApproval(Base):
    """高风险动作审批单（规则发布、灰度、沙箱执行等）。"""

    __tablename__ = "harness_approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("harness_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target: Mapped[str] = mapped_column(String(512), default="")
    risk: Mapped[str] = mapped_column(String(16), default="high")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    run = relationship("HarnessRun", back_populates="approvals")
