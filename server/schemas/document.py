"""文档相关 Pydantic 模型 —— 定义文档列表和文档详情的响应结构。"""
from datetime import datetime
from pydantic import BaseModel


class DocumentResponse(BaseModel):
    """文档信息响应：文件名、类型、大小、RAG 索引状态、分块数等。"""
    id: int
    original_filename: str
    file_type: str
    file_size: int
    status: str
    chunk_count: int
    created_at: datetime

    class Config:
        from_attributes = True


class DocumentList(BaseModel):
    """文档列表响应。"""
    documents: list[DocumentResponse]
