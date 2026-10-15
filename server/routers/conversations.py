"""会话路由 —— 提供会话的 CRUD 接口：列表、创建、详情（含消息）、更新标题、删除。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from server.database import get_db
from server.dependencies import get_current_user
from server.models.user import User
from server.schemas.conversation import (
    ConversationCreate, ConversationUpdate,
    ConversationResponse, ConversationList,
)
from server.schemas.message import MessageResponse, ConversationDetail
from server.services import conversation_service

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.get("", response_model=ConversationList)
def list_conversations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """GET /api/conversations —— 获取当前用户的所有会话列表，按更新时间降序。"""
    convs = conversation_service.list_conversations(db, current_user.id)
    return ConversationList(
        conversations=[ConversationResponse.model_validate(c) for c in convs],
        total=len(convs),
    )


@router.post("", response_model=ConversationResponse)
def create_conversation(
    req: ConversationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """POST /api/conversations —— 创建新会话。"""
    conv = conversation_service.create_conversation(db, current_user.id, req.title)
    return ConversationResponse.model_validate(conv)


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """GET /api/conversations/{id} —— 获取会话详情（含所有消息列表）。"""
    conv = conversation_service.get_conversation(db, conversation_id, current_user.id)
    messages = conversation_service.get_messages(db, conversation_id, current_user.id)
    return ConversationDetail(
        id=conv.id,
        title=conv.title,
        messages=[MessageResponse.model_validate(m) for m in messages],
        created_at=conv.created_at,
        updated_at=conv.updated_at,
    )


@router.patch("/{conversation_id}", response_model=ConversationResponse)
def update_conversation(
    conversation_id: int,
    req: ConversationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """PATCH /api/conversations/{id} —— 更新会话标题。"""
    conv = conversation_service.update_conversation(db, conversation_id, current_user.id, req.title)
    return ConversationResponse.model_validate(conv)


@router.delete("/{conversation_id}")
def delete_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """DELETE /api/conversations/{id} —— 删除会话（级联删除其所有消息）。"""
    conversation_service.delete_conversation(db, conversation_id, current_user.id)
    return {"detail": "Conversation deleted"}
