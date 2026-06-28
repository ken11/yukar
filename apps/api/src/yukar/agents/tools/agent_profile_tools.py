"""Manager-only agent profile tools — CRUD for named AgentProfile entries (Wave 5 BE-A).

Named profiles let the Manager create purpose-specific variants of Worker or
Evaluator (e.g. "frontend-worker", "backend-worker") and assign them to tasks
via the ``agent`` field of ``task_update``.

These tools are added to the Manager tool list only (not Worker/Evaluator).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, cast


def make_agent_profile_tools(
    root: str,
    project_id: str,
    pub_event: Callable[[str, str], None] | None = None,
) -> list[Any]:
    """Return Strands tool objects for agent profile CRUD.

    Tools returned:
      - ``list_agent_profiles``
      - ``read_agent_profile``
      - ``write_agent_profile``
      - ``delete_agent_profile``

    Args:
        root: Workspace root (from settings).
        project_id: The current project ID.
        pub_event: Optional ``(kind, name) -> None`` callable.  Called with
            ``kind="agent_profile"`` and the profile name after a successful
            ``write_agent_profile`` so the caller can publish a
            ``SensitiveFileWrittenEvent``.  When ``None`` (the default) no
            event is published.

    Returns:
        List of four Strands tool objects for Manager use only.
    """
    from strands import tool

    from yukar.agents.tools.response_builder import make_error, make_success
    from yukar.models.agent_profile import AgentProfile
    from yukar.models.project import RepoCommands
    from yukar.storage import agent_profiles_repo

    @tool
    def list_agent_profiles() -> dict[str, Any]:
        """List all named agent profiles defined for this project.

        Named profiles let you assign a specific configuration (instructions,
        skill subset, MCP subset, command allow/deny) to individual tasks via
        the ``agent`` field of ``task_update``.

        Returns:
            ``{"status": "success"|"error", "content": [...],
            "profiles": [{name, description, base_role, skills, mcp_servers,
            commands}]}``.
        """
        try:
            profiles = agent_profiles_repo.list_profiles(root, project_id)
            return make_success(
                f"Found {len(profiles)} profile(s).",
                profiles=[p.model_dump(mode="json") for p in profiles],
            )
        except Exception as exc:
            return make_error(str(exc), profiles=[])

    @tool
    def read_agent_profile(name: str) -> dict[str, Any]:
        """Read the configuration of a named agent profile.

        Args:
            name: Profile name (kebab-case, e.g. ``"frontend-worker"``).

        Returns:
            ``{"status": "success"|"error", "content": [...], <profile fields>}``.
        """
        try:
            profile = agent_profiles_repo.get_profile(root, project_id, name)
            if profile is None:
                return make_error(f"Agent profile not found: {name}")
            return make_success(f"Read profile {name!r}.", **profile.model_dump(mode="json"))
        except Exception as exc:
            return make_error(str(exc))

    @tool
    async def write_agent_profile(
        name: str,
        description: str,
        base_role: str,
        instructions: str = "",
        skills: list[str] | None = None,
        mcp_servers: list[str] | None = None,
        command_allow: list[str] | None = None,
        command_deny: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create or update a named agent profile for this project.

        Use profiles to give different Worker instances project-specific
        guidance, tool subsets, and command permissions.  For example, create
        a ``frontend-worker`` profile with frontend-relevant skills and a
        ``backend-worker`` profile with backend-specific instructions, then
        assign the right profile to each task via ``task_update(agent=...)``.

        Args:
            name: Unique kebab-case profile name, e.g. ``"frontend-worker"``.
            description: Short description so the Manager can choose the right profile.
            base_role: Must be ``"worker"`` or ``"evaluator"``.
            instructions: Extra system-prompt text appended to the role's base prompt.
                          Use Markdown. Leave empty to use only the base prompt.
            skills: List of skill names to activate for this profile.
                    Empty list (default) means all project skills are available.
            mcp_servers: List of MCP server names to activate.
                         Empty list (default) means all project MCP servers.
            command_allow: Shell command prefixes that the Worker may run
                           (e.g. ``["pytest", "npm test"]``).
                           Empty = defer to repo-level allow list.
            command_deny:  Shell command prefixes that are always blocked,
                           regardless of the allow list.

        Returns:
            ``{"status": "success"|"error", "content": [...], "ok": bool, "name": name}``.
        """
        try:
            if base_role not in {"worker", "evaluator"}:
                return make_error(
                    f"base_role must be 'worker' or 'evaluator', got {base_role!r}",
                    ok=False,
                )
            _base_role = cast(Literal["worker", "evaluator"], base_role)
            profile = AgentProfile(
                name=name,
                description=description,
                base_role=_base_role,
                instructions=instructions,
                skills=skills or [],
                mcp_servers=mcp_servers or [],
                commands=RepoCommands(
                    allow=command_allow or [],
                    deny=command_deny or [],
                ),
            )
            await agent_profiles_repo.save_profile(root, project_id, profile)
        except Exception as exc:
            return make_error(str(exc), ok=False)
        if pub_event is not None:
            pub_event("agent_profile", name)
        return make_success(f"Saved profile {name!r}.", ok=True, name=name)

    @tool
    def delete_agent_profile(name: str) -> dict[str, Any]:
        """Delete a named agent profile.

        Args:
            name: Profile name to delete.

        Returns:
            ``{"status": "success"|"error", "content": [...], "ok": bool}``.
        """
        try:
            deleted = agent_profiles_repo.delete_profile(root, project_id, name)
            if not deleted:
                return make_error(f"Agent profile not found: {name}", ok=False)
        except Exception as exc:
            return make_error(str(exc), ok=False)
        return make_success(f"Deleted profile {name!r}.", ok=True)

    return [list_agent_profiles, read_agent_profile, write_agent_profile, delete_agent_profile]
