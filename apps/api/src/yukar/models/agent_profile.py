"""AgentProfile model — named per-purpose agent profiles (Wave 5 BE-A).

A profile lets the Manager assign a named configuration to a task instead
of using the generic role-level AgentConfig.  Multiple Worker profiles
(e.g. ``frontend-worker``, ``backend-worker``) can coexist in the same
project, each with its own instructions and skill/MCP subsets.  Command
permissions are NOT part of a profile — they come solely from the repo-level
allow/deny list.

The profile is the *named* variant; AgentConfig remains the role-level
default.  When a task has ``agent=None`` the orchestrator uses the default
role config; when ``agent`` is set the named profile overlays it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


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

    # NOTE: there is deliberately NO per-profile command allowlist.  Command
    # permissions come SOLELY from the repo-level allow/deny list (the human's
    # security boundary).  A profile-level allowlist could only NARROW a Worker's
    # commands — never grant — so its sole real effect was to silently lock
    # Workers out of running tests.  The lever was removed entirely; any legacy
    # ``allowed_commands`` key in an on-disk profile is ignored on load.
