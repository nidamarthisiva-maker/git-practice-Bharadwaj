"""Pydantic request / response models for the REST API."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    session_id: Optional[str] = Field(
        default=None, description="Conversation id. Omit to start a new conversation."
    )
    message: str = Field(min_length=1, max_length=4000)


class ToolCallInfo(BaseModel):
    name: str
    args: dict[str, Any]
    result: str


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    tool_calls: list[ToolCallInfo] = []
