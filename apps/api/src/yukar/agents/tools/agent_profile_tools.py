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
    from yukar.storage import agent_profiles_repo

    @tool
    def list_agent_profiles() -> dict[str, Any]:
        """List all named agent profiles defined for this project.

        Named profiles let you assign a specific configuration (instructions,
        skill subset, MCP subset) to individual tasks via the ``agent`` field
        of ``task_update``.  Command permissions are NOT part of a profile —
        they come solely from the repo-level allow/deny list.

        Returns:
            ``{"status": "success"|"error", "content": [...],
            "profiles": [{name, description, base_role, skills, mcp_servers}]}``.
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
        description: str | None = None,
        base_role: str | None = None,
        instructions: str | None = None,
        skills: list[str] | None = None,
        mcp_servers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create or update a named agent profile for this project.

        Use profiles to give different Worker instances project-specific
        guidance and tool subsets.  For example, create a ``frontend-worker``
        profile with frontend-relevant skills and a ``backend-worker`` profile
        with backend-specific instructions, then assign the right profile to
        each task via ``task_update(agent=...)``.

        A profile does NOT control which shell commands the Worker may run.
        Command permissions come solely from the repo-level allow/deny list,
        which the human configures in the repo settings — you cannot grant or
        restrict commands from here.

        PARTIAL UPDATE (read-merge): any argument you leave unset (``None``) is
        left UNCHANGED on an existing profile.  So to tweak only the
        instructions, pass ``name`` and ``instructions`` — the skills and MCP
        servers are preserved, never wiped.  To clear a list field, pass an
        explicit empty list ``[]``.

        Do NOT re-write a profile you have already created unless its
        configuration genuinely needs to change; if nothing changed the write
        is skipped and reported as ``unchanged``.

        Args:
            name: Unique kebab-case profile name, e.g. ``"frontend-worker"``.
            description: Short description so the Manager can choose the right profile.
            base_role: ``"worker"`` or ``"evaluator"``.  Required only when
                       creating a new profile; omit to keep an existing one.
            instructions: Extra system-prompt text appended to the role's base prompt.
                          Use Markdown. Empty string = base prompt only.
            skills: Skill names to activate.  ``[]`` = all project skills.
            mcp_servers: MCP server names to activate.  ``[]`` = all project MCP servers.

        Returns:
            ``{"status": "success"|"error", "content": [...], "ok": bool,
            "name": name, "unchanged": bool}``.
        """
        try:
            existing = agent_profiles_repo.get_profile(root, project_id, name)
            if existing is None and base_role is None:
                return make_error(
                    "base_role is required when creating a new profile "
                    "('worker' or 'evaluator').",
                    ok=False,
                )
            # Read-merge: provided value wins; otherwise keep existing (or the
            # model default for a brand-new profile).  None means "unchanged".
            resolved_base_role = base_role if base_role is not None else (
                existing.base_role if existing is not None else None
            )
            if resolved_base_role not in {"worker", "evaluator"}:
                return make_error(
                    f"base_role must be 'worker' or 'evaluator', got {resolved_base_role!r}",
                    ok=False,
                )
            _base_role = cast(Literal["worker", "evaluator"], resolved_base_role)

            def _pick(provided: Any, current: Any, default: Any) -> Any:
                if provided is not None:
                    return provided
                return current if existing is not None else default

            profile = AgentProfile(
                name=name,
                description=_pick(description, existing.description if existing else None, ""),
                base_role=_base_role,
                instructions=_pick(instructions, existing.instructions if existing else None, ""),
                skills=_pick(skills, existing.skills if existing else None, []),
                mcp_servers=_pick(mcp_servers, existing.mcp_servers if existing else None, []),
            )

            # Anti-churn: skip the write (and the event) when nothing changed.
            if existing is not None and profile == existing:
                return make_success(
                    f"Profile {name!r} already matches; no write performed.",
                    ok=True,
                    name=name,
                    unchanged=True,
                )

            await agent_profiles_repo.save_profile(root, project_id, profile)
        except Exception as exc:
            return make_error(str(exc), ok=False)
        if pub_event is not None:
            pub_event("agent_profile", name)
        return make_success(f"Saved profile {name!r}.", ok=True, name=name, unchanged=False)

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
