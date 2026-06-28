"""Manager-only tools for creating/reading skills and MCP servers (Wave 5 BE-A).

These tools let the Manager populate project-level skills (L2) and MCP server
configurations (L3) directly from within a run, without requiring the user to
set them up manually via the API.

Added to the Manager tool list only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yukar.config.settings import McpSettings


def make_skill_mcp_tools(
    root: str,
    project_id: str,
    mcp_settings: McpSettings | None = None,
    pub_event: Callable[[str, str], None] | None = None,
) -> list[Any]:
    """Return Strands tool objects for skill and MCP management.

    Tools returned:
      - ``list_skills``
      - ``read_skill``
      - ``write_skill``
      - ``write_mcp_server``

    Args:
        root: Workspace root (from settings).
        project_id: The current project ID.
        mcp_settings: Optional :class:`~yukar.config.settings.McpSettings`
            instance.  When provided, ``write_mcp_server`` enforces the
            allowlist policy.  When ``None`` (legacy / test default), the tool
            behaves as before: all registrations are allowed.
        pub_event: Optional ``(kind, name) -> None`` callable.  Called with
            ``kind="skill"`` and the skill name after a successful ``write_skill``
            so the caller can publish a ``SensitiveFileWrittenEvent``.
            When ``None`` (the default) no event is published.

    Returns:
        List of four Strands tool objects for Manager use only.
    """
    from strands import tool

    from yukar.agents.mcp_manager import _sse_host_allowed, _stdio_command_allowed
    from yukar.agents.tools.response_builder import make_error, make_success
    from yukar.models.mcp import McpConfig, McpServerConfig
    from yukar.storage import mcp_repo, skills_repo

    @tool
    def list_skills() -> dict[str, Any]:
        """List all skills defined for this project.

        Skills are reusable instruction sets that can be activated for specific
        agent profiles (or all agents by default).

        Returns:
            ``{"status": "success"|"error", "content": [...], "skills": [{name,
            description}]}``.
        """
        try:
            skill_metas = skills_repo.list_skills(root, project_id)
            return make_success(
                f"Found {len(skill_metas)} skill(s).",
                skills=[s.model_dump(mode="json") for s in skill_metas],
            )
        except Exception as exc:
            return make_error(str(exc), skills=[])

    @tool
    def read_skill(name: str) -> dict[str, Any]:
        """Read the full content of a project skill.

        Args:
            name: Skill name (directory name under the project's skills/ folder).

        Returns:
            ``{"status": "success", "content": [{"text": <markdown>}],
            "name": ..., "description": ...}``
            or ``{"status": "error", "content": [...], "error": ...}`` if not found.

            The markdown body is in ``result["content"][0]["text"]``.
            Structural metadata (``name``, ``description``) are spread as
            additional top-level keys so the LLM can reference them directly.
        """
        try:
            skill = skills_repo.get_skill(root, project_id, name)
            # Use make_success so that translator._handle_tool_result_block receives
            # content as list[dict] (not a raw str), and result_text is populated
            # correctly.  The markdown body goes into text; metadata is spread.
            return make_success(
                skill.content,
                name=skill.name,
                description=skill.description,
            )
        except FileNotFoundError:
            return make_error(f"Skill not found: {name}")
        except Exception as exc:
            return make_error(str(exc))

    @tool
    async def write_skill(name: str, content: str) -> dict[str, Any]:
        """Create or replace a project skill.

        A skill is a SKILL.md file containing instructions or context that
        Worker/Evaluator agents can use.  The content may include a YAML
        frontmatter block with ``name`` and ``description`` keys.

        Example content::

            ---
            name: pytest-patterns
            description: Project-specific pytest conventions
            ---
            # Pytest Patterns

            Always use `pytest.mark.asyncio` for async tests.

        Args:
            name: Unique skill name (used as the directory name).
            content: Full SKILL.md content (Markdown, optionally with frontmatter).

        Returns:
            ``{"status": "success"|"error", "content": [...], "ok": bool, "name": name}``.
        """
        try:
            await skills_repo.save_skill(root, project_id, name, content)
        except Exception as exc:
            return make_error(str(exc), ok=False)
        if pub_event is not None:
            pub_event("skill", name)
        return make_success(f"Saved skill {name!r}.", ok=True, name=name)

    @tool
    async def write_mcp_server(
        name: str,
        server_type: str,
        url: str = "",
        command: str = "",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        allowed_tools: list[str] | None = None,
        rejected_tools: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add or update an MCP server configuration for this project.

        MCP servers provide additional tools to agents at runtime.  The server
        configuration is merged into the existing ``mcp.yaml``; existing servers
        with different names are preserved.

        Use ``${VAR}`` syntax in ``url`` or ``env`` values — they are expanded at
        runtime from environment variables and are never stored in plaintext.

        Args:
            name: Unique server name (e.g. ``"github-mcp"``).
            server_type: ``"stdio"`` for subprocess-based servers,
                         ``"sse"`` for HTTP SSE servers.
            url: Base URL for SSE servers.
            command: Executable for stdio servers (e.g. ``"npx"``).
            args: Argument list for stdio servers.
            env: Environment variables to pass to the server process.
                 Use ``${VAR}`` for secrets.
            allowed_tools: Whitelist of tool names exposed from this server.
                           ``None`` (default) = expose all tools.
            rejected_tools: Blacklist of tool names to suppress.

        Returns:
            ``{"status": "success"|"error", "content": [...], "ok": bool, "name": name}``.
        """
        try:
            # ------------------------------------------------------------------
            # Layer 0: Input safety (B1 hardening).
            # ------------------------------------------------------------------
            # Reject server names containing path separators — prevents a
            # compromised Manager from crafting a name that escapes the config
            # directory when written to disk.
            if "/" in name or "\\" in name:
                return make_error(
                    f"MCP server name {name!r} must not contain path separators",
                    ok=False,
                )
            # Reject env keys that name a high-value credential (aligns with the
            # subprocess env-scrub intent: the same secret names that are stripped
            # from child processes must not be injected into MCP stdio children
            # via the agent-controlled cfg.env dict).
            #
            # Also reject env VALUES whose ${VAR} references name a forbidden
            # variable — e.g. env={"INNOCENT": "${AWS_SECRET_ACCESS_KEY}"} would
            # be expanded at launch time, bypassing the key-name guard above.
            if env:
                import re as _re

                from yukar.config.settings import _is_forbidden_api_key_env

                bad_keys = [k for k in env if _is_forbidden_api_key_env(k)]
                if bad_keys:
                    return make_error(
                        f"MCP server env contains forbidden credential key(s): {bad_keys!r}; "
                        "use ${VAR} substitution for secrets and avoid high-value credential names",
                        ok=False,
                    )
                # Check values for ${FORBIDDEN_VAR} references.
                _VAR_REF_RE = _re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
                bad_value_refs: list[str] = []
                for k, v in env.items():
                    for var_name in _VAR_REF_RE.findall(v):
                        if _is_forbidden_api_key_env(var_name):
                            bad_value_refs.append(f"{k}=${{{var_name}}}")
                if bad_value_refs:
                    return make_error(
                        f"MCP server env contains ${'{'}...{'}'} reference(s) to forbidden "
                        f"credential variable(s): {bad_value_refs!r}; "
                        "do not forward high-value credentials into MCP child processes",
                        ok=False,
                    )
            # Reject stdio command containing path separators (B1 — basename-only check bypass).
            # A command like "/attacker/path/npx" passes the basename allowlist ("npx")
            # but executes an untrusted binary.  Require the operator to configure the
            # absolute path in allowed_stdio_commands explicitly instead.
            if server_type == "stdio" and command and ("/" in command or "\\" in command):
                return make_error(
                    f"stdio command {command!r} must not contain path separators; "
                    "use the bare command name (e.g. 'npx') and ensure it is in "
                    "settings.mcp.allowed_stdio_commands",
                    ok=False,
                )
            # ------------------------------------------------------------------
            # Layer 1: Registration gate (policy enforcement).
            # ------------------------------------------------------------------
            if mcp_settings is not None:
                if not mcp_settings.allow_agent_registration:
                    return make_error(
                        "MCP server registration by agents is disabled by policy "
                        "(settings.mcp.allow_agent_registration=false)",
                        ok=False,
                    )
                # allow_agent_registration=True — still check allowlists.
                # An empty allowlist means "nothing explicitly permitted" → reject.
                if server_type == "sse":
                    if not mcp_settings.allowed_sse_hosts:
                        return make_error(
                            "MCP SSE server registration requires"
                            " settings.mcp.allowed_sse_hosts to be non-empty",
                            ok=False,
                        )
                    if not _sse_host_allowed(url, mcp_settings.allowed_sse_hosts):
                        from urllib.parse import urlparse

                        parsed_host = urlparse(url).hostname
                        if parsed_host is None:
                            return make_error(
                                f"SSE url {url!r} has no parseable host — include a scheme "
                                "(e.g. https://example.com/sse)",
                                ok=False,
                            )
                        return make_error(
                            f"SSE host {parsed_host!r} is not in settings.mcp.allowed_sse_hosts",
                            ok=False,
                        )
                else:  # stdio
                    if not mcp_settings.allowed_stdio_commands:
                        return make_error(
                            "MCP stdio server registration requires "
                            "settings.mcp.allowed_stdio_commands to be non-empty",
                            ok=False,
                        )
                    if not _stdio_command_allowed(command, mcp_settings.allowed_stdio_commands):
                        from pathlib import Path

                        basename = Path(command).name if command else ""
                        return make_error(
                            f"stdio command {basename!r} is not in "
                            "settings.mcp.allowed_stdio_commands",
                            ok=False,
                        )

            from typing import Literal

            _type: Literal["stdio", "sse"] = "stdio" if server_type == "stdio" else "sse"
            new_server = McpServerConfig(
                name=name,
                type=_type,
                url=url or None,
                command=command or None,
                args=args or [],
                env=env or {},
                allowed_tools=allowed_tools if allowed_tools else None,
                rejected_tools=rejected_tools if rejected_tools else None,
            )
            # Upsert: load existing config, replace or append.
            cfg = mcp_repo.get_mcp_config(root, project_id)
            servers = [s for s in cfg.servers if s.name != name]
            servers.append(new_server)
            updated_cfg = McpConfig(servers=servers)
            await mcp_repo.save_mcp_config(root, project_id, updated_cfg)
        except Exception as exc:
            return make_error(str(exc), ok=False)
        return make_success(f"Saved MCP server {name!r}.", ok=True, name=name)

    return [list_skills, read_skill, write_skill, write_mcp_server]
