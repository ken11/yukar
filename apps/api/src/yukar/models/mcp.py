"""MCP server config models — project-level MCP settings (L3)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class McpServerConfig(BaseModel):
    """Configuration for a single MCP server."""

    name: str
    type: Literal["stdio", "sse"]
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    allowed_tools: list[str] | None = None
    rejected_tools: list[str] | None = None


class McpConfig(BaseModel):
    """Root MCP config — list of server configurations."""

    servers: list[McpServerConfig] = Field(default_factory=list)
