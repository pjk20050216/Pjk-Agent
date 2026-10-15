"""Tool 执行结果的标准结构。"""
from typing import Any
from pydantic import BaseModel


class ToolResult(BaseModel):
    success: bool
    data: Any = None
    error: str | None = None
    metadata: dict = {}
