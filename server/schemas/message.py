"""消息相关 Pydantic 模型 —— 定义发送消息请求、消息响应和会话详情的结构。"""
from datetime import datetime
from pydantic import BaseModel


class MessageResponse(BaseModel):
    """单条消息响应：包含角色（user/assistant）、内容、token 数。"""
    id: int
    conversation_id: int
    role: str
    content: str
    token_count: int | None = 0
    created_at: datetime

    class Config:
        from_attributes = True


class ChatRequest(BaseModel):
    """发送消息请求体：消息文本 + 可选的要附加到 RAG 上下文的文档 ID 列表 + 是否启用联网搜索。"""
    content: str
    document_ids: list[int] | None = None
    enable_web_search: bool | None = None


class ConversationDetail(BaseModel):
    """会话详情响应（含完整消息历史）：用于加载某个对话时一次性返回全部消息。"""
    id: int
    title: str
    messages: list[MessageResponse]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
