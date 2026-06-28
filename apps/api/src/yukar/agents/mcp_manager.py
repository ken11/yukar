"""MCP client manager — Wave 4a (L3 MCP).

Manages the lifecycle of MCP client connections configured per project.
Designed for yukar's single-event-loop / workers=1 constraint.

Design notes:
- start/stop wrap synchronous blocking calls in asyncio.to_thread
  (C extensions / stdio/sse connections).
- Each server failure is logged as a warning and skipped; built-in tools remain
  available.
- The manager is owned exclusively by EpicOrchestrator (created once per run,
  stopped in the run's finally block — equivalent to FileSessionManager ownership).
- MCP tools are intentionally outside path_guard scope because MCP servers are
  external processes whose filesystem access cannot be controlled by yukar's
  sandbox. Users who configure MCP servers accept this responsibility. This is
  noted here for auditability.
- Env (secrets): stdio child processes receive a scrubbed env via
  build_subprocess_env — host secrets (ANTHROPIC_API_KEY / AWS_* / GITHUB_TOKEN /
  SSH_AUTH_SOCK etc.) are NOT inherited by default.  Operators who need a variable
  in the MCP child must list it explicitly in cfg.env (${VAR} expansion is
  supported); that is the only sanctioned egress path for credentials.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from yukar.models.mcp import McpServerConfig
from yukar.sandbox.env import build_subprocess_env

# MCP / Strands imports — available because mcp is a declared dependency.
# Imported at module level so tests can monkeypatch them on this module.
try:
    from mcp import StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    from strands.tools.mcp import MCPClient

    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False
    # Typed as Any so the rest of the module can reference these names at
    # runtime without NameError; the _MCP_AVAILABLE guard ensures they are
    # only called when the real imports succeeded.
    StdioServerParameters: Any = None
    sse_client: Any = None
    stdio_client: Any = None
    MCPClient: Any = None

logger = logging.getLogger(__name__)


def _expand_env_vars(value: str) -> str:
    """Expand ``${VAR}`` references in *value* with os.environ.

    - Undefined variables are replaced with '' and a warning is logged.
    - Literal text without ``${...}`` is returned unchanged.
    """

    def _replacer(match: re.Match[str]) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            logger.warning(
                "MCP config: environment variable '%s' is not set; substituting empty string",
                var_name,
            )
            return ""
        return env_value

    return re.sub(r"\$\{([^}]+)}", _replacer, value)


def _expand_server_env(cfg: McpServerConfig) -> dict[str, str]:
    """Return the env dict with ${VAR} expanded from os.environ."""
    return {k: _expand_env_vars(v) for k, v in cfg.env.items()}


# ---------------------------------------------------------------------------
# Shared policy helpers — used by write_mcp_server (registration gate) and
# McpClientManager._start (connection gate).
# ---------------------------------------------------------------------------


def _sse_host_allowed(url: str, allowed_hosts: list[str]) -> bool:
    """Return True if the SSE server URL's hostname is in *allowed_hosts*.

    If *allowed_hosts* is empty the behaviour depends on call-site semantics:
    - Registration gate (allow_agent_registration=True, empty list): the caller
      should reject — empty means "nothing explicitly permitted yet".
    - Connection gate (McpClientManager._start, empty list): the caller should
      allow — empty means "no restriction".

    This function only checks whether the hostname appears in the list; the
    caller decides what to do when the list is empty.
    """
    # urlparse(...).hostname is already lowercased; casefold the allow list too
    # so an operator entry like "Example.com" matches "http://example.com".
    hostname = (urlparse(url).hostname or "").casefold()
    return hostname in {h.casefold() for h in allowed_hosts}


def _stdio_command_allowed(command: str, allowed_commands: list[str]) -> bool:
    """Return True if *command*'s basename is in *allowed_commands*.

    Same empty-list semantics as :func:`_sse_host_allowed`.
    """
    basename = Path(command).name
    return basename in allowed_commands


class McpClientManager:
    """Manages multiple MCP client connections for a single project run.

    Usage pattern (EpicOrchestrator):

        mgr = McpClientManager(config.servers)
        await mgr.start_async()
        tools = await mgr.get_tools_async()
        tools_by_server = await mgr.get_tools_by_server_async()
        # ... pass tools to agents ...
        # in finally:
        await mgr.stop_async()

    Thread safety: designed for single-event-loop usage (workers=1).
    All async methods wrap the synchronous _start/_get_tools/_stop in
    asyncio.to_thread (upper-bounded by asyncio default pool limits).
    """

    def __init__(
        self,
        configs: list[McpServerConfig],
        mcp_settings: Any | None = None,
    ) -> None:
        self.configs = configs
        # McpSettings instance (optional).  When None, connection-time filtering
        # is disabled (empty-allowlist = no restriction — preserves legacy behaviour).
        self._mcp_settings = mcp_settings
        # Each entry: (server_name, client).  Parallel lists to _clients so we
        # can look up which client belongs to which server name.
        self._clients: list[Any] = []
        self._client_names: list[str] = []
        self._started: bool = False

    # ------------------------------------------------------------------
    # Internal tool filters helper (ported from tokuye)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_tool_filters(cfg: McpServerConfig) -> dict[str, list[str]] | None:
        """Build tool_filters dict for MCPClient from allowed/rejected lists.

        Returns None when no filtering is configured (all tools allowed).
        """
        if not cfg.allowed_tools and not cfg.rejected_tools:
            return None
        filters: dict[str, list[str]] = {}
        if cfg.allowed_tools:
            filters["allowed"] = list(cfg.allowed_tools)
        if cfg.rejected_tools:
            filters["rejected"] = list(cfg.rejected_tools)
        return filters

    # ------------------------------------------------------------------
    # Synchronous start (called via to_thread)
    # ------------------------------------------------------------------

    def _start(self) -> None:
        """Start all configured MCP client connections (synchronous).

        Failures per server are logged as warnings and skipped.
        """
        if self._started:
            logger.warning("McpClientManager: already started, skipping")
            return

        if not self.configs:
            logger.debug("McpClientManager: no MCP servers configured")
            self._started = True
            return

        if not _MCP_AVAILABLE:
            logger.warning("McpClientManager: MCP dependencies not importable, skipping MCP setup")
            self._started = True
            return

        # Resolve allowlists from mcp_settings (None → empty → no restriction).
        _allowed_sse: list[str] = (
            list(self._mcp_settings.allowed_sse_hosts) if self._mcp_settings is not None else []
        )
        _allowed_stdio: list[str] = (
            list(self._mcp_settings.allowed_stdio_commands)
            if self._mcp_settings is not None
            else []
        )
        # Strict mode (enforce_connection_allowlist=True): empty allowlist → fail-closed.
        # Defaults to False for full backward compatibility.
        _strict: bool = (
            bool(self._mcp_settings.enforce_connection_allowlist)
            if self._mcp_settings is not None
            else False
        )

        for cfg in self.configs:
            # ------------------------------------------------------------------
            # Connection-time policy (Layer 2 of the MCP allowlist).
            #
            # Default behaviour (strict=False):
            #   Non-empty allowlist → must match.
            #   Empty allowlist → no restriction (user-configured MCP works
            #   without any settings changes).
            #
            # Strict mode (strict=True / enforce_connection_allowlist=True):
            #   Empty allowlist → fail-closed (all servers of that type rejected).
            #   Non-empty allowlist → must match (same as default non-empty).
            # ------------------------------------------------------------------
            if cfg.type == "sse":
                if _strict and not _allowed_sse:
                    logger.warning(
                        "McpClientManager: strict mode rejects server %r (empty allowed_sse_hosts)",
                        cfg.name,
                    )
                    continue
                if _allowed_sse:
                    url_for_check = cfg.url or ""
                    if not _sse_host_allowed(url_for_check, _allowed_sse):
                        logger.warning(
                            "McpClientManager: server %r (sse) host %r"
                            " not in allowed_sse_hosts, skipping",
                            cfg.name,
                            url_for_check,
                        )
                        continue
            elif cfg.type == "stdio":
                if _strict and not _allowed_stdio:
                    logger.warning(
                        "McpClientManager: strict mode rejects server %r"
                        " (empty allowed_stdio_commands)",
                        cfg.name,
                    )
                    continue
                if _allowed_stdio:
                    cmd_for_check = cfg.command or ""
                    if not _stdio_command_allowed(cmd_for_check, _allowed_stdio):
                        logger.warning(
                            "McpClientManager: server %r (stdio) command %r"
                            " not in allowed_stdio_commands, skipping",
                            cfg.name,
                            cmd_for_check,
                        )
                        continue

            try:
                tool_filters = self._build_tool_filters(cfg)
                filter_kwargs: dict[str, object] = (
                    {"tool_filters": tool_filters} if tool_filters is not None else {}
                )

                if cfg.type == "sse":
                    if not cfg.url:
                        logger.warning(
                            "McpClientManager: server %r (sse) missing 'url', skipping",
                            cfg.name,
                        )
                        continue
                    url = cfg.url
                    client = MCPClient(
                        lambda _url=url: sse_client(_url),  # noqa: B023
                        **filter_kwargs,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
                    )

                elif cfg.type == "stdio":
                    if not cfg.command:
                        logger.warning(
                            "McpClientManager: server %r (stdio) missing 'command', skipping",
                            cfg.name,
                        )
                        continue
                    # Build a scrubbed env for the MCP stdio child process.
                    # Only variables operator explicitly listed in cfg.env (with optional
                    # ${VAR} expansion) bypass the secret scrub — this is the one
                    # sanctioned egress path for credentials (same as build_subprocess_env
                    # extra= contract).  All other host-env vars (ANTHROPIC_API_KEY /
                    # AWS_* / GITHUB_TOKEN / SSH_AUTH_SOCK etc.) are stripped by default.
                    #
                    # cwd=Path.cwd(): build_subprocess_env uses this only to set the
                    # PWD env var.  We deliberately leave StdioServerParameters.cwd
                    # unset (defaults to None), so the MCP child inherits the API
                    # process's real cwd; PWD=Path.cwd() therefore matches that actual
                    # cwd without needing project-root knowledge here — MCP is outside
                    # path_guard scope regardless.
                    expanded_env = _expand_server_env(cfg)
                    child_env = build_subprocess_env(cwd=Path.cwd(), extra=expanded_env)
                    params = StdioServerParameters(
                        command=cfg.command,
                        args=list(cfg.args),
                        env=child_env,
                    )
                    client = MCPClient(
                        lambda _params=params: stdio_client(_params),  # noqa: B023
                        **filter_kwargs,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
                    )

                else:
                    logger.warning(
                        "McpClientManager: server %r has unknown type %r, skipping",
                        cfg.name,
                        cfg.type,
                    )
                    continue

                client.__enter__()  # type: ignore[attr-defined]
                self._clients.append(client)
                self._client_names.append(cfg.name)
                logger.info("McpClientManager: server %r (%s) connected", cfg.name, cfg.type)

            except Exception:
                logger.warning(
                    "McpClientManager: failed to connect server %r, skipping",
                    cfg.name,
                    exc_info=True,
                )

        self._started = True
        logger.info(
            "McpClientManager: started — %d/%d servers connected",
            len(self._clients),
            len(self.configs),
        )

    # ------------------------------------------------------------------
    # Synchronous get_tools (called via to_thread)
    # ------------------------------------------------------------------

    def _get_tools(self) -> list[Any]:
        """Return all tools from connected MCP servers (synchronous)."""
        tools: list[object] = []
        for client in self._clients:
            try:
                server_tools = client.list_tools_sync()  # type: ignore[attr-defined]
                tools.extend(server_tools)
                logger.debug("McpClientManager: got %d tools from server", len(server_tools))
            except Exception:
                logger.warning(
                    "McpClientManager: failed to list tools from a server", exc_info=True
                )
        return tools

    def _get_tools_by_server(self) -> dict[str, list[Any]]:
        """Return tools grouped by server name (synchronous).

        Keys are the McpServerConfig.name values for servers that connected
        successfully.  Servers that fail to list tools are logged and omitted.
        """
        result: dict[str, list[Any]] = {}
        for name, client in zip(self._client_names, self._clients, strict=False):
            try:
                server_tools = client.list_tools_sync()  # type: ignore[attr-defined]
                result[name] = list(server_tools)
                logger.debug(
                    "McpClientManager: got %d tools from server %r", len(server_tools), name
                )
            except Exception:
                logger.warning(
                    "McpClientManager: failed to list tools from server %r", name, exc_info=True
                )
        return result

    # ------------------------------------------------------------------
    # Synchronous stop (called via to_thread)
    # ------------------------------------------------------------------

    def _stop(self) -> None:
        """Stop all MCP client connections (synchronous)."""
        for client in self._clients:
            try:
                client.__exit__(None, None, None)  # type: ignore[attr-defined]
            except Exception:
                logger.warning("McpClientManager: error stopping a client", exc_info=True)

        self._clients.clear()
        self._client_names.clear()
        self._started = False
        logger.info("McpClientManager: stopped")

    # ------------------------------------------------------------------
    # Async wrappers
    # ------------------------------------------------------------------

    async def start_async(self) -> None:
        """Start all MCP servers asynchronously (wraps _start in to_thread)."""
        await asyncio.to_thread(self._start)

    async def get_tools_async(self) -> list[object]:
        """Return all MCP tools asynchronously (wraps _get_tools in to_thread)."""
        return await asyncio.to_thread(self._get_tools)

    async def get_tools_by_server_async(self) -> dict[str, list[Any]]:
        """Return tools grouped by server name asynchronously.

        Used by EpicOrchestrator to build ``_mcp_tools_by_server`` so that
        profile-scoped MCP subsets can be computed without re-querying the servers
        on every dispatch.
        """
        return await asyncio.to_thread(self._get_tools_by_server)

    async def stop_async(self) -> None:
        """Stop all MCP servers asynchronously (wraps _stop in to_thread).

        Always safe to call even if start_async was never called or failed.
        """
        await asyncio.to_thread(self._stop)
