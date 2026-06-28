"""Manager-only agent config tools — read/write per-role custom instructions (L1).

These tools let the Manager create or update the custom instruction files
for Worker, Evaluator, and itself.  They are added to the Manager's tool list
only (not Worker/Evaluator).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def make_agent_config_tools(
    root: str,
    project_id: str,
    pub_event: Callable[[str, str], None] | None = None,
) -> list[Any]:
    """Return [write_agent_config, read_agent_config] Strands tool objects.

    Args:
        root: Workspace root (from settings).
        project_id: The current project ID.
        pub_event: Optional ``(kind, name) -> None`` callable.  Called with
            ``kind="agent_config"`` and the role name after a successful write
            so the caller can publish a ``SensitiveFileWrittenEvent``.
            When ``None`` (the default) no event is published.

    Returns:
        Two Strands tool objects for Manager use only.
    """
    from strands import tool

    from yukar.agents.tools.response_builder import make_error, make_success
    from yukar.storage import agent_config_repo

    @tool
    async def write_agent_config(role: str, instructions: str) -> dict[str, Any]:
        """Create or update per-role custom instructions for this project.

        Use this to give Worker or Evaluator (or yourself as Manager) specific
        guidance that should apply to all tasks in this project — for example,
        coding style conventions, testing frameworks, or domain knowledge.

        Args:
            role: Agent role to configure. Must be one of:
                ``"manager"``, ``"worker"``, ``"evaluator"``.
            instructions: Custom instructions in Markdown format.
                          These are appended to the agent's base system prompt.

        Returns:
            ``{"status": "success"|"error", "content": [...], "ok": bool, "role": role}``.
        """
        try:
            await agent_config_repo.save_agent_instructions(root, project_id, role, instructions)
        except Exception as exc:
            return make_error(str(exc), ok=False)
        if pub_event is not None:
            pub_event("agent_config", role)
        return make_success(f"Saved instructions for role {role!r}.", ok=True, role=role)

    @tool
    def read_agent_config(role: str) -> dict[str, Any]:
        """Read the current custom instructions for an agent role.

        Args:
            role: Agent role to read. Must be one of:
                ``"manager"``, ``"worker"``, ``"evaluator"``.

        Returns:
            ``{"status": "success"|"error", "content": [...], "role": role,
            "instructions": str}``.
        """
        try:
            instructions = agent_config_repo.get_agent_instructions(root, project_id, role)
        except Exception as exc:
            return make_error(str(exc), role=role, instructions="")
        return make_success(
            f"Read instructions for role {role!r}.", role=role, instructions=instructions
        )

    return [write_agent_config, read_agent_config]
