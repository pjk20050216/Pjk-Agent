"""数据库模块 —— 创建 SQLAlchemy 引擎和会话工厂，提供数据库初始化与 FastAPI 依赖注入。"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from server.config import settings

# SQLite 数据库引擎（check_same_thread=False 以支持 FastAPI 多线程访问）
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    echo=False,
)

# 数据库会话工厂，每个请求获取一个独立的会话实例
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """SQLAlchemy ORM 模型的基类。"""
    pass


def init_db():
    """应用启动时自动创建所有数据库表（如果不存在）。"""
    from server.models import User, Conversation, Message, Document, AgentRun  # noqa: F401
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI 依赖注入：为每个请求提供数据库会话，请求结束后自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
