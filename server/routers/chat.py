"""聊天路由 —— 处理用户发送消息并接收 AI 流式回复（SSE）。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from server.database import get_db
from server.dependencies import get_current_user
from server.models.user import User
from server.schemas.message import ChatRequest
from server.services import chat_service

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("/{conversation_id}")
async def chat(
    conversation_id: int,
    req: ChatRequest,
    db: Session = Depends(get_db),
    # 利用 dependencies 类中的 get_current_user 函数，从请求头中提取 JWT Token 并验证其有效性
    current_user: User = Depends(get_current_user),
):
    """POST /api/chat/{id} —— 在指定会话中发送消息，返回 SSE 流式响应（token 事件 + done/error 事件）。"""
    return await chat_service.stream_chat_response(
        db=db,
        conversation_id=conversation_id,
        user_id=current_user.id,
        content=req.content,
        document_ids=req.document_ids,
        enable_web_search=req.enable_web_search,
    )
