"""Harness API 请求模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CreateRunRequest(BaseModel):
    objective: str = Field(min_length=4, max_length=4000)
    mode: Literal["auto", "interactive"] = "auto"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    comment: str = Field(default="", max_length=1000)


class FileWriteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    content: str
    comment: str = Field(default="", max_length=500)
    plane: Literal["host", "sandbox"] = "host"


class MemoryCreateRequest(BaseModel):
    text: str = Field(min_length=2, max_length=4000)
    kind: str = "experience"
    tags: list[str] = Field(default_factory=list)


class ResumeRequest(BaseModel):
    reason: str = ""
