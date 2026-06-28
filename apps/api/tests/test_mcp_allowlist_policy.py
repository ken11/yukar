"""Tests for MCP allowlist policy — registration gate and connection gate.

Covers:
1. McpSettings model defaults and construction.
2. write_mcp_server (registration gate, Layer 1):
   - Default allow_agent_registration=False → always reject.
   - allow_agent_registration=True + empty allowed_sse_hosts → reject.
   - allow_agent_registration=True + url host in allowed_sse_hosts → accept.
   - allow_agent_registration=True + url host NOT in allowed_sse_hosts → reject.
   - stdio: empty allowed_stdio_commands → reject.
   - stdio: command basename in allowed_stdio_commands → accept.
   - stdio: command basename NOT in allowed_stdio_commands → reject.
   - mcp_settings=None (legacy/no policy) → always accept (backward compat).
3. McpClientManager._start (connection gate, Layer 2):
   - allowed_sse_hosts empty → no restriction, connection attempted for all sse.
   - allowed_sse_hosts non-empty + url host allowed → connection attempted.
   - allowed_sse_hosts non-empty + url host not allowed → server skipped (connect fn not called).
   - allowed_stdio_commands empty → no restriction.
   - allowed_stdio_commands non-empty + command basename allowed → connection attempted.
   - allowed_stdio_commands non-empty + command basename not allowed → server skipped.
4. Shared helpers _sse_host_allowed / _stdio_command_allowed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1. McpSettings model
# ---------------------------------------------------------------------------


class TestMcpSettingsModel:
    def test_defaults(self) -> None:
        from yukar.config.settings import McpSettings

        s = McpSettings()
        assert s.allow_agent_registration is False
        assert s.allowed_sse_hosts == []
        assert s.allowed_stdio_commands == []

    def test_custom_values(self) -> None:
        from yukar.config.settings import McpSettings

        s = McpSettings(
            allow_agent_registration=True,
            allowed_sse_hosts=["example.com", "localhost"],
            allowed_stdio_commands=["npx", "node"],
        )
        assert s.allow_agent_registration is True
        assert "example.com" in s.allowed_sse_hosts
        assert "npx" in s.allowed_stdio_commands

    def test_settings_has_mcp_field(self) -> None:
        from yukar.config.settings import McpSettings, Settings

        cfg = Settings()
        assert isinstance(cfg.mcp, McpSettings)
        assert cfg.mcp.allow_agent_registration is False


# ---------------------------------------------------------------------------
# 2. Shared helpers
# ---------------------------------------------------------------------------


class TestSharedHelpers:
    def test_sse_host_allowed_match(self) -> None:
        from yukar.agents.mcp_manager import _sse_host_allowed

        assert _sse_host_allowed("http://example.com/sse", ["example.com"]) is True

    def test_sse_host_allowed_no_match(self) -> None:
        from yukar.agents.mcp_manager import _sse_host_allowed

        assert _sse_host_allowed("http://evil.com/sse", ["example.com"]) is False

    def test_sse_host_allowed_empty_list(self) -> None:
        from yukar.agents.mcp_manager import _sse_host_allowed

        # Empty list: not in it → returns False.  Caller decides semantics.
        assert _sse_host_allowed("http://anything.com/sse", []) is False

    def test_stdio_command_allowed_match(self) -> None:
        from yukar.agents.mcp_manager import _stdio_command_allowed

        assert _stdio_command_allowed("npx", ["npx", "node"]) is True
        assert _stdio_command_allowed("/usr/local/bin/npx", ["npx"]) is True

    def test_stdio_command_allowed_no_match(self) -> None:
        from yukar.agents.mcp_manager import _stdio_command_allowed

        assert _stdio_command_allowed("bash", ["npx"]) is False

    def test_stdio_command_allowed_empty_list(self) -> None:
        from yukar.agents.mcp_manager import _stdio_command_allowed

        assert _stdio_command_allowed("npx", []) is False


# ---------------------------------------------------------------------------
# 3. write_mcp_server — registration gate (Layer 1)
# ---------------------------------------------------------------------------


class TestWriteMcpServerPolicy:
    """Tests for the write_mcp_server Strands tool with McpSettings policy."""

    def _get_write_mcp_fn(self, root: str, mcp_settings: Any = None) -> Any:
        from yukar.agents.tools.skill_mcp_tools import make_skill_mcp_tools

        tools = make_skill_mcp_tools(root, "proj", mcp_settings)
        tool = tools[3]
        return tool.func if hasattr(tool, "func") else tool.__wrapped__

    @pytest.mark.asyncio
    async def test_default_policy_rejects_sse(self, tmp_path: Path) -> None:
        """Default McpSettings (allow_agent_registration=False) → reject SSE."""
        from yukar.config.settings import McpSettings

        fn = self._get_write_mcp_fn(str(tmp_path), McpSettings())
        result = await fn(
            name="my-server",
            server_type="sse",
            url="http://example.com/sse",
        )
        assert result["ok"] is False
        assert "allow_agent_registration=false" in result["error"]

    @pytest.mark.asyncio
    async def test_default_policy_rejects_stdio(self, tmp_path: Path) -> None:
        """Default McpSettings (allow_agent_registration=False) → reject stdio."""
        from yukar.config.settings import McpSettings

        fn = self._get_write_mcp_fn(str(tmp_path), McpSettings())
        result = await fn(
            name="my-server",
            server_type="stdio",
            command="npx",
        )
        assert result["ok"] is False
        assert "allow_agent_registration=false" in result["error"]

    @pytest.mark.asyncio
    async def test_no_policy_allows_all(self, tmp_path: Path) -> None:
        """mcp_settings=None (legacy) → all registrations allowed."""
        fn = self._get_write_mcp_fn(str(tmp_path), None)
        result = await fn(
            name="my-server",
            server_type="stdio",
            command="npx",
            args=["@github/mcp"],
        )
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_registration_enabled_empty_sse_allowlist_rejects(self, tmp_path: Path) -> None:
        """allow_agent_registration=True but allowed_sse_hosts=[] → reject."""
        from yukar.config.settings import McpSettings

        fn = self._get_write_mcp_fn(
            str(tmp_path),
            McpSettings(allow_agent_registration=True, allowed_sse_hosts=[]),
        )
        result = await fn(
            name="my-sse",
            server_type="sse",
            url="http://example.com/sse",
        )
        assert result["ok"] is False
        assert "allowed_sse_hosts" in result["error"]

    @pytest.mark.asyncio
    async def test_registration_enabled_host_in_allowlist_accepts(self, tmp_path: Path) -> None:
        """allow_agent_registration=True + host in allowed_sse_hosts → accept."""
        from yukar.config.settings import McpSettings

        fn = self._get_write_mcp_fn(
            str(tmp_path),
            McpSettings(
                allow_agent_registration=True,
                allowed_sse_hosts=["example.com"],
            ),
        )
        result = await fn(
            name="my-sse",
            server_type="sse",
            url="http://example.com/sse",
        )
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_registration_enabled_host_not_in_allowlist_rejects(self, tmp_path: Path) -> None:
        """allow_agent_registration=True + host NOT in allowed_sse_hosts → reject."""
        from yukar.config.settings import McpSettings

        fn = self._get_write_mcp_fn(
            str(tmp_path),
            McpSettings(
                allow_agent_registration=True,
                allowed_sse_hosts=["trusted.com"],
            ),
        )
        result = await fn(
            name="evil-sse",
            server_type="sse",
            url="http://evil.com/sse",
        )
        assert result["ok"] is False
        assert "evil.com" in result["error"]

    @pytest.mark.asyncio
    async def test_registration_enabled_empty_stdio_allowlist_rejects(self, tmp_path: Path) -> None:
        """allow_agent_registration=True but allowed_stdio_commands=[] → reject."""
        from yukar.config.settings import McpSettings

        fn = self._get_write_mcp_fn(
            str(tmp_path),
            McpSettings(
                allow_agent_registration=True,
                allowed_stdio_commands=[],
            ),
        )
        result = await fn(
            name="my-stdio",
            server_type="stdio",
            command="npx",
        )
        assert result["ok"] is False
        assert "allowed_stdio_commands" in result["error"]

    @pytest.mark.asyncio
    async def test_registration_enabled_command_in_allowlist_accepts(self, tmp_path: Path) -> None:
        """allow_agent_registration=True + command basename in allowed_stdio_commands → accept."""
        from yukar.config.settings import McpSettings

        fn = self._get_write_mcp_fn(
            str(tmp_path),
            McpSettings(
                allow_agent_registration=True,
                allowed_stdio_commands=["npx"],
            ),
        )
        result = await fn(
            name="npm-mcp",
            server_type="stdio",
            command="npx",
            args=["@github/mcp"],
        )
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_registration_enabled_command_not_in_allowlist_rejects(
        self, tmp_path: Path
    ) -> None:
        """allow_agent_registration=True + command NOT in allowed_stdio_commands → reject."""
        from yukar.config.settings import McpSettings

        fn = self._get_write_mcp_fn(
            str(tmp_path),
            McpSettings(
                allow_agent_registration=True,
                allowed_stdio_commands=["npx"],
            ),
        )
        result = await fn(
            name="bad-server",
            server_type="stdio",
            command="bash",
        )
        assert result["ok"] is False
        assert "bash" in result["error"]

    @pytest.mark.asyncio
    async def test_command_absolute_path_rejected(self, tmp_path: Path) -> None:
        """Absolute path commands (with path separators) are rejected (B1 hardening).

        Previously only the basename was checked, allowing '/attacker/path/npx'
        to bypass the allowlist check if 'npx' was allowed.  Now any command
        containing a path separator is rejected at registration time.
        """
        from yukar.config.settings import McpSettings

        fn = self._get_write_mcp_fn(
            str(tmp_path),
            McpSettings(
                allow_agent_registration=True,
                allowed_stdio_commands=["npx"],
            ),
        )
        result = await fn(
            name="abs-server",
            server_type="stdio",
            command="/usr/local/bin/npx",
            args=["@github/mcp"],
        )
        # B1 hardening: absolute path in command is rejected
        assert result["ok"] is False
        assert "path separator" in result["content"][0]["text"].lower()


# ---------------------------------------------------------------------------
# 4. McpClientManager._start — connection gate (Layer 2)
# ---------------------------------------------------------------------------


class TestMcpClientManagerConnectionGate:
    """Tests for McpClientManager._start with mcp_settings allowlist enforcement.

    The MCP dependency is mocked — we verify whether the MCPClient constructor
    (and thus the underlying connection function) was called or skipped.
    """

    def _make_manager(self, configs: list[Any], mcp_settings: Any = None) -> Any:
        from yukar.agents.mcp_manager import McpClientManager

        return McpClientManager(configs, mcp_settings)

    def _sse_cfg(self, name: str, url: str) -> Any:
        from yukar.models.mcp import McpServerConfig

        return McpServerConfig(name=name, type="sse", url=url)

    def _stdio_cfg(self, name: str, command: str) -> Any:
        from yukar.models.mcp import McpServerConfig

        return McpServerConfig(name=name, type="stdio", command=command)

    def test_no_allowlist_connects_all_sse(self, tmp_path: Path) -> None:
        """Empty allowed_sse_hosts → no restriction; all sse servers attempted."""
        from yukar.config.settings import McpSettings

        manager = self._make_manager(
            [self._sse_cfg("s1", "http://a.com/sse"), self._sse_cfg("s2", "http://b.com/sse")],
            McpSettings(allow_agent_registration=False, allowed_sse_hosts=[]),
        )

        connect_calls: list[str] = []

        def _fake_sse(url: str) -> MagicMock:
            connect_calls.append(url)
            return MagicMock()

        fake_client = MagicMock()
        fake_client.__enter__ = MagicMock(return_value=fake_client)
        fake_client.__exit__ = MagicMock(return_value=False)

        with (
            patch("yukar.agents.mcp_manager._MCP_AVAILABLE", True),
            patch("yukar.agents.mcp_manager.sse_client", side_effect=_fake_sse),
            patch("yukar.agents.mcp_manager.MCPClient", return_value=fake_client) as mock_mcp,
        ):
            manager._start()

        # Both servers should have been passed to MCPClient (attempted connection).
        assert mock_mcp.call_count == 2

    def test_sse_allowlist_blocks_unlisted_host(self) -> None:
        """Non-empty allowed_sse_hosts: unlisted host is skipped."""
        from yukar.config.settings import McpSettings

        manager = self._make_manager(
            [
                self._sse_cfg("trusted", "http://trusted.com/sse"),
                self._sse_cfg("evil", "http://evil.com/sse"),
            ],
            McpSettings(allowed_sse_hosts=["trusted.com"]),
        )

        fake_client = MagicMock()
        fake_client.__enter__ = MagicMock(return_value=fake_client)
        fake_client.__exit__ = MagicMock(return_value=False)

        with (
            patch("yukar.agents.mcp_manager._MCP_AVAILABLE", True),
            patch("yukar.agents.mcp_manager.sse_client"),
            patch("yukar.agents.mcp_manager.MCPClient", return_value=fake_client) as mock_mcp,
        ):
            manager._start()

        # Only the trusted server should have been attempted.
        assert mock_mcp.call_count == 1
        assert manager._client_names == ["trusted"]

    def test_sse_allowlist_allows_listed_host(self) -> None:
        """Non-empty allowed_sse_hosts: listed host is connected."""
        from yukar.config.settings import McpSettings

        manager = self._make_manager(
            [self._sse_cfg("ok-server", "http://example.com/sse")],
            McpSettings(allowed_sse_hosts=["example.com"]),
        )

        fake_client = MagicMock()
        fake_client.__enter__ = MagicMock(return_value=fake_client)
        fake_client.__exit__ = MagicMock(return_value=False)

        with (
            patch("yukar.agents.mcp_manager._MCP_AVAILABLE", True),
            patch("yukar.agents.mcp_manager.sse_client"),
            patch("yukar.agents.mcp_manager.MCPClient", return_value=fake_client) as mock_mcp,
        ):
            manager._start()

        assert mock_mcp.call_count == 1

    def test_no_stdio_allowlist_connects_all(self) -> None:
        """Empty allowed_stdio_commands → no restriction; all stdio servers attempted."""
        from yukar.config.settings import McpSettings

        manager = self._make_manager(
            [self._stdio_cfg("s1", "npx"), self._stdio_cfg("s2", "node")],
            McpSettings(allowed_stdio_commands=[]),
        )

        fake_client = MagicMock()
        fake_client.__enter__ = MagicMock(return_value=fake_client)
        fake_client.__exit__ = MagicMock(return_value=False)

        fake_params = MagicMock()

        with (
            patch("yukar.agents.mcp_manager._MCP_AVAILABLE", True),
            patch("yukar.agents.mcp_manager.StdioServerParameters", return_value=fake_params),
            patch("yukar.agents.mcp_manager.stdio_client"),
            patch("yukar.agents.mcp_manager.MCPClient", return_value=fake_client) as mock_mcp,
        ):
            manager._start()

        assert mock_mcp.call_count == 2

    def test_stdio_allowlist_blocks_unlisted_command(self) -> None:
        """Non-empty allowed_stdio_commands: unlisted command basename is skipped."""
        from yukar.config.settings import McpSettings

        manager = self._make_manager(
            [
                self._stdio_cfg("allowed", "npx"),
                self._stdio_cfg("blocked", "bash"),
            ],
            McpSettings(allowed_stdio_commands=["npx"]),
        )

        fake_client = MagicMock()
        fake_client.__enter__ = MagicMock(return_value=fake_client)
        fake_client.__exit__ = MagicMock(return_value=False)

        fake_params = MagicMock()

        with (
            patch("yukar.agents.mcp_manager._MCP_AVAILABLE", True),
            patch("yukar.agents.mcp_manager.StdioServerParameters", return_value=fake_params),
            patch("yukar.agents.mcp_manager.stdio_client"),
            patch("yukar.agents.mcp_manager.MCPClient", return_value=fake_client) as mock_mcp,
        ):
            manager._start()

        assert mock_mcp.call_count == 1
        assert manager._client_names == ["allowed"]

    def test_stdio_allowlist_allows_listed_command(self) -> None:
        """Non-empty allowed_stdio_commands: listed command basename is connected."""
        from yukar.config.settings import McpSettings

        manager = self._make_manager(
            [self._stdio_cfg("npm-server", "npx")],
            McpSettings(allowed_stdio_commands=["npx"]),
        )

        fake_client = MagicMock()
        fake_client.__enter__ = MagicMock(return_value=fake_client)
        fake_client.__exit__ = MagicMock(return_value=False)

        fake_params = MagicMock()

        with (
            patch("yukar.agents.mcp_manager._MCP_AVAILABLE", True),
            patch("yukar.agents.mcp_manager.StdioServerParameters", return_value=fake_params),
            patch("yukar.agents.mcp_manager.stdio_client"),
            patch("yukar.agents.mcp_manager.MCPClient", return_value=fake_client) as mock_mcp,
        ):
            manager._start()

        assert mock_mcp.call_count == 1

    def test_no_mcp_settings_connects_all(self) -> None:
        """mcp_settings=None (legacy) → no restriction at connection time."""
        manager = self._make_manager(
            [self._sse_cfg("legacy", "http://any.host/sse")],
            None,
        )

        fake_client = MagicMock()
        fake_client.__enter__ = MagicMock(return_value=fake_client)
        fake_client.__exit__ = MagicMock(return_value=False)

        with (
            patch("yukar.agents.mcp_manager._MCP_AVAILABLE", True),
            patch("yukar.agents.mcp_manager.sse_client"),
            patch("yukar.agents.mcp_manager.MCPClient", return_value=fake_client) as mock_mcp,
        ):
            manager._start()

        assert mock_mcp.call_count == 1

    def test_stdio_absolute_path_basename_checked(self) -> None:
        """stdio command given as absolute path: basename is matched against allowlist."""
        from yukar.config.settings import McpSettings

        manager = self._make_manager(
            [self._stdio_cfg("abs-server", "/usr/local/bin/npx")],
            McpSettings(allowed_stdio_commands=["npx"]),
        )

        fake_client = MagicMock()
        fake_client.__enter__ = MagicMock(return_value=fake_client)
        fake_client.__exit__ = MagicMock(return_value=False)

        fake_params = MagicMock()

        with (
            patch("yukar.agents.mcp_manager._MCP_AVAILABLE", True),
            patch("yukar.agents.mcp_manager.StdioServerParameters", return_value=fake_params),
            patch("yukar.agents.mcp_manager.stdio_client"),
            patch("yukar.agents.mcp_manager.MCPClient", return_value=fake_client) as mock_mcp,
        ):
            manager._start()

        # /usr/local/bin/npx → basename "npx" → allowed.
        assert mock_mcp.call_count == 1


# ---------------------------------------------------------------------------
# 5. Case A: stdio child-process env scrub
# ---------------------------------------------------------------------------


class TestStdioEnvScrub:
    """Tests for case A: build_subprocess_env used for stdio MCP child env.

    StdioServerParameters is monkeypatched to capture the env kwarg so we can
    inspect what the child would receive without spawning a real process.
    """

    def _make_stdio_manager(self, env_cfg: dict[str, str] | None = None) -> Any:
        from yukar.agents.mcp_manager import McpClientManager
        from yukar.models.mcp import McpServerConfig

        cfg = McpServerConfig(
            name="test-stdio",
            type="stdio",
            command="npx",
            args=["@test/mcp"],
            env=env_cfg or {},
        )
        return McpClientManager([cfg], mcp_settings=None)

    def _run_start_capture_env(
        self,
        manager: Any,
        monkeypatch: Any,
        extra_host_env: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Start manager with mocked MCP deps; return the env dict captured from
        StdioServerParameters."""
        if extra_host_env:
            for k, v in extra_host_env.items():
                monkeypatch.setenv(k, v)

        captured: dict[str, Any] = {}

        def _capture_params(**kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            return MagicMock()

        fake_client = MagicMock()
        fake_client.__enter__ = MagicMock(return_value=fake_client)
        fake_client.__exit__ = MagicMock(return_value=False)

        with (
            patch("yukar.agents.mcp_manager._MCP_AVAILABLE", True),
            patch(
                "yukar.agents.mcp_manager.StdioServerParameters",
                side_effect=_capture_params,
            ),
            patch("yukar.agents.mcp_manager.stdio_client"),
            patch("yukar.agents.mcp_manager.MCPClient", return_value=fake_client),
        ):
            manager._start()

        assert "env" in captured, "StdioServerParameters was not called with env kwarg"
        return captured["env"]  # type: ignore[return-value]

    def test_env_is_always_dict_not_none(self, monkeypatch: Any) -> None:
        """env kwarg passed to StdioServerParameters must be a dict, never None."""
        manager = self._make_stdio_manager(env_cfg={})
        env = self._run_start_capture_env(manager, monkeypatch)
        assert isinstance(env, dict)
        assert env is not None

    def test_host_secrets_not_in_child_env(self, monkeypatch: Any) -> None:
        """Host secrets must NOT appear in the child env even when set in os.environ."""
        manager = self._make_stdio_manager(env_cfg={})
        env = self._run_start_capture_env(
            manager,
            monkeypatch,
            extra_host_env={
                "ANTHROPIC_API_KEY": "sk-host-secret",
                "AWS_SECRET_ACCESS_KEY": "aws-host-secret",
                "GITHUB_TOKEN": "gh-host-token",
                "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
            },
        )
        assert "ANTHROPIC_API_KEY" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "GITHUB_TOKEN" not in env
        assert "SSH_AUTH_SOCK" not in env

    def test_explicit_cfg_env_literal_is_passed(self, monkeypatch: Any) -> None:
        """Variables explicitly listed in cfg.env with literal values must reach the child."""
        manager = self._make_stdio_manager(env_cfg={"MY_VAR": "hello"})
        env = self._run_start_capture_env(manager, monkeypatch)
        assert env.get("MY_VAR") == "hello"

    def test_explicit_cfg_env_expands_and_passes_secret(self, monkeypatch: Any) -> None:
        """Operator may explicitly route a host secret via cfg.env=${VAR} (sanctioned path)."""
        monkeypatch.setenv("GITHUB_TOKEN", "gh-explicit-token")
        manager = self._make_stdio_manager(env_cfg={"GITHUB_TOKEN": "${GITHUB_TOKEN}"})
        env = self._run_start_capture_env(manager, monkeypatch)
        # cfg.env explicitly requested GITHUB_TOKEN → it must be present.
        assert env.get("GITHUB_TOKEN") == "gh-explicit-token"

    def test_cfg_env_var_expansion(self, monkeypatch: Any) -> None:
        """${VAR} in cfg.env is expanded from os.environ before reaching the child."""
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        manager = self._make_stdio_manager(env_cfg={"AWS_REGION": "${AWS_REGION}"})
        env = self._run_start_capture_env(manager, monkeypatch)
        assert env.get("AWS_REGION") == "us-east-1"

    def test_path_and_home_preserved(self, monkeypatch: Any) -> None:
        """PATH and HOME must be present in the child env (safe passthrough)."""
        # Set both explicitly so the assertion does not depend on the test
        # runner's ambient env — HOME has no fallback injection (unlike PATH).
        manager = self._make_stdio_manager(env_cfg={})
        env = self._run_start_capture_env(
            manager,
            monkeypatch,
            extra_host_env={"PATH": "/usr/bin:/bin", "HOME": "/home/tester"},
        )
        assert env.get("PATH") == "/usr/bin:/bin"
        assert env.get("HOME") == "/home/tester"


# ---------------------------------------------------------------------------
# 6. Case B: strict mode (enforce_connection_allowlist=True)
# ---------------------------------------------------------------------------


class TestStrictConnectionGate:
    """Tests for case B: enforce_connection_allowlist=True → empty allowlist = fail-closed."""

    def _make_manager(self, configs: list[Any], mcp_settings: Any = None) -> Any:
        from yukar.agents.mcp_manager import McpClientManager

        return McpClientManager(configs, mcp_settings)

    def _sse_cfg(self, name: str, url: str) -> Any:
        from yukar.models.mcp import McpServerConfig

        return McpServerConfig(name=name, type="sse", url=url)

    def _stdio_cfg(self, name: str, command: str) -> Any:
        from yukar.models.mcp import McpServerConfig

        return McpServerConfig(name=name, type="stdio", command=command)

    def _run_start(self, manager: Any) -> int:
        """Run _start with mocked MCP deps; return MCPClient call_count."""
        fake_client = MagicMock()
        fake_client.__enter__ = MagicMock(return_value=fake_client)
        fake_client.__exit__ = MagicMock(return_value=False)
        fake_params = MagicMock()

        with (
            patch("yukar.agents.mcp_manager._MCP_AVAILABLE", True),
            patch("yukar.agents.mcp_manager.StdioServerParameters", return_value=fake_params),
            patch("yukar.agents.mcp_manager.stdio_client"),
            patch("yukar.agents.mcp_manager.sse_client"),
            patch("yukar.agents.mcp_manager.MCPClient", return_value=fake_client) as mock_mcp,
        ):
            manager._start()
            return mock_mcp.call_count  # type: ignore[return-value]

    def test_enforce_connection_allowlist_default_is_false(self) -> None:
        """McpSettings.enforce_connection_allowlist must default to False."""
        from yukar.config.settings import McpSettings

        assert McpSettings().enforce_connection_allowlist is False

    def test_strict_empty_sse_allowlist_rejects_all(self) -> None:
        """strict=True + empty allowed_sse_hosts → all SSE servers rejected."""
        from yukar.config.settings import McpSettings

        manager = self._make_manager(
            [self._sse_cfg("s1", "http://a.com/sse"), self._sse_cfg("s2", "http://b.com/sse")],
            McpSettings(enforce_connection_allowlist=True, allowed_sse_hosts=[]),
        )
        call_count = self._run_start(manager)
        assert call_count == 0
        assert manager._client_names == []

    def test_strict_empty_stdio_allowlist_rejects_all(self) -> None:
        """strict=True + empty allowed_stdio_commands → all stdio servers rejected."""
        from yukar.config.settings import McpSettings

        manager = self._make_manager(
            [self._stdio_cfg("s1", "npx"), self._stdio_cfg("s2", "uvx")],
            McpSettings(enforce_connection_allowlist=True, allowed_stdio_commands=[]),
        )
        call_count = self._run_start(manager)
        assert call_count == 0
        assert manager._client_names == []

    def test_strict_nonempty_sse_allowlist_listed_connects(self) -> None:
        """strict=True + non-empty allowlist + listed host → connects."""
        from yukar.config.settings import McpSettings

        manager = self._make_manager(
            [self._sse_cfg("ok", "http://trusted.com/sse")],
            McpSettings(
                enforce_connection_allowlist=True,
                allowed_sse_hosts=["trusted.com"],
            ),
        )
        call_count = self._run_start(manager)
        assert call_count == 1

    def test_strict_nonempty_stdio_allowlist_listed_connects(self) -> None:
        """strict=True + non-empty allowlist + listed command → connects."""
        from yukar.config.settings import McpSettings

        manager = self._make_manager(
            [self._stdio_cfg("ok", "npx")],
            McpSettings(
                enforce_connection_allowlist=True,
                allowed_stdio_commands=["npx"],
            ),
        )
        call_count = self._run_start(manager)
        assert call_count == 1

    def test_default_false_empty_allowlist_is_unrestricted(self) -> None:
        """Backward compat: enforce_connection_allowlist=False + empty allowlist = unrestricted."""
        from yukar.config.settings import McpSettings

        manager = self._make_manager(
            [self._sse_cfg("s1", "http://any.com/sse"), self._stdio_cfg("s2", "npx")],
            McpSettings(enforce_connection_allowlist=False),
        )
        call_count = self._run_start(manager)
        # Both servers should be attempted (no restriction).
        assert call_count == 2
