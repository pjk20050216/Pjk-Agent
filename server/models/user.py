"""用户数据模型 —— 存储注册用户的基本信息，关联会话和文档。"""
from datetime import datetime, timezone
from sqlalchemy import String, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from server.database import Base


class User(Base):
    """用户表：用户名、邮箱（均唯一）、密码哈希、头像，关联多个会话和文档。"""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # 级联删除：删除用户时自动删除其所有会话和文档
    conversations = relationship("Conversation", back_populates="user", cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="user", cascade="all, delete-orphan")
    agent_runs = relationship("AgentRun", back_populates="user", cascade="all, delete-orphan")
    harness_runs = relationship("HarnessRun", back_populates="user", cascade="all, delete-orphan")
