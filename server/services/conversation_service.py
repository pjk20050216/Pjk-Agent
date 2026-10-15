"""
会话业务逻辑 —— 会话与消息的 CRUD 操作，包含所有权校验以防止跨用户访问。
负责对话的事件储存和数据访问的安全性
"""
from sqlalchemy.orm import Session
from sqlalchemy import desc
from fastapi import HTTPException, status
from server.models.conversation import Conversation
from server.models.message import Message


def list_conversations(db: Session, user_id: int) -> list[Conversation]:
    """获取当前用户的所有会话，按更新时间降序排列。"""
    return (
        db.query(Conversation)
        .filter(Conversation.user_id == user_id)
        .order_by(desc(Conversation.updated_at))
        .all()
    )


def create_conversation(db: Session, user_id: int, title: str = "New Chat") -> Conversation:
    """创建新会话，默认标题为 "New Chat"。"""
    conv = Conversation(user_id=user_id, title=title)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def get_conversation(db: Session, conversation_id: int, user_id: int) -> Conversation:
    """按 ID 获取会话，同时校验所有权（非所有者返回 404）。"""
    conv = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == user_id,
    ).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


def update_conversation(db: Session, conversation_id: int, user_id: int, title: str) -> Conversation:
    """更新会话标题，校验所有权。"""
    conv = get_conversation(db, conversation_id, user_id)
    conv.title = title
    db.commit()
    db.refresh(conv)
    return conv


def delete_conversation(db: Session, conversation_id: int, user_id: int) -> None:
    """删除会话，级联删除其所有消息。"""
    conv = get_conversation(db, conversation_id, user_id)
    db.delete(conv)
    db.commit()


def get_messages(db: Session, conversation_id: int, user_id: int) -> list[Message]:
    """获取会话的所有消息，按创建时间升序排列（构建聊天历史时使用）。"""
    get_conversation(db, conversation_id, user_id)
    return (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
        .all()
    )


def save_message(db: Session, conversation_id: int, role: str, content: str, token_count: int = 0) -> Message:
    """保存一条消息到数据库（user 或 assistant 角色）。"""
    msg = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        token_count=token_count,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg
