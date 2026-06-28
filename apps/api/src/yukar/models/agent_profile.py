"""AgentProfile model — named per-purpose agent profiles (Wave 5 BE-A).

A profile lets the Manager assign a named configuration to a task instead
of using the generic role-level AgentConfig.  Multiple Worker profiles
(e.g. ``frontend-worker``, ``backend-worker``) can coexist in the same
project, each with its own instructions, skill/MCP subsets and command
allow/deny lists.

The profile is the *named* variant; AgentConfig remains the role-level
default.  When a task has ``agent=None`` the orchestrator uses the default
role config; when ``agent`` is set the named profile overlays it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from yukar.models.project import RepoCommands


class AgentProfile(BaseModel):
    """Named agent profile — stored as Markdown+YAML-frontmatter per profile."""

    name: str
    """Unique kebab-case identifier.  Used as the profile ID in tasks and paths."""

    description: str = ""
    """Human/Manager-readable description.  Used by the Manager to choose a profile."""

    base_role: Literal["worker", "evaluator"]
    """Which agent role this profile is based on."""

    instructions: str = ""
    """Extra system-prompt overlay appended to the base role's system prompt."""

    skills: list[str] = Field(default_factory=list)
    """Skill names to activate.  Empty list = use all project skills (no filtering)."""

    mcp_servers: list[str] = Field(default_factory=list)
    """MCP server names to activate.  Empty list = use all project MCP servers."""

    commands: RepoCommands = Field(default_factory=RepoCommands)
    """Profile-level run_command allow/deny.  Merged with repo-level at dispatch time."""
