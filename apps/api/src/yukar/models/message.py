"""Message model — Strands-compatible session file format.

Format: sessions/session_{epic-id}/agents/agent_{id}/messages/message_<N>.json
{
    "message": {"role": "user"|"assistant", "content": [{"text": "..."}]},
    "message_id": 0,
    "created_at": "2026-06-12T00:00:00Z"
}

Strands persists three kinds of content parts:
  text:        {"text": "..."}
  toolUse:     {"toolUse": {"toolUseId": "...", "name": "...", "input": {...}}}
  toolResult:  {"toolResult": {"toolUseId": "...", "status": "...",
                               "content": [{"text": "..."}]}}

ContentPart handles all three.  Raw camelCase keys (toolUse/toolResult/
toolUseId) are read via field aliases.  Over HTTP, FastAPI serialises response
models with ``by_alias=True`` (its default), so the wire format is **camelCase**
(``toolUse``/``toolResult``/``toolUseId``) — this is what ``packages/api-types``
generates and what the frontend consumes.  ``model_dump()`` without
``by_alias=True`` yields the snake_case field names and is not the wire format.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolUseBlock(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tool_use_id: str = Field(alias="toolUseId")
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tool_use_id: str = Field(alias="toolUseId")
    status: str | None = None
    text: str | None = None  # content[].text flattened into a single string

    @model_validator(mode="before")
    @classmethod
    def _flatten_content(cls, data: Any) -> Any:
        """Raw Strands toolResult has content: [{"text": "..."}]. Flatten it into text."""
        if isinstance(data, dict) and "text" not in data and isinstance(data.get("content"), list):
            parts = [c.get("text", "") for c in data["content"] if isinstance(c, dict)]
            data = {**data, "text": "".join(parts)}
        return data


class ContentPart(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    text: str | None = None
    tool_use: ToolUseBlock | None = Field(default=None, alias="toolUse")
    tool_result: ToolResultBlock | None = Field(default=None, alias="toolResult")


class MessagePayload(BaseModel):
    role: Literal["user", "assistant"]
    content: list[ContentPart]


class Message(BaseModel):
    message: MessagePayload
    message_id: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
