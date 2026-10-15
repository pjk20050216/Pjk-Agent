"""Agent 相关 Pydantic Schema —— 定义 Agent 运行请求、响应、回复等结构。"""
from datetime import datetime
from pydantic import BaseModel


class AgentRunRequest(BaseModel):
    request: str
    conversation_id: int | None = None
    document_ids: list[int] | None = None


class AgentReplyRequest(BaseModel):
    reply: str


class AgentToolInfo(BaseModel):
    name: str
    description: str
    parameters: dict


class AgentToolList(BaseModel):
    tools: list[AgentToolInfo]


class AgentRunResponse(BaseModel):
    id: int
    user_id: int
    conversation_id: int | None
    request: str
    plan: dict | None
    tool_results: dict | None
    final_output: str | None
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AgentRunList(BaseModel):
    runs: list[AgentRunResponse]
    total: int
