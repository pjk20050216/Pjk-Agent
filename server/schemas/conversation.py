"""会话相关 Pydantic 模型 —— 定义会话创建/更新请求和列表/详情响应结构。"""
from datetime import datetime
from pydantic import BaseModel


class ConversationCreate(BaseModel):
    """创建会话请求体：标题默认为 "New Chat"。"""
    title: str = "New Chat"


class ConversationUpdate(BaseModel):
    """更新会话请求体：修改标题。"""
    title: str


class ConversationResponse(BaseModel):
    """会话基础信息响应：id、标题、创建/更新时间。"""
    id: int
    title: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ConversationList(BaseModel):
    """会话列表响应：包含所有会话的数组和总数。"""
    conversations: list[ConversationResponse]
    total: int
