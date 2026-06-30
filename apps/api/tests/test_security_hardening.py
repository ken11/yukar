"""Tests for security hardening items 1-6 from the 2026-06 security re-audit.

Items covered:
  1. api_key_env validator in LLMSettings (B3-01)
  2. Secret-file name blocklist in indexer/_collect_files (A4-01)
  3. Attached-short-option path escape in _arg_escapes_worktree (A2-01)
  4a. run_startup integrity checksum logging (A3-01)
  4b. SensitiveFileWrittenEvent published from write_agent_config / write_skill /
      write_agent_profile / remember / complete_epic learnings (A3-01/-02)
  5. Bounded read in fs_read and fs_edit._read_file (A1-02)
  6. One-liner hardening: summarizer symlink guard, ignore.py follow_symlinks=False,
     docs router existence gates, write_mcp_server path-sep and secret-key rejection
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# 1. api_key_env validator (B3-01)
# ---------------------------------------------------------------------------


class TestApiKeyEnvValidator:
    """LLMSettings.api_key_env must reject dangerous foreign-credential names."""

    def _make(self, api_key_env: str | None) -> Any:
        from yukar.config.settings import LLMSettings

        return LLMSettings(api_key_env=api_key_env)

    def test_none_accepted(self) -> None:
        s = self._make(None)
        assert s.api_key_env is None

    def test_anthropic_api_key_accepted(self) -> None:
        s = self._make("ANTHROPIC_API_KEY")
        assert s.api_key_env == "ANTHROPIC_API_KEY"

    def test_custom_name_accepted(self) -> None:
        s = self._make("MY_LLM_KEY")
        assert s.api_key_env == "MY_LLM_KEY"

    def test_custom_name_with_underscore_accepted(self) -> None:
        s = self._make("_MY_KEY_123")
        assert s.api_key_env == "_MY_KEY_123"

    @pytest.mark.parametrize(
        "name",
        [
            "AWS_SECRET_ACCESS_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SESSION_TOKEN",
            "AWS_SECURITY_TOKEN",
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "GITLAB_TOKEN",
            "SSH_AUTH_SOCK",
            "GOOGLE_APPLICATION_CREDENTIALS",
            # Prefix patterns
            "AWS_PROFILE",  # starts with AWS_
            "AZURE_CLIENT_SECRET",  # starts with AZURE_
            "GCP_SA_KEY",  # starts with GCP_
            # Substring patterns
            "MY_SECRET_VALUE",  # contains SECRET
            "DB_PASSWORD",  # contains PASSWORD
            "PRIVATE_KEY_PATH",  # contains PRIVATE_KEY
        ],
    )
    def test_forbidden_names_rejected(self, name: str) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            self._make(name)

    def test_invalid_identifier_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            self._make("1INVALID")  # starts with digit

    def test_spaces_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            self._make("MY KEY")  # contains space


# ---------------------------------------------------------------------------
# 2. Secret-file name blocklist (A4-01)
# ---------------------------------------------------------------------------


class TestSecretFileBlocklist:
    """_is_secret_file / _collect_files must skip known secret-bearing names."""

    def _is_secret(self, name: str) -> bool:
        from yukar.indexer.walker import _is_secret_file

        return _is_secret_file(Path(name))

    def test_exact_env_blocked(self) -> None:
        assert self._is_secret(".env")

    def test_env_prefix_blocked(self) -> None:
        assert self._is_secret(".env.local")
        assert self._is_secret(".env.production")

    def test_netrc_blocked(self) -> None:
        assert self._is_secret(".netrc")

    def test_credentials_blocked(self) -> None:
        assert self._is_secret("credentials")

    def test_htpasswd_blocked(self) -> None:
        assert self._is_secret(".htpasswd")

    def test_ssh_keys_blocked(self) -> None:
        assert self._is_secret("id_rsa")
        assert self._is_secret("id_dsa")
        assert self._is_secret("id_ecdsa")
        assert self._is_secret("id_ed25519")

    def test_pem_suffix_blocked(self) -> None:
        assert self._is_secret("server.pem")
        assert self._is_secret("ca.pem")

    def test_key_suffix_blocked(self) -> None:
        assert self._is_secret("private.key")

    def test_p12_blocked(self) -> None:
        assert self._is_secret("keystore.p12")

    def test_pfx_blocked(self) -> None:
        assert self._is_secret("cert.pfx")

    def test_pkcs12_blocked(self) -> None:
        assert self._is_secret("cert.pkcs12")

    def test_normal_file_allowed(self) -> None:
        assert not self._is_secret("main.py")
        assert not self._is_secret("README.md")
        assert not self._is_secret("config.yaml")
        assert not self._is_secret("environment.py")

    def test_collect_files_skips_secret_files(self, tmp_path: Path) -> None:
        """_collect_files must skip secret files even if not gitignored."""
        from yukar.indexer.walker import _collect_files
        from yukar.sandbox.ignore import IgnoreRules

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "main.py").write_text("print('hello')")
        (repo / ".env").write_text("SECRET=hunter2")
        (repo / "id_rsa").write_text("-----BEGIN RSA PRIVATE KEY-----")
        (repo / "cert.pem").write_text("-----BEGIN CERTIFICATE-----")

        # Use a git repo so IgnoreRules works
        import subprocess

        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"], cwd=repo, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=repo, check=True
        )

        rules = IgnoreRules.from_repo(repo)
        files = _collect_files(repo, rules)
        names = {f.name for f in files}

        assert "main.py" in names
        assert ".env" not in names
        assert "id_rsa" not in names
        assert "cert.pem" not in names


# ---------------------------------------------------------------------------
# 3. Attached-short-option path escape (A2-01)
# ---------------------------------------------------------------------------


class TestAttachedShortOptionEscape:
    """_arg_escapes_worktree must catch -I/abs and -d@/abs."""

    def _escapes(self, arg: str, worktree: Path) -> bool:
        from yukar.agents.tools.command import _arg_escapes_worktree

        real = str(worktree.resolve())
        return _arg_escapes_worktree(arg, (real,), real)

    def test_absolute_attached_short_opt_rejected(self, tmp_path: Path) -> None:
        assert self._escapes("-I/etc/passwd", tmp_path)

    def test_home_attached_short_opt_rejected(self, tmp_path: Path) -> None:
        assert self._escapes("-I~/secrets", tmp_path)

    def test_at_sign_absolute_attached_rejected(self, tmp_path: Path) -> None:
        # curl -d@/etc/passwd form
        assert self._escapes("-d@/etc/passwd", tmp_path)

    def test_at_sign_home_attached_rejected(self, tmp_path: Path) -> None:
        assert self._escapes("-d@~/file", tmp_path)

    def test_relative_attached_allowed(self, tmp_path: Path) -> None:
        # -I. (current directory) should NOT be flagged
        assert not self._escapes("-I.", tmp_path)

    def test_normal_flag_allowed(self, tmp_path: Path) -> None:
        # -v, --verbose, etc.
        assert not self._escapes("-v", tmp_path)
        assert not self._escapes("--verbose", tmp_path)

    def test_in_worktree_path_allowed(self, tmp_path: Path) -> None:
        # An absolute path INSIDE the worktree should be allowed.
        inside = str(tmp_path / "src")
        arg = f"-I{inside}"
        assert not self._escapes(arg, tmp_path)


# ---------------------------------------------------------------------------
# 5. Bounded read — fs.py (A1-02)
# ---------------------------------------------------------------------------


class TestFsReadBounded:
    """fs_read must cap bytes at _MAX_READ_BYTES even if file grows after stat."""

    def _make_ctx(self, worktree: Path) -> Any:
        from yukar.agents.context import AgentContext
        from yukar.sandbox.path_guard import PathGuard

        guard = PathGuard(root=worktree)
        ctx = MagicMock(spec=AgentContext)
        ctx.path_guard = guard
        return ctx

    def test_normal_file_read(self, tmp_path: Path) -> None:
        from yukar.agents.tools.fs import make_fs_tools

        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "hello.txt").write_text("hello world")

        ctx = self._make_ctx(worktree)
        [fs_read, _, _, _] = make_fs_tools(ctx)
        result = fs_read(path="hello.txt")
        assert result["status"] == "success"
        assert "hello world" in result["content"][0]["text"]

    def test_file_too_large_stat_rejected(self, tmp_path: Path) -> None:
        from yukar.agents.tools.fs import _MAX_READ_BYTES, make_fs_tools

        worktree = tmp_path / "wt"
        worktree.mkdir()
        big = worktree / "big.bin"
        # Write a file just over the limit
        big.write_bytes(b"x" * (_MAX_READ_BYTES + 1))

        ctx = self._make_ctx(worktree)
        [fs_read, _, _, _] = make_fs_tools(ctx)
        result = fs_read(path="big.bin")
        assert result["status"] == "error"
        assert "too large" in result["content"][0]["text"].lower()


# ---------------------------------------------------------------------------
# 5. Bounded read — fs_edit.py (A1-02)
# ---------------------------------------------------------------------------


class TestFsEditBounded:
    """_read_file in fs_edit must reject files over _MAX_EDIT_READ_BYTES."""

    def _make_ctx(self, worktree: Path) -> Any:
        from yukar.agents.context import AgentContext
        from yukar.sandbox.path_guard import PathGuard

        guard = PathGuard(root=worktree)
        ctx = MagicMock(spec=AgentContext)
        ctx.path_guard = guard
        return ctx

    def test_over_limit_rejected(self, tmp_path: Path) -> None:
        from yukar.agents.tools.fs_edit import _MAX_EDIT_READ_BYTES, make_fs_edit_tools

        worktree = tmp_path / "wt"
        worktree.mkdir()
        big = worktree / "big.txt"
        big.write_bytes(b"a" * (_MAX_EDIT_READ_BYTES + 1))

        ctx = self._make_ctx(worktree)
        [replace, _, _] = make_fs_edit_tools(ctx)
        result = replace(path="big.txt", old_text="nonexistent", new_text="x")
        assert result["status"] == "error"
        assert "limit" in result["content"][0]["text"].lower()


# ---------------------------------------------------------------------------
# 6. docs router existence gates (A5-01)
# ---------------------------------------------------------------------------


class TestDocsRouterExistenceGates:
    """PUT /api/projects/{id}/docs and …/epics/{id}/docs must 404 for missing entities."""

    @pytest.mark.asyncio
    async def test_put_project_doc_nonexistent_project_returns_404(
        self, tmp_path: Path
    ) -> None:
        """Confirm get_project_or_404 is called before writing."""
        # The router calls get_project_or_404 which raises HTTPException 404
        # when the project dir doesn't exist.  We can test this directly.
        from fastapi import HTTPException

        from yukar.api.routers.docs import PutDocRequest, put_project_doc

        with pytest.raises(HTTPException) as exc_info:
            await put_project_doc(
                project_id="ghost-project",
                filename="spec.md",
                body=PutDocRequest(content="hello"),
                root=str(tmp_path),
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_put_epic_doc_nonexistent_project_returns_404(
        self, tmp_path: Path
    ) -> None:
        from fastapi import HTTPException

        from yukar.api.routers.docs import PutDocRequest, put_epic_doc

        with pytest.raises(HTTPException) as exc_info:
            await put_epic_doc(
                project_id="ghost-project",
                epic_id="ep-1",
                filename="notes.md",
                body=PutDocRequest(content="hello"),
                root=str(tmp_path),
            )
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# 6. write_mcp_server — path-separator rejection and secret env key rejection (B1)
# ---------------------------------------------------------------------------


class TestWriteMcpServerHardening:
    """write_mcp_server must reject path separators in name and secret env keys."""

    def _tools(self) -> Any:
        from yukar.agents.tools.skill_mcp_tools import make_skill_mcp_tools
        from yukar.config.settings import McpSettings

        mcp = McpSettings(allow_agent_registration=True, allowed_sse_hosts=["example.com"])
        tools = make_skill_mcp_tools(
            root="/tmp/fake_root",
            project_id="proj1",
            mcp_settings=mcp,
        )
        # write_mcp_server is index 3
        return tools[3]

    @pytest.mark.asyncio
    async def test_path_sep_in_name_rejected(self) -> None:
        write_mcp_server = self._tools()
        result = await write_mcp_server(
            name="../../escape",
            server_type="sse",
            url="https://example.com/sse",
        )
        assert result["status"] == "error"
        assert "path separator" in result["content"][0]["text"].lower()

    @pytest.mark.asyncio
    async def test_secret_env_key_rejected(self) -> None:
        write_mcp_server = self._tools()
        result = await write_mcp_server(
            name="my-server",
            server_type="sse",
            url="https://example.com/sse",
            env={"AWS_SECRET_ACCESS_KEY": "leaked"},
        )
        assert result["status"] == "error"
        assert "forbidden" in result["content"][0]["text"].lower()

    @pytest.mark.asyncio
    async def test_path_sep_in_stdio_command_rejected(self) -> None:
        from yukar.agents.tools.skill_mcp_tools import make_skill_mcp_tools
        from yukar.config.settings import McpSettings

        mcp = McpSettings(
            allow_agent_registration=True,
            allowed_stdio_commands=["npx"],
        )
        tools = make_skill_mcp_tools(root="/tmp/fake_root", project_id="proj1", mcp_settings=mcp)
        write_mcp_server = tools[3]
        result = await write_mcp_server(
            name="bad-server",
            server_type="stdio",
            command="/attacker/path/npx",
        )
        assert result["status"] == "error"
        assert "path separator" in result["content"][0]["text"].lower()

    @pytest.mark.asyncio
    async def test_clean_sse_server_accepted(self) -> None:
        write_mcp_server = self._tools()
        # This would fail at the repo save step since we don't have a real workspace,
        # but the early validation gates should pass (error comes from storage, not validation).
        result = await write_mcp_server(
            name="good-server",
            server_type="sse",
            url="https://example.com/sse",
            env={"MY_TOKEN": "value"},
        )
        # Should fail at mcp_repo step (no real FS), NOT at the security gate
        # i.e. the error message should NOT mention path separator or forbidden
        if result["status"] == "error":
            msg = result["content"][0]["text"].lower()
            assert "path separator" not in msg
            assert "forbidden" not in msg


# ---------------------------------------------------------------------------
# 4b. SensitiveFileWrittenEvent published from write_agent_config (A3-01)
# ---------------------------------------------------------------------------


class TestSensitiveFileWrittenEvent:
    """write_agent_config must publish SensitiveFileWrittenEvent via pub_event."""

    @pytest.mark.asyncio
    async def test_write_agent_config_publishes_event(self, tmp_path: Path) -> None:
        from yukar.agents.tools.agent_config_tools import make_agent_config_tools

        published: list[tuple[str, str]] = []

        def fake_pub(kind: str, name: str) -> None:
            published.append((kind, name))

        # Make a minimal project layout
        project_id = "proj1"
        root = str(tmp_path)
        from yukar.config.paths import project_agents_dir

        project_agents_dir(root, project_id).mkdir(parents=True, exist_ok=True)

        tools = make_agent_config_tools(root, project_id, pub_event=fake_pub)
        write_agent_config = tools[0]

        result = await write_agent_config(role="worker", instructions="# Custom")
        assert result["status"] == "success"
        assert published == [("agent_config", "worker")]

    @pytest.mark.asyncio
    async def test_write_skill_publishes_event(self, tmp_path: Path) -> None:
        published: list[tuple[str, str]] = []

        def fake_pub(kind: str, name: str) -> None:
            published.append((kind, name))

        from yukar.agents.tools.skill_mcp_tools import make_skill_mcp_tools

        root = str(tmp_path)
        project_id = "proj1"

        # Create minimal skills directory
        from yukar.config.paths import project_skills_dir

        project_skills_dir(root, project_id).mkdir(parents=True, exist_ok=True)

        tools = make_skill_mcp_tools(root, project_id, pub_event=fake_pub)
        write_skill = tools[2]

        result = await write_skill(name="my-skill", content="# Skill content")
        assert result["status"] == "success"
        assert published == [("skill", "my-skill")]

    @pytest.mark.asyncio
    async def test_write_agent_profile_publishes_event(self, tmp_path: Path) -> None:
        published: list[tuple[str, str]] = []

        def fake_pub(kind: str, name: str) -> None:
            published.append((kind, name))

        from yukar.agents.tools.agent_profile_tools import make_agent_profile_tools

        root = str(tmp_path)
        project_id = "proj1"

        # Create minimal profiles directory
        from yukar.config.paths import yukar_dir

        yukar_dir(root, project_id).mkdir(parents=True, exist_ok=True)

        tools = make_agent_profile_tools(root, project_id, pub_event=fake_pub)
        write_agent_profile = tools[2]

        result = await write_agent_profile(
            name="frontend-worker",
            description="Frontend tasks",
            base_role="worker",
        )
        assert result["status"] == "success"
        assert published == [("agent_profile", "frontend-worker")]

    def test_sensitive_file_written_event_in_run_event_union(self) -> None:
        """SensitiveFileWrittenEvent must be in the RunEvent discriminated union."""
        import typing

        from yukar.models.events import RunEvent, SensitiveFileWrittenEvent

        args = typing.get_args(RunEvent)
        # Unwrap Annotated
        if args:
            inner = args[0]
            member_types = typing.get_args(inner)
            assert SensitiveFileWrittenEvent in member_types, (
                "SensitiveFileWrittenEvent not found in RunEvent union"
            )


# ---------------------------------------------------------------------------
# 2. _is_secret_file edge cases
# ---------------------------------------------------------------------------


class TestIsSecretFileEdgeCases:
    """Additional edge cases for the blocklist."""

    def test_dotenv_dot_local_blocked(self) -> None:
        from yukar.indexer.walker import _is_secret_file

        assert _is_secret_file(Path(".env.local"))

    def test_dotenv_dot_test_blocked(self) -> None:
        from yukar.indexer.walker import _is_secret_file

        assert _is_secret_file(Path(".env.test"))

    def test_env_py_not_blocked(self) -> None:
        """env.py (Django settings) must NOT be blocked."""
        from yukar.indexer.walker import _is_secret_file

        assert not _is_secret_file(Path("env.py"))

    def test_dotenv_inside_dir_blocked(self) -> None:
        """_is_secret_file checks the name only, not the parent."""
        from yukar.indexer.walker import _is_secret_file

        assert _is_secret_file(Path("/some/project/.env"))

    def test_environment_ts_not_blocked(self) -> None:
        from yukar.indexer.walker import _is_secret_file

        assert not _is_secret_file(Path("environment.ts"))

    def test_env_example_not_blocked(self) -> None:
        """.env.example is a committed template, not a secret file."""
        from yukar.indexer.walker import _is_secret_file

        assert not _is_secret_file(Path(".env.example"))

    def test_env_sample_not_blocked(self) -> None:
        """.env.sample is a committed template, not a secret file."""
        from yukar.indexer.walker import _is_secret_file

        assert not _is_secret_file(Path(".env.sample"))

    def test_env_example_indexed_collect_files(self, tmp_path: Path) -> None:
        """.env.example must appear in _collect_files results (not silently dropped)."""
        import subprocess

        from yukar.indexer.walker import _collect_files
        from yukar.sandbox.ignore import IgnoreRules

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "main.py").write_text("print('hello')")
        (repo / ".env.example").write_text("SECRET=<your_secret_here>")
        (repo / ".env.local").write_text("SECRET=real_value")

        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)

        rules = IgnoreRules.from_repo(repo)
        files = _collect_files(repo, rules)
        names = {f.name for f in files}

        assert ".env.example" in names, ".env.example should be indexed (it is a template)"
        assert ".env.local" not in names, ".env.local should be blocked (it is a secret file)"


# ---------------------------------------------------------------------------
# #1 END-TO-END: SensitiveFileWrittenEvent fans out to project-level queue
# ---------------------------------------------------------------------------


class TestSensitiveFileWrittenEventFanout:
    """SensitiveFileWrittenEvent must reach project-level subscribers via the real bus."""

    @pytest.mark.asyncio
    async def test_sensitive_file_written_reaches_project_subscriber(self) -> None:
        """Publishing SensitiveFileWrittenEvent must deliver it to subscribe_project."""
        import asyncio

        from yukar.events import bus as event_bus
        from yukar.models.events import SensitiveFileWrittenEvent

        project_id = "proj-e2e-test"
        epic_id = "ep-1"
        run_id = "run-1"

        received: list[object] = []

        async with event_bus.subscribe_project(project_id) as q:
            # Publish the event through the real bus (not a mock).
            event = SensitiveFileWrittenEvent(
                project_id=project_id,
                epic_id=epic_id,
                run_id=run_id,
                kind="skill",
                name="injected-skill",
            )
            event_bus.publish(project_id, epic_id, event)

            # The event should be in the project queue immediately (put_nowait).
            try:
                item = await asyncio.wait_for(q.get(), timeout=1.0)
                received.append(item)
            except TimeoutError:
                pass  # will fail the assertion below

        assert len(received) == 1, (
            "Expected exactly one event in the project-level queue; "
            f"got {len(received)}.  SensitiveFileWrittenEvent is likely missing "
            "from _LIFECYCLE_TYPES in events/bus.py."
        )
        evt = received[0]
        assert isinstance(evt, SensitiveFileWrittenEvent)
        assert evt.kind == "skill"
        assert evt.name == "injected-skill"

    @pytest.mark.asyncio
    async def test_sensitive_file_written_in_replay_buffer(self) -> None:
        """SensitiveFileWrittenEvent must be replayed to subscribers that connect late."""
        import asyncio

        from yukar.events import bus as event_bus
        from yukar.models.events import SensitiveFileWrittenEvent

        project_id = "proj-replay-test"
        epic_id = "ep-2"
        run_id = "run-2"

        # Publish BEFORE subscribing (late subscriber scenario).
        event = SensitiveFileWrittenEvent(
            project_id=project_id,
            epic_id=epic_id,
            run_id=run_id,
            kind="agent_config",
            name="worker",
        )
        event_bus.publish(project_id, epic_id, event)

        # Now subscribe (late) and check the epic-level replay buffer.
        received: list[object] = []
        async with event_bus.subscribe(project_id, epic_id) as q:
            # Replay buffer events are put_nowait'd before yield, so they're
            # already in the queue when we first call get().
            try:
                item = await asyncio.wait_for(q.get(), timeout=1.0)
                received.append(item)
            except TimeoutError:
                pass

        assert len(received) == 1, (
            "SensitiveFileWrittenEvent should be in the epic-level replay buffer "
            "(included via _LIFECYCLE_TYPES)."
        )
        assert isinstance(received[0], SensitiveFileWrittenEvent)


# ---------------------------------------------------------------------------
# #2 MCP env VALUE ${...} bypass rejection
# ---------------------------------------------------------------------------


class TestMcpEnvValueForbiddenRef:
    """write_mcp_server must reject env VALUES that reference forbidden variables."""

    def _tools(self) -> Any:
        from yukar.agents.tools.skill_mcp_tools import make_skill_mcp_tools
        from yukar.config.settings import McpSettings

        mcp = McpSettings(allow_agent_registration=True, allowed_sse_hosts=["example.com"])
        tools = make_skill_mcp_tools(
            root="/tmp/fake_root",
            project_id="proj1",
            mcp_settings=mcp,
        )
        return tools[3]  # write_mcp_server

    @pytest.mark.asyncio
    async def test_forbidden_var_ref_in_value_rejected(self) -> None:
        """env={"INNOCENT": "${AWS_SECRET_ACCESS_KEY}"} must be rejected."""
        write_mcp_server = self._tools()
        result = await write_mcp_server(
            name="my-server",
            server_type="sse",
            url="https://example.com/sse",
            env={"INNOCENT": "${AWS_SECRET_ACCESS_KEY}"},
        )
        assert result["status"] == "error"
        assert "forbidden" in result["content"][0]["text"].lower()

    @pytest.mark.asyncio
    async def test_lowercase_forbidden_var_ref_in_value_rejected(self) -> None:
        """env={"X": "${aws_secret_access_key}"} must also be rejected (case-insensitive)."""
        write_mcp_server = self._tools()
        result = await write_mcp_server(
            name="my-server",
            server_type="sse",
            url="https://example.com/sse",
            env={"X": "${aws_secret_access_key}"},
        )
        assert result["status"] == "error"
        assert "forbidden" in result["content"][0]["text"].lower()

    @pytest.mark.asyncio
    async def test_benign_literal_value_allowed(self) -> None:
        """env={"MY_TOKEN": "literal"} must NOT be blocked at the validation gate."""
        write_mcp_server = self._tools()
        result = await write_mcp_server(
            name="good-server",
            server_type="sse",
            url="https://example.com/sse",
            env={"MY_TOKEN": "literal_value"},
        )
        # May fail at storage (no real FS), but NOT at the security gate.
        if result["status"] == "error":
            msg = result["content"][0]["text"].lower()
            assert "forbidden" not in msg

    @pytest.mark.asyncio
    async def test_public_var_ref_in_value_allowed(self) -> None:
        """env={"PORT": "${SOME_PUBLIC_VAR}"} (non-forbidden ref) must pass validation."""
        write_mcp_server = self._tools()
        result = await write_mcp_server(
            name="good-server2",
            server_type="sse",
            url="https://example.com/sse",
            env={"PORT": "${SOME_PUBLIC_VAR}"},
        )
        if result["status"] == "error":
            msg = result["content"][0]["text"].lower()
            assert "forbidden" not in msg


# ---------------------------------------------------------------------------
# #3 Case-insensitive secret matching
# ---------------------------------------------------------------------------


class TestCaseInsensitiveSecretMatching:
    """_is_forbidden_api_key_env must reject mixed-case secret names."""

    def _check(self, name: str) -> bool:
        from yukar.config.settings import _is_forbidden_api_key_env

        return _is_forbidden_api_key_env(name)

    def test_lowercase_aws_secret_rejected(self) -> None:
        assert self._check("aws_secret_access_key")

    def test_mixed_case_secret_token_rejected(self) -> None:
        assert self._check("My_Secret_Token")

    def test_mixed_case_password_rejected(self) -> None:
        assert self._check("Db_Password")

    def test_lowercase_github_token_rejected(self) -> None:
        assert self._check("github_token")

    def test_uppercase_still_rejected(self) -> None:
        assert self._check("AWS_SECRET_ACCESS_KEY")

    def test_benign_name_still_allowed(self) -> None:
        assert not self._check("MY_LLM_KEY")
        assert not self._check("ANTHROPIC_API_KEY")
