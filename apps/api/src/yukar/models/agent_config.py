"""Agent config model — per-role instruction overlay (L1)."""

from __future__ import annotations

from pydantic import BaseModel

from yukar.models.roles import ConfigurableAgentRole


class AgentConfig(BaseModel):
    """Per-role custom instruction for Manager / Worker / Evaluator."""

    role: ConfigurableAgentRole
    instructions: str = ""
