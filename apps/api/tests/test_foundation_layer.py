"""Tests for M2 foundation layer.

Covers:
- llm/fake: FakeModel with text and tool_use turns, Agent integration
- sandbox/path_guard: traversal, symlink escape, allowed paths
- git/worktree: lazy creation, idempotency, existing-branch reuse
- agents/tools: fs_read/write outside-worktree rejection, run_command allow/deny/cwd/timeout
- agents/tools: git_commit in worktree branch only
- storage/session_store: Strands FileSessionManager on-disk format compatibility
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests._helpers import make_git_repo

# ---------------------------------------------------------------------------
# FakeModel tests
# ---------------------------------------------------------------------------


class TestFakeModel:
    def test_text_turn_single(self) -> None:
        from yukar.llm.fake import FakeModel, TextTurn

        model = FakeModel(script=[TextTurn("hello world")])
        first = model._script[0]
        assert isinstance(first, TextTurn)
        assert first.text == "hello world"
        assert model._index == 0

    def test_tool_use_turn(self) -> None:
        from yukar.llm.fake import FakeModel, ToolUseTurn

        model = FakeModel(script=[ToolUseTurn(tool_name="fs_read", tool_input={"path": "x.py"})])
        first = model._script[0]
        assert isinstance(first, ToolUseTurn)
        assert first.tool_name == "fs_read"

    def test_exhausted_returns_default(self) -> None:
        from yukar.llm.fake import FakeModel

        model = FakeModel(script=[])
        turn = model._next_turn()
        from yukar.llm.fake import TextTurn

        assert isinstance(turn, TextTurn)
        assert "exhausted" in turn.text.lower()

    def test_reset(self) -> None:
        from yukar.llm.fake import FakeModel, TextTurn

        model = FakeModel(script=[TextTurn("a"), TextTurn("b")])
        _ = model._next_turn()
        assert model._index == 1
        model.reset()
        assert model._index == 0

    def test_from_env_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import json

        from yukar.llm.fake import FakeModel, TextTurn

        script = json.dumps([{"type": "text", "text": "from env"}])
        monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
        model = FakeModel.from_env()
        assert len(model._script) == 1
        assert isinstance(model._script[0], TextTurn)
        assert model._script[0].text == "from env"

    def test_from_env_tool_use(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import json

        from yukar.llm.fake import FakeModel, ToolUseTurn

        script = json.dumps([{"type": "tool_use", "tool_name": "git_status", "tool_input": {}}])
        monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
        model = FakeModel.from_env()
        assert isinstance(model._script[0], ToolUseTurn)
        assert model._script[0].tool_name == "git_status"

    # ------------------------------------------------------------------
    # Role-based from_env tests
    # ------------------------------------------------------------------

    def test_from_env_role_object_manager(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Role-object format: manager gets its own script."""
        import json

        from yukar.llm.fake import FakeModel, TextTurn

        script = json.dumps(
            {
                "manager": [{"type": "text", "text": "manager turn"}],
                "worker": [{"type": "text", "text": "worker turn"}],
                "evaluator": [{"type": "text", "text": "evaluator turn"}],
            }
        )
        monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)

        model = FakeModel.from_env(role="manager")
        assert len(model._script) == 1
        assert isinstance(model._script[0], TextTurn)
        assert model._script[0].text == "manager turn"

    def test_from_env_role_object_worker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Role-object format: worker gets its own script."""
        import json

        from yukar.llm.fake import FakeModel, TextTurn

        script = json.dumps(
            {
                "manager": [{"type": "text", "text": "manager turn"}],
                "worker": [{"type": "text", "text": "worker turn"}],
                "evaluator": [{"type": "text", "text": "evaluator turn"}],
            }
        )
        monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)

        model = FakeModel.from_env(role="worker")
        assert len(model._script) == 1
        assert isinstance(model._script[0], TextTurn)
        assert model._script[0].text == "worker turn"

    def test_from_env_role_object_evaluator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Role-object format: evaluator gets its own script."""
        import json

        from yukar.llm.fake import FakeModel, TextTurn

        script = json.dumps(
            {
                "manager": [{"type": "text", "text": "manager turn"}],
                "worker": [{"type": "text", "text": "worker turn"}],
                "evaluator": [{"type": "text", "text": "evaluator turn"}],
            }
        )
        monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)

        model = FakeModel.from_env(role="evaluator")
        assert len(model._script) == 1
        assert isinstance(model._script[0], TextTurn)
        assert model._script[0].text == "evaluator turn"

    def test_from_env_role_object_missing_role_gives_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing role in object format yields empty script (exhausted model)."""
        import json

        from yukar.llm.fake import FakeModel

        script = json.dumps({"manager": [{"type": "text", "text": "hi"}]})
        monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)

        model = FakeModel.from_env(role="worker")
        assert len(model._script) == 0

    def test_from_env_role_object_none_role_gives_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """role=None with object format yields empty script."""
        import json

        from yukar.llm.fake import FakeModel

        script = json.dumps({"manager": [{"type": "text", "text": "hi"}]})
        monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)

        model = FakeModel.from_env(role=None)
        assert len(model._script) == 0

    def test_from_env_array_ignores_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Array format is role-agnostic: all roles get the same turns."""
        import json

        from yukar.llm.fake import FakeModel, TextTurn

        script = json.dumps([{"type": "text", "text": "shared"}])
        monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)

        for role in ("manager", "worker", "evaluator", None):
            model = FakeModel.from_env(role=role)
            assert len(model._script) == 1
            assert isinstance(model._script[0], TextTurn)
            assert model._script[0].text == "shared"

    def test_from_env_each_call_returns_independent_copy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each from_env() call returns a fresh FakeModel starting at index 0."""
        import json

        from yukar.llm.fake import FakeModel

        script = json.dumps(
            {
                "worker": [
                    {"type": "text", "text": "turn-1"},
                    {"type": "text", "text": "turn-2"},
                ]
            }
        )
        monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)

        m1 = FakeModel.from_env(role="worker")
        m2 = FakeModel.from_env(role="worker")

        # Advance m1.
        m1._next_turn()
        assert m1._index == 1

        # m2 is independent — still at the start.
        assert m2._index == 0

    def test_from_env_unknown_turn_type_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unknown turn type in script raises ValueError."""
        import json

        from yukar.llm.fake import FakeModel

        script = json.dumps([{"type": "bogus", "text": "??"}])
        monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)

        with pytest.raises(ValueError, match="Unknown script turn type"):
            FakeModel.from_env()

    # ------------------------------------------------------------------
    # factory.create_model integration tests
    # ------------------------------------------------------------------

    def test_create_model_fake_uses_from_env_array(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """create_model(provider=fake) delegates to from_env and parses the env."""
        import json

        from yukar.config.settings import LLMSettings
        from yukar.llm.factory import create_model
        from yukar.llm.fake import FakeModel, TextTurn

        script = json.dumps([{"type": "text", "text": "factory env"}])
        monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)

        model = create_model(LLMSettings(provider="fake"), role="worker")
        assert isinstance(model, FakeModel)
        assert len(model._script) == 1
        assert isinstance(model._script[0], TextTurn)
        assert model._script[0].text == "factory env"

    def test_create_model_fake_role_object(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """create_model passes role to from_env; role-object scripts are split."""
        import json

        from yukar.config.settings import LLMSettings
        from yukar.llm.factory import create_model
        from yukar.llm.fake import FakeModel, TextTurn

        script = json.dumps(
            {
                "manager": [{"type": "text", "text": "mgr"}],
                "worker": [{"type": "text", "text": "wkr"}],
                "evaluator": [{"type": "text", "text": "eval"}],
            }
        )
        monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)

        mgr = create_model(LLMSettings(provider="fake"), role="manager")
        wkr = create_model(LLMSettings(provider="fake"), role="worker")
        evl = create_model(LLMSettings(provider="fake"), role="evaluator")

        assert isinstance(mgr, FakeModel)
        assert isinstance(mgr._script[0], TextTurn)
        assert mgr._script[0].text == "mgr"

        assert isinstance(wkr, FakeModel)
        assert isinstance(wkr._script[0], TextTurn)
        assert wkr._script[0].text == "wkr"

        assert isinstance(evl, FakeModel)
        assert isinstance(evl._script[0], TextTurn)
        assert evl._script[0].text == "eval"

    def test_create_model_fake_no_env_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no YUKAR_FAKE_SCRIPT set, create_model(fake) gives empty-script model."""
        from yukar.config.settings import LLMSettings
        from yukar.llm.factory import create_model
        from yukar.llm.fake import FakeModel, TextTurn

        monkeypatch.delenv("YUKAR_FAKE_SCRIPT", raising=False)

        model = create_model(LLMSettings(provider="fake"), role="worker")
        assert isinstance(model, FakeModel)
        # Empty script → exhausted immediately.
        turn = model._next_turn()
        assert isinstance(turn, TextTurn)
        assert "exhausted" in turn.text.lower()

    async def test_stream_text_events(self) -> None:
        from yukar.llm.fake import FakeModel, TextTurn

        model = FakeModel(script=[TextTurn("hi")])
        events: list[dict[str, Any]] = []
        async for ev in model.stream(messages=[], tool_specs=None):
            events.append(dict(ev))

        keys = [next(iter(e)) for e in events]
        assert keys[0] == "messageStart"
        assert "contentBlockStart" in keys
        assert "contentBlockDelta" in keys
        assert "contentBlockStop" in keys
        assert keys[-2] == "messageStop"
        assert keys[-1] == "metadata"

        deltas = [e for e in events if "contentBlockDelta" in e]
        # Text may be delivered as multiple chunks; reconstruct the full string.
        text = "".join(e["contentBlockDelta"]["delta"]["text"] for e in deltas)
        assert text == "hi"

    async def test_stream_tool_use_events(self) -> None:
        import json

        from yukar.llm.fake import FakeModel, ToolUseTurn

        model = FakeModel(script=[ToolUseTurn(tool_name="fs_read", tool_input={"path": "a.py"})])
        events: list[dict[str, Any]] = []
        async for ev in model.stream(messages=[], tool_specs=None):
            events.append(dict(ev))

        # Should contain a contentBlockStart with toolUse
        starts = [e for e in events if "contentBlockStart" in e]
        assert starts
        start = starts[0]["contentBlockStart"]
        assert "start" in start
        assert start["start"]["toolUse"]["name"] == "fs_read"

        # Input delta should be valid JSON
        deltas = [e for e in events if "contentBlockDelta" in e]
        assert deltas
        delta_json = deltas[0]["contentBlockDelta"]["delta"]["toolUse"]["input"]
        parsed = json.loads(delta_json)
        assert parsed == {"path": "a.py"}

    async def test_agent_integration_text(self) -> None:
        """Agent with FakeModel should process a text turn without LLM calls."""
        from strands import Agent

        from yukar.llm.fake import FakeModel, TextTurn

        model = FakeModel(script=[TextTurn("The answer is 42.")])
        agent = Agent(model=model, tools=[], callback_handler=None)
        result = await agent.invoke_async("What is the answer?")
        # The FakeModel emits the text directly; result.message should contain it.
        assert result is not None
        # The response text ends up in result.message content
        content_text = "".join(block.get("text", "") for block in result.message.get("content", []))
        assert "42" in content_text


# ---------------------------------------------------------------------------
# PathGuard tests
# ---------------------------------------------------------------------------


class TestPathGuard:
    def test_allows_file_inside_root(self, tmp_path: Path) -> None:
        from yukar.sandbox.path_guard import PathGuard

        guard = PathGuard(tmp_path)
        (tmp_path / "hello.txt").write_text("hi")
        resolved = guard.resolve("hello.txt")
        assert resolved == (tmp_path / "hello.txt").resolve()

    def test_allows_nested_path(self, tmp_path: Path) -> None:
        from yukar.sandbox.path_guard import PathGuard

        guard = PathGuard(tmp_path)
        (tmp_path / "a" / "b").mkdir(parents=True)
        resolved = guard.resolve("a/b")
        assert resolved.is_dir()

    def test_allows_root_itself(self, tmp_path: Path) -> None:
        from yukar.sandbox.path_guard import PathGuard

        guard = PathGuard(tmp_path)
        resolved = guard.resolve(".")
        assert resolved == tmp_path.resolve()

    def test_rejects_dotdot_escape(self, tmp_path: Path) -> None:
        from yukar.sandbox.path_guard import PathGuard, PathGuardError

        inner = tmp_path / "inner"
        inner.mkdir()
        guard = PathGuard(inner)
        with pytest.raises(PathGuardError):
            guard.resolve("../outside.txt")

    def test_rejects_absolute_escape(self, tmp_path: Path) -> None:
        from yukar.sandbox.path_guard import PathGuard, PathGuardError

        guard = PathGuard(tmp_path)
        with pytest.raises(PathGuardError):
            guard.resolve("/etc/passwd")

    def test_rejects_symlink_escape(self, tmp_path: Path) -> None:
        from yukar.sandbox.path_guard import PathGuard, PathGuardError

        inner = tmp_path / "sandbox"
        inner.mkdir()
        outside = tmp_path / "secret"
        outside.mkdir()
        (outside / "data.txt").write_text("secret")
        # Create a symlink inside the sandbox that points outside.
        link = inner / "escape"
        link.symlink_to(outside)
        guard = PathGuard(inner)
        with pytest.raises(PathGuardError):
            guard.resolve("escape/data.txt")

    def test_rejects_invalid_root(self, tmp_path: Path) -> None:
        from yukar.sandbox.path_guard import PathGuard

        with pytest.raises(ValueError):
            PathGuard(tmp_path / "nonexistent")

    def test_ignore_hook_blocks_path(self, tmp_path: Path) -> None:
        from yukar.sandbox.path_guard import PathGuard, PathGuardError

        (tmp_path / "blocked.txt").write_text("blocked")
        guard = PathGuard(tmp_path, ignore_fn=lambda p: p.name == "blocked.txt")
        with pytest.raises(PathGuardError):
            guard.resolve("blocked.txt")

    def test_ignore_hook_allows_path(self, tmp_path: Path) -> None:
        from yukar.sandbox.path_guard import PathGuard

        (tmp_path / "ok.txt").write_text("ok")
        guard = PathGuard(tmp_path, ignore_fn=lambda p: p.name == "blocked.txt")
        resolved = guard.resolve("ok.txt")
        assert resolved.name == "ok.txt"

    def test_check_cwd(self, tmp_path: Path) -> None:
        from yukar.sandbox.path_guard import PathGuard, PathGuardError

        inner = tmp_path / "wt"
        inner.mkdir()
        guard = PathGuard(inner)
        # Valid cwd
        valid = guard.check_cwd(".")
        assert valid == inner.resolve()
        # Invalid cwd
        with pytest.raises(PathGuardError):
            guard.check_cwd("..")


# ---------------------------------------------------------------------------
# Worktree tests
# ---------------------------------------------------------------------------


class TestWorktree:
    async def test_creates_new_worktree_and_branch(self, tmp_path: Path) -> None:
        from yukar.git.worktree import ensure_worktree

        repo = make_git_repo(tmp_path)
        wt_path = tmp_path / "wt" / "ep1"
        result = await ensure_worktree(repo, wt_path, "yukar/ep-1-test", "main")
        assert result.exists()
        assert (result / ".git").exists() or (result / "HEAD").exists()

    async def test_idempotent_when_already_exists(self, tmp_path: Path) -> None:
        from yukar.git.worktree import ensure_worktree

        repo = make_git_repo(tmp_path)
        wt_path = tmp_path / "wt" / "ep1"
        await ensure_worktree(repo, wt_path, "yukar/ep-1-test", "main")
        # Second call must not raise.
        result = await ensure_worktree(repo, wt_path, "yukar/ep-1-test", "main")
        assert result.exists()

    async def test_reuses_existing_branch(self, tmp_path: Path) -> None:
        from yukar.git.worktree import ensure_worktree

        repo = make_git_repo(tmp_path)
        branch = "yukar/ep-2-reuse"
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        }
        # Pre-create the branch in the repo.
        subprocess.run(
            ["git", "branch", branch],
            cwd=str(repo),
            check=True,
            env=env,
        )
        wt_path = tmp_path / "wt" / "ep2"
        result = await ensure_worktree(repo, wt_path, branch, "main")
        assert result.exists()

    async def test_remove_worktree(self, tmp_path: Path) -> None:
        from yukar.git.worktree import ensure_worktree, remove_worktree

        repo = make_git_repo(tmp_path)
        wt_path = tmp_path / "wt" / "ep-remove"
        await ensure_worktree(repo, wt_path, "yukar/ep-rm", "main")
        assert wt_path.exists()
        removed, error = await remove_worktree(repo, wt_path)
        assert removed is True, f"Expected removal to succeed, got error: {error}"
        assert error is None
        # After removal the directory is gone.
        assert not wt_path.exists()

    async def test_remove_nonexistent_is_noop(self, tmp_path: Path) -> None:
        from yukar.git.worktree import remove_worktree

        repo = make_git_repo(tmp_path)
        nonexistent = tmp_path / "wt" / "nothing"
        # Non-existent path is treated as already removed — returns (True, None).
        removed, error = await remove_worktree(repo, nonexistent)
        assert removed is True
        assert error is None


# ---------------------------------------------------------------------------
# fs tools tests
# ---------------------------------------------------------------------------


class TestFsTools:
    async def _make_ctx(self, worktree: Path) -> Any:
        from yukar.agents.context import AgentContext

        return await AgentContext.create(
            project_id="proj",
            epic_id="EP-1",
            repo_name="repo",
            worktree_path=worktree,
            workspace_root=str(worktree.parent),
        )

    async def test_fs_read_success(self, tmp_path: Path) -> None:
        from yukar.agents.tools.fs import make_fs_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "hello.txt").write_text("hello world")
        ctx = await self._make_ctx(wt)
        fs_read, _, _ = make_fs_tools(ctx)
        # @tool-decorated sync functions are called directly (not awaited)
        result = fs_read(path="hello.txt")
        assert result["status"] == "success"
        assert "hello world" in result["content"][0]["text"]

    async def test_fs_read_outside_worktree_rejected(self, tmp_path: Path) -> None:
        from yukar.agents.tools.fs import make_fs_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt)
        fs_read, _, _ = make_fs_tools(ctx)
        result = fs_read(path="../outside.txt")
        assert result["status"] == "error"

    async def test_fs_write_creates_file(self, tmp_path: Path) -> None:
        from yukar.agents.tools.fs import make_fs_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt)
        _, fs_write, _ = make_fs_tools(ctx)
        result = fs_write(path="new_file.py", content="print('hi')")
        assert result["status"] == "success"
        assert (wt / "new_file.py").read_text() == "print('hi')"

    async def test_fs_write_creates_parents(self, tmp_path: Path) -> None:
        from yukar.agents.tools.fs import make_fs_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt)
        _, fs_write, _ = make_fs_tools(ctx)
        result = fs_write(path="a/b/c/file.txt", content="nested")
        assert result["status"] == "success"
        assert (wt / "a" / "b" / "c" / "file.txt").read_text() == "nested"

    async def test_fs_write_outside_worktree_rejected(self, tmp_path: Path) -> None:
        from yukar.agents.tools.fs import make_fs_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt)
        _, fs_write, _ = make_fs_tools(ctx)
        result = fs_write(path="../evil.sh", content="rm -rf /")
        assert result["status"] == "error"

    async def test_fs_list_success(self, tmp_path: Path) -> None:
        from yukar.agents.tools.fs import make_fs_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "a.py").write_text("")
        (wt / "b.py").write_text("")
        ctx = await self._make_ctx(wt)
        _, _, fs_list = make_fs_tools(ctx)
        result = fs_list(path=".")
        assert result["status"] == "success"
        assert "a.py" in result["entries"]
        assert "b.py" in result["entries"]

    async def test_fs_list_outside_worktree_rejected(self, tmp_path: Path) -> None:
        from yukar.agents.tools.fs import make_fs_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt)
        _, _, fs_list = make_fs_tools(ctx)
        result = fs_list(path="..")
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# run_command tests
# ---------------------------------------------------------------------------


class TestRunCommand:
    async def _make_ctx(self, worktree: Path, allow: list[str], deny: list[str]) -> Any:
        from yukar.agents.context import AgentContext

        return await AgentContext.create(
            project_id="proj",
            epic_id="EP-1",
            repo_name="repo",
            worktree_path=worktree,
            workspace_root=str(worktree.parent),
            allow=allow,
            deny=deny,
        )

    async def test_allowed_command_succeeds(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt, allow=["echo"], deny=[])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="echo hello")
        assert result["status"] == "success"
        assert "hello" in result["stdout"]

    async def test_denied_command_rejected(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt, allow=["echo"], deny=["rm"])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="rm -rf /tmp/x")
        assert result["status"] == "error"
        assert "not permitted" in result["content"][0]["text"]

    async def test_not_in_allow_list_rejected(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt, allow=["pytest"], deny=[])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="curl http://evil.com")
        assert result["status"] == "error"

    async def test_empty_allow_denies_all(self, tmp_path: Path) -> None:
        """Empty allow list is deny-all (fail-safe explicit-allowlist policy)."""
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt, allow=[], deny=[])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="echo open")
        # Empty allow → all commands denied.
        assert result["status"] == "error"
        assert "not permitted" in result["content"][0]["text"]

    async def test_cwd_outside_worktree_rejected(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        # allow=[] → all commands denied; but cwd is checked first → cwd error.
        ctx = await self._make_ctx(wt, allow=[], deny=[])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="echo hi", cwd="..")
        assert result["status"] == "error"
        assert "cwd" in result["content"][0]["text"].lower()

    async def test_timeout_kills_process(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt, allow=["sleep"], deny=[])
        (run_command,) = make_command_tools(ctx, timeout=0.5)
        result = await run_command(command="sleep 10")
        assert result["status"] == "error"
        assert "timed out" in result["content"][0]["text"].lower()

    async def test_cwd_inside_worktree_allowed(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        subdir = wt / "src"
        subdir.mkdir(parents=True)
        ctx = await self._make_ctx(wt, allow=["echo"], deny=[])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="echo inside", cwd="src")
        assert result["status"] == "success"


# ---------------------------------------------------------------------------
# git tools tests
# ---------------------------------------------------------------------------


class TestGitTools:
    def _setup_worktree_repo(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create a main repo and a worktree on a branch."""
        repo = make_git_repo(tmp_path, "main-repo")
        branch = "yukar/ep-git-test"
        wt_path = tmp_path / "wt" / "git-test"
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        }
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), "-b", branch, "main"],
            cwd=str(repo),
            check=True,
            env=env,
        )
        return repo, wt_path

    async def _make_ctx(self, worktree: Path) -> Any:
        from yukar.agents.context import AgentContext

        return await AgentContext.create(
            project_id="proj",
            epic_id="EP-git",
            repo_name="repo",
            worktree_path=worktree,
            workspace_root=str(worktree.parent),
        )

    async def test_git_status_clean(self, tmp_path: Path) -> None:
        from yukar.agents.tools.git_tools import make_git_tools

        _, wt = self._setup_worktree_repo(tmp_path)
        ctx = await self._make_ctx(wt)
        git_status, _, _, _ = make_git_tools(ctx)
        result = await git_status()
        assert result["status"] == "success"
        assert "clean" in result["output"].lower() or result["output"] == "(clean)"

    async def test_git_status_with_changes(self, tmp_path: Path) -> None:
        from yukar.agents.tools.git_tools import make_git_tools

        _, wt = self._setup_worktree_repo(tmp_path)
        (wt / "new_file.py").write_text("# new")
        ctx = await self._make_ctx(wt)
        git_status, _, _, _ = make_git_tools(ctx)
        result = await git_status()
        assert result["status"] == "success"
        assert "new_file.py" in result["output"]

    async def test_git_diff_after_change(self, tmp_path: Path) -> None:
        from yukar.agents.tools.git_tools import make_git_tools

        _, wt = self._setup_worktree_repo(tmp_path)
        (wt / "README.md").write_text("# modified\n")
        ctx = await self._make_ctx(wt)
        _, git_diff, _, _ = make_git_tools(ctx)
        result = await git_diff()
        assert result["status"] == "success"

    async def test_git_commit_creates_commit_on_epic_branch(self, tmp_path: Path) -> None:
        from yukar.agents.tools.git_tools import make_git_tools

        repo, wt = self._setup_worktree_repo(tmp_path)
        (wt / "work.py").write_text("x = 1\n")
        ctx = await self._make_ctx(wt)
        _, _, git_add, git_commit = make_git_tools(ctx)
        add_result = await git_add(paths="work.py")
        assert add_result["status"] == "success"
        commit_result = await git_commit(message="test: add work.py")
        assert commit_result["status"] == "success"

        # Verify the commit exists on the epic branch in the worktree
        env = {**os.environ}
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=str(wt),
            capture_output=True,
            text=True,
            env=env,
        )
        assert "test: add work.py" in log.stdout

        # Verify main is not affected
        main_log = subprocess.run(
            ["git", "log", "--oneline", "-1", "main"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            env=env,
        )
        assert "test: add work.py" not in main_log.stdout

    async def test_git_commit_no_staged_fails(self, tmp_path: Path) -> None:
        from yukar.agents.tools.git_tools import make_git_tools

        _, wt = self._setup_worktree_repo(tmp_path)
        ctx = await self._make_ctx(wt)
        _, _, _, git_commit = make_git_tools(ctx)
        # Nothing staged → commit should fail
        result = await git_commit(message="empty commit")
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Evaluator tools tests
# ---------------------------------------------------------------------------


class TestEvaluatorTools:
    async def _make_ctx(self, worktree: Path, allow: list[str] | None = None) -> Any:
        from yukar.agents.context import AgentContext

        return await AgentContext.create(
            project_id="proj",
            epic_id="EP-eval",
            repo_name="repo",
            worktree_path=worktree,
            workspace_root=str(worktree.parent),
            allow=allow or [],
            deny=[],
        )

    def _setup_wt(self, tmp_path: Path) -> tuple[Path, Path]:
        repo = make_git_repo(tmp_path)
        wt_path = tmp_path / "wt"
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        }
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), "-b", "yukar/ep-eval", "main"],
            cwd=str(repo),
            check=True,
            env=env,
        )
        return repo, wt_path

    async def test_read_diff_clean(self, tmp_path: Path) -> None:
        from yukar.agents.tools.evaluator_tools import make_evaluator_tools

        _, wt = self._setup_wt(tmp_path)
        ctx = await self._make_ctx(wt)
        read_diff, _ = make_evaluator_tools(ctx)
        result = await read_diff()
        assert result["status"] == "success"

    async def test_run_tests_allowed(self, tmp_path: Path) -> None:
        from yukar.agents.tools.evaluator_tools import make_evaluator_tools

        _, wt = self._setup_wt(tmp_path)
        ctx = await self._make_ctx(wt, allow=["echo"])
        _, run_tests = make_evaluator_tools(ctx)
        result = await run_tests(command="echo test output")
        assert result["status"] == "success"
        assert "test output" in result["stdout"]

    async def test_run_tests_deny_blocked(self, tmp_path: Path) -> None:
        from yukar.agents.context import AgentContext
        from yukar.agents.tools.evaluator_tools import make_evaluator_tools

        _, wt = self._setup_wt(tmp_path)
        ctx = await AgentContext.create(
            project_id="proj",
            epic_id="EP-eval",
            repo_name="repo",
            worktree_path=wt,
            workspace_root=str(wt.parent),
            allow=["pytest"],
            deny=["curl"],
        )
        _, run_tests = make_evaluator_tools(ctx)
        result = await run_tests(command="curl http://evil.com")
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# session_store ↔ Strands FileSessionManager compatibility
# ---------------------------------------------------------------------------


class TestSessionStoreStrandsCompat:
    """Verify that files written by session_store can be read by Strands and vice-versa."""

    async def test_ensure_session_strands_readable(self, tmp_workspace: Path) -> None:
        """session_store.ensure_session → Strands FileSessionManager.read_session."""
        from strands.session.file_session_manager import FileSessionManager

        from yukar.storage.session_store import ensure_session

        root = str(tmp_workspace)
        project_id = "compat-proj"
        epic_id = "EP-compat"
        await ensure_session(root, project_id, epic_id)

        # Strands sessions_dir is the directory containing session_<id>/
        from yukar.config import paths

        sessions_dir = str(paths.sessions_dir(root, project_id, epic_id))
        mgr = FileSessionManager(session_id=epic_id, storage_dir=sessions_dir)
        session = mgr.read_session(epic_id)
        assert session is not None
        assert session.session_id == epic_id

    async def test_ensure_agent_strands_readable(self, tmp_workspace: Path) -> None:
        """session_store.ensure_agent → Strands FileSessionManager.read_agent."""
        from strands.session.file_session_manager import FileSessionManager

        from yukar.config import paths
        from yukar.storage.session_store import ensure_agent, ensure_session

        root = str(tmp_workspace)
        project_id = "compat-proj"
        epic_id = "EP-compat-agent"
        agent_id = "manager"
        await ensure_session(root, project_id, epic_id)
        await ensure_agent(root, project_id, epic_id, agent_id, {"role": "manager"})

        sessions_dir = str(paths.sessions_dir(root, project_id, epic_id))
        mgr = FileSessionManager(session_id=epic_id, storage_dir=sessions_dir)
        agent = mgr.read_agent(epic_id, agent_id)
        assert agent is not None
        assert agent.agent_id == agent_id
        assert agent.state.get("role") == "manager"

    async def test_message_strands_readable(self, tmp_workspace: Path) -> None:
        """session_store.append_message → Strands FileSessionManager.read_message."""
        from strands.session.file_session_manager import FileSessionManager

        from yukar.config import paths
        from yukar.storage.session_store import append_message, ensure_agent, ensure_session

        root = str(tmp_workspace)
        project_id = "compat-proj"
        epic_id = "EP-compat-msg"
        agent_id = "th-001"
        await ensure_session(root, project_id, epic_id)
        await ensure_agent(root, project_id, epic_id, agent_id)
        await append_message(root, project_id, epic_id, agent_id, "user", "Hello Strands")

        sessions_dir = str(paths.sessions_dir(root, project_id, epic_id))
        mgr = FileSessionManager(session_id=epic_id, storage_dir=sessions_dir)
        msgs = mgr.list_messages(epic_id, agent_id)
        assert len(msgs) == 1
        # The message content should be readable.
        msg = msgs[0].to_message()
        texts = [block.get("text", "") for block in msg.get("content", [])]
        assert "Hello Strands" in "".join(texts)

    async def test_strands_written_session_store_readable(self, tmp_workspace: Path) -> None:
        """Strands FileSessionManager.create_session → session_store can read it.

        A fresh project/epic directory is used so Strands creates its own layout
        and our session_store reads it back without having written anything first.
        """
        import json

        from strands.session.file_session_manager import FileSessionManager
        from strands.types.session import SessionAgent, SessionMessage

        from yukar.config import paths
        from yukar.storage.session_store import list_messages

        root = str(tmp_workspace)
        # Use a *different* project/epic so ensure_session was never called here.
        project_id = "strands-writes-proj"
        epic_id = "EP-sw"
        agent_id = "worker-1"

        # sessions_dir is the parent for Strands' storage_dir.
        sessions_dir_path = paths.sessions_dir(root, project_id, epic_id)
        sessions_dir_path.mkdir(parents=True, exist_ok=True)
        sessions_dir = str(sessions_dir_path)

        # FileSessionManager.__init__ automatically calls create_session if the
        # session does not exist yet (via RepositorySessionManager.__init__).
        mgr = FileSessionManager(session_id=epic_id, storage_dir=sessions_dir)

        session_agent = SessionAgent(
            agent_id=agent_id,
            state={"role": "worker"},
            conversation_manager_state={},
        )
        mgr.create_agent(epic_id, session_agent)

        sm = SessionMessage.from_message(
            message={"role": "user", "content": [{"text": "hi from strands"}]},
            index=0,
        )
        mgr.create_message(epic_id, agent_id, sm)

        # Verify agent.json state directly (get_agent_state removed — dead code).
        a_json = paths.agent_json(root, project_id, epic_id, agent_id)
        raw_agent = json.loads(a_json.read_text(encoding="utf-8"))
        assert raw_agent["state"].get("role") == "worker"

        # Verify agent directory is present (list_agents removed — dead code).
        a_dir = paths.agent_dir(root, project_id, epic_id, agent_id)
        assert a_dir.exists()

        messages = list_messages(root, project_id, epic_id, agent_id)
        assert len(messages) == 1
        assert messages[0].message.content[0].text == "hi from strands"


# ---------------------------------------------------------------------------
# Review fix #1 — run_command allow/deny hardening
# ---------------------------------------------------------------------------


class TestRunCommandHardening:
    """Tests for basename normalization, default-deny, git argv guard."""

    async def _make_ctx(self, worktree: Path, allow: list[str], deny: list[str]) -> Any:
        from yukar.agents.context import AgentContext

        return await AgentContext.create(
            project_id="proj",
            epic_id="EP-hard",
            repo_name="repo",
            worktree_path=worktree,
            workspace_root=str(worktree.parent),
            allow=allow,
            deny=deny,
        )

    async def test_absolute_path_rm_denied_via_basename(self, tmp_path: Path) -> None:
        """/bin/rm must be caught by deny=['rm'] via basename normalization."""
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt, allow=[], deny=["rm"])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="/bin/rm -rf /tmp/x")
        assert result["status"] == "error"
        assert "not permitted" in result["content"][0]["text"]

    async def test_absolute_path_allowed_via_basename(self, tmp_path: Path) -> None:
        """/bin/echo should be allowed when 'echo' is in the allow list."""
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt, allow=["echo"], deny=[])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="/bin/echo hello")
        assert result["status"] == "success"
        assert "hello" in result["stdout"]

    async def test_env_rm_denied_when_env_not_allowed(self, tmp_path: Path) -> None:
        """'env rm' must be rejected when 'env' is not in the allow list.

        Shell wrappers (env, sh, bash) receive no special treatment.
        They are subject to the same allowlist rules as any other command.
        """
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        # allow=['echo'] but NOT 'env' → 'env rm ...' rejected
        ctx = await self._make_ctx(wt, allow=["echo"], deny=[])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="env rm -rf /tmp/x")
        assert result["status"] == "error"
        assert "not permitted" in result["content"][0]["text"]

    async def test_git_dash_capital_c_rejected(self, tmp_path: Path) -> None:
        """git -C /etc status must be blocked (worktree scope escape)."""
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt, allow=["git"], deny=[])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="git -C /etc status")
        assert result["status"] == "error"
        assert "not permitted" in result["content"][0]["text"]

    async def test_git_git_dir_flag_rejected(self, tmp_path: Path) -> None:
        """git --git-dir=... must be blocked."""
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt, allow=["git"], deny=[])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="git --git-dir=/etc/evil status")
        assert result["status"] == "error"
        assert "not permitted" in result["content"][0]["text"]

    async def test_git_work_tree_flag_rejected(self, tmp_path: Path) -> None:
        """git --work-tree=... must be blocked."""
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt, allow=["git"], deny=[])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="git --work-tree=/tmp status")
        assert result["status"] == "error"
        assert "not permitted" in result["content"][0]["text"]

    async def test_git_exec_path_flag_rejected(self, tmp_path: Path) -> None:
        """git --exec-path=... must be blocked."""
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt, allow=["git"], deny=[])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="git --exec-path=/evil status")
        assert result["status"] == "error"
        assert "not permitted" in result["content"][0]["text"]

    async def test_git_any_subcommand_denied_via_run_command(self, tmp_path: Path) -> None:
        """'git status' must be denied via run_command; use run_git instead.

        git is unconditionally denied by run_command's baseline, even for local
        subcommands.  All git operations must go through the dedicated git tools.
        """
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt, allow=["git"], deny=[])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="git status")
        assert result["status"] == "error"
        assert "baseline" in result["content"][0]["text"]
        assert "run_git" in result["content"][0]["text"]

    async def test_empty_allow_denies_everything(self, tmp_path: Path) -> None:
        """Empty allow list blocks all commands (fail-safe default)."""
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt, allow=[], deny=[])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="echo hi")
        assert result["status"] == "error"
        assert "not permitted" in result["content"][0]["text"]

    async def test_run_tests_via_evaluator_inherits_allow_deny(self, tmp_path: Path) -> None:
        """run_tests delegates to run_command → allow/deny hardening applies automatically."""
        import os
        import subprocess

        from yukar.agents.context import AgentContext
        from yukar.agents.tools.evaluator_tools import make_evaluator_tools

        repo = tmp_path / "repo"
        repo.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "t@t.com",
        }
        subprocess.run(
            ["git", "init", "-b", "main"], cwd=str(repo), env=env, check=True, capture_output=True
        )
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=str(repo), env=env, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=str(repo), env=env, check=True, capture_output=True
        )

        wt_path = tmp_path / "wt"
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), "-b", "yukar/ep", "main"],
            cwd=str(repo),
            env=env,
            check=True,
            capture_output=True,
        )

        # Evaluator with empty allow — run_tests must be blocked.
        ctx = await AgentContext.create(
            project_id="proj",
            epic_id="EP-eval",
            repo_name="repo",
            worktree_path=wt_path,
            workspace_root=str(tmp_path),
            allow=[],  # deny-all
            deny=[],
        )
        _, run_tests = make_evaluator_tools(ctx)
        result = await run_tests(command="/bin/rm -rf /tmp")
        assert result["status"] == "error"
        assert "not permitted" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Review fix #5 — git_add paths through PathGuard
# ---------------------------------------------------------------------------


class TestGitAddPathGuard:
    """git_add must reject paths that escape the worktree."""

    async def _setup(self, tmp_path: Path) -> tuple[Path, Any]:
        import os
        import subprocess

        from yukar.agents.context import AgentContext

        repo = tmp_path / "repo"
        repo.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "t@t.com",
        }

        def g(*args: str) -> None:
            subprocess.run(["git", *args], cwd=str(repo), env=env, check=True, capture_output=True)

        g("init", "-b", "main")
        g("config", "user.email", "t@t.com")
        g("config", "user.name", "Test")
        (repo / "README.md").write_text("# r\n")
        g("add", ".")
        g("commit", "-m", "init")

        wt_path = tmp_path / "wt"
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), "-b", "yukar/ep-add-test", "main"],
            cwd=str(repo),
            env=env,
            check=True,
            capture_output=True,
        )

        ctx = await AgentContext.create(
            project_id="proj",
            epic_id="EP-add",
            repo_name="repo",
            worktree_path=wt_path,
            workspace_root=str(tmp_path),
        )
        return wt_path, ctx

    async def test_git_add_inside_worktree_succeeds(self, tmp_path: Path) -> None:
        from yukar.agents.tools.git_tools import make_git_tools

        wt, ctx = await self._setup(tmp_path)
        (wt / "new.py").write_text("x = 1\n")
        _, _, git_add, _ = make_git_tools(ctx)
        result = await git_add(paths="new.py")
        assert result["status"] == "success"

    async def test_git_add_dotdot_escape_rejected(self, tmp_path: Path) -> None:
        """git add ../outside.txt must be blocked by PathGuard."""
        from yukar.agents.tools.git_tools import make_git_tools

        wt, ctx = await self._setup(tmp_path)
        _, _, git_add, _ = make_git_tools(ctx)
        result = await git_add(paths="../secret.txt")
        assert result["status"] == "error"
        assert "path error" in result["content"][0]["text"].lower()

    async def test_git_add_absolute_outside_rejected(self, tmp_path: Path) -> None:
        """git add /etc/passwd must be blocked by PathGuard."""
        from yukar.agents.tools.git_tools import make_git_tools

        wt, ctx = await self._setup(tmp_path)
        _, _, git_add, _ = make_git_tools(ctx)
        result = await git_add(paths="/etc/passwd")
        assert result["status"] == "error"
        assert "path error" in result["content"][0]["text"].lower()


# ---------------------------------------------------------------------------
# Review fix #4 — ToolCallEvent / ToolResultEvent carry tool_use_id
# (tests updated to use real message-kwargs interface, not synthetic tool_use_stream)
# ---------------------------------------------------------------------------


class TestStreamTranslatorToolUseId:
    """Verify tool_use_id is propagated from Strands message events to RunEvents.

    All tests use the real Strands callback interface: {"message": <Message>}
    as verified by probe.  Synthetic "type==tool_use_stream" kwargs are NOT
    used because that event carries a str input and is not authoritative.
    """

    async def test_tool_call_carries_tool_use_id(self) -> None:
        """ToolCallEvent must include the toolUseId from the assistant message."""
        import asyncio

        from yukar.agents.streaming import StreamTranslator
        from yukar.events import bus as event_bus
        from yukar.models.events import ToolCallEvent

        translator = StreamTranslator(project_id="p", epic_id="e", run_id="r", thread_id="t")

        received: list[Any] = []
        async with event_bus.subscribe("p", "e") as q:
            # Real Strands interface: assistant message with toolUse block.
            translator.callback(
                message={
                    "role": "assistant",
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "tuid-abc123",
                                "name": "fs_read",
                                "input": {"path": "x.py"},
                            }
                        }
                    ],
                }
            )
            try:
                event = await asyncio.wait_for(q.get(), timeout=0.5)
                received.append(event)
            except TimeoutError:
                pass

        assert len(received) == 1
        ev = received[0]
        assert isinstance(ev, ToolCallEvent)
        assert ev.tool_use_id == "tuid-abc123"
        assert ev.tool_name == "fs_read"
        assert ev.tool_input == {"path": "x.py"}

    async def test_tool_result_carries_tool_use_id_and_real_name(self) -> None:
        """ToolResultEvent must include tool_use_id and look up the real tool name."""
        import asyncio

        from yukar.agents.streaming import StreamTranslator
        from yukar.events import bus as event_bus
        from yukar.models.events import ToolResultEvent

        translator = StreamTranslator(project_id="p", epic_id="e2", run_id="r", thread_id="t")

        # Register id→name via assistant message (real interface).
        translator.callback(
            message={
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "tuid-xyz789",
                            "name": "git_commit",
                            "input": {"message": "Add file"},
                        }
                    }
                ],
            }
        )

        received: list[Any] = []
        async with event_bus.subscribe("p", "e2") as q:
            # Drain the ToolCallEvent that was already published.
            import contextlib

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.1)

            # Real Strands interface: user message with toolResult block.
            translator.callback(
                message={
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": "tuid-xyz789",
                                "status": "success",
                                "content": [{"text": "Committed."}],
                            }
                        }
                    ],
                }
            )
            try:
                event = await asyncio.wait_for(q.get(), timeout=0.5)
                received.append(event)
            except TimeoutError:
                pass

        assert len(received) == 1
        ev = received[0]
        assert isinstance(ev, ToolResultEvent)
        assert ev.tool_use_id == "tuid-xyz789"
        # Real name looked up from the call-side map.
        assert ev.tool_name == "git_commit"
        assert ev.result == "Committed."

    async def test_tool_use_stream_does_not_publish(self) -> None:
        """tool_use_stream kwargs must NOT publish any event.

        Strands fires this once with current_tool_use.input as a str (partial
        JSON).  The authoritative tool data arrives later via message kwargs.
        Reacting to tool_use_stream would either miss data or publish with
        incomplete input.
        """
        import asyncio

        from yukar.agents.streaming import StreamTranslator
        from yukar.events import bus as event_bus

        translator = StreamTranslator(project_id="p", epic_id="e3", run_id="r", thread_id="t")

        async with event_bus.subscribe("p", "e3") as q:
            # This is the actual Strands event shape — input is always a str here.
            translator.callback(
                **{
                    "type": "tool_use_stream",
                    "current_tool_use": {
                        "name": "fs_read",
                        "toolUseId": "partial-id",
                        "input": '{"path": "x.py"}',  # str, not dict
                    },
                }
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.1)


# ---------------------------------------------------------------------------
# Task C — Baseline denylist (always-on, operator allow cannot override)
# ---------------------------------------------------------------------------


class TestDefaultDenylist:
    """Tests for check_default_denylist — pure-function, no subprocess.

    All DENY cases must return a non-None string.
    All ALLOW cases must return None.
    """

    def _check(self, command: str) -> str | None:
        """Helper: split command and call check_default_denylist."""
        import shlex

        from yukar.agents.tools.command import check_default_denylist

        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        return check_default_denylist(tokens)

    # -----------------------------------------------------------------------
    # DENY cases
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf /",
            "rm -rf //",
            "rm -rf /.",
            "rm -rf /*",
            "rm -fr /",
            "rm -Rf /etc",
            "rm -rf /usr",
            "rm -rf ~",
            "rm -rf ~/",
            "rm -rf $HOME",
            "/bin/rm -rf /",
            "rm --no-preserve-root -rf /",
            "dd if=/dev/zero of=/dev/sda",
            "mkfs.ext4 /dev/sdb",
            "mkfs /dev/sdb",
            "chmod -R 777 /",
            "chown -R root /etc",
            "shutdown -h now",
            "reboot",
            "poweroff",
            "init 0",
            "init 6",
            "git push",
            "git push --force origin main",
            "git -c x=y push",
            "git -C /tmp push",
            "git -c a=b -c c=d push",
            "git --git-dir=/x push",
            "git clone https://x/y",
            "git fetch",
            "git pull",
            # P2 desync: --attr-source is a value-consuming option; walker must
            # consume the next token as its value and find 'fetch' as subcommand.
            "git --attr-source log fetch origin main",
            "git --attr-source x push",
            # Unrecognised global option: must be denied fail-safe.
            "git --totally-unknown-global x fetch",
            "git pull origin main",
            "git remote -v",
            "git remote add origin https://x/y",
            "git submodule update",
            "git submodule update --init --remote",
            "git lfs pull",
            "git lfs install",
            # P2 plumbing denial — transport plumbing not in local allowlist
            "git ls-remote https://x/y",
            "git fetch-pack x",
            "git send-pack x",
            "git upload-pack .",
            "git receive-pack .",
            "git http-fetch x",
            "git daemon",
            "git send-email x",
            "git imap-send",
            "git archive --remote=x",
            "git bundle create b HEAD",
            "git request-pull a b c",
            "git svn clone x",
            "git p4 sync",
            "git maintenance start",
            "sudo rm -rf /",
            "env rm -rf /",
            "env VAR=1 rm -rf /",
            "sudo -u root rm -rf /",
            "xargs rm -rf /",
            "timeout 5 rm -rf /",
            "sudo reboot",
            'bash -c "rm -rf /"',
            'sh -c "rm -rf /"',
            'bash -lc "rm -rf /"',
            'sh -ce "shutdown -h now"',
            'bash --login -c "git push"',
        ],
    )
    def test_deny(self, command: str) -> None:
        result = self._check(command)
        assert result is not None, f"Expected DENY for {command!r}, but got None"

    # -----------------------------------------------------------------------
    # ALLOW cases (must return None — NOT denied by baseline)
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize(
        "command",
        [
            "rm file.txt",
            "rm -rf build/",
            "rm -rf ./node_modules",
            "rm -rf dist",
            "rm -rf src/generated",
            "chmod 777 file.sh",
            "chmod +x s.sh",
            "chmod -R 755 build/",
            "dd if=in.bin of=out.bin",
            "init 3",
            "pytest",
            "npm test",
            "sudo -u rm pytest",
            "timeout 5 pytest",
            'bash -lc "pytest"',
            'bash -c "echo hi"',
            "sh -e script.sh",
        ],
    )
    def test_allow(self, command: str) -> None:
        result = self._check(command)
        assert result is None, f"Expected ALLOW for {command!r}, but got: {result!r}"

    # -----------------------------------------------------------------------
    # DENY cases: git is unconditionally denied regardless of subcommand
    # (git must use run_git, not run_command)
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize(
        "command",
        [
            # Previously-allowed local subcommands are now also denied.
            'git commit -m "fix push bug"',
            "git log",
            "git -c user.name=x commit -m y",
            "git rev-parse HEAD",
            "git show HEAD",
            "git diff --cached",
            "git checkout -b feature",
            "git add .",
            "git stash",
            "git branch -a",
            "git rebase main",
            "git merge feature",
            "git config user.email x@y",
            "git --version",
            "git --attr-source=HEAD log",
            "git -p log",
            "git --no-pager status",
            # Wrapper form containing git is also denied.
            "env EDITOR=vim git commit",
        ],
    )
    def test_deny_git_unconditional(self, command: str) -> None:
        """All git invocations must be denied by run_command's baseline."""
        result = self._check(command)
        assert result is not None, f"Expected DENY (git unconditional) for {command!r}, got None"
        assert "run_git" in result or "baseline" in result

    # -----------------------------------------------------------------------
    # Recursive / nested-shell DENY cases (defence-in-depth Layer 2+3)
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize(
        "command",
        [
            # Layer 3 → Layer 1: inner command is directly dangerous.
            'bash -c "sudo rm -rf /"',
            'sh -c "env rm -rf /"',
            "bash -c \"bash -c 'rm -rf /'\"",
            # Layer 2 → Layer 3: wrapper contains a shell with -c.
            'sudo bash -c "rm -rf /"',
            # Layer 3 → Layer 3: nested shells.
            "bash -c \"sh -ce 'shutdown -h now'\"",
            # Layer 2 → Layer 3: xargs wraps a shell script.
            'xargs sh -c "git push"',
            # New git remote subcommands via shell -c and wrapper.
            'sh -c "git fetch"',
            "env git pull",
            # P2 plumbing via shell -c and env wrapper.
            'sh -c "git ls-remote https://x"',
            "env git fetch-pack x",
            # P2 desync via shell -c: --attr-source is value-consuming, real subcommand is fetch.
            'sh -c "git --attr-source log fetch"',
            # Round-4/5: wrapper/shell git deny regression.
            # git is unconditionally denied; the recursive walker reaches the
            # git baseline-deny whether git is the top-level command or nested.
            "env git -C /etc status",
            'sh -c "git --exec-path=/evil status"',
            "env git --git-dir=/other/.git status",
            "xargs git --work-tree=/tmp status",
            'bash -lc "git -C /etc log"',
        ],
    )
    def test_deny_recursive(self, command: str) -> None:
        """Nested wrapper/shell patterns must be denied by recursive evaluation."""
        result = self._check(command)
        assert result is not None, f"Expected DENY (recursive) for {command!r}, but got None"

    # -----------------------------------------------------------------------
    # Round-4 / Round-5: baseline-level unit asserts for git via
    # check_default_denylist.  git is now unconditionally denied — any argv
    # whose basename is 'git' is blocked regardless of subcommand or flags.
    # -----------------------------------------------------------------------

    def test_baseline_denies_exec_path_top_level(self) -> None:
        """check_default_denylist must block git --exec-path= even at top level."""
        from yukar.agents.tools.command import check_default_denylist

        assert check_default_denylist(["git", "--exec-path=/x", "status"]) is not None

    def test_baseline_denies_dash_capital_c_top_level(self) -> None:
        """check_default_denylist must block git -C /etc even at top level."""
        from yukar.agents.tools.command import check_default_denylist

        assert check_default_denylist(["git", "-C", "/etc", "status"]) is not None

    def test_baseline_denies_git_dir_top_level(self) -> None:
        """check_default_denylist must block git --git-dir= even at top level."""
        from yukar.agents.tools.command import check_default_denylist

        assert check_default_denylist(["git", "--git-dir=/x", ".", "status"]) is not None

    # -----------------------------------------------------------------------
    # Recursive false-positive check: benign nested patterns must still PASS
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize(
        "command",
        [
            'bash -c "echo hi"',
            'bash -lc "pytest"',
            "sh -e script.sh",
            "sudo -u rm pytest",
            'bash -c "cat file.txt"',
        ],
    )
    def test_allow_recursive_no_false_positives(self, command: str) -> None:
        """Benign commands must NOT be accidentally denied by recursive checks."""
        result = self._check(command)
        assert result is None, (
            f"Expected ALLOW (recursive, no false positive) for {command!r}, but got: {result!r}"
        )

    # -----------------------------------------------------------------------
    # Confirm baseline fires even when operator allow list includes the command
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_baseline_deny_ignores_operator_allowlist(self, tmp_path: Path) -> None:
        """rm/git/shutdown in allowlist still blocked by baseline — allow cannot override."""
        from yukar.agents.context import AgentContext
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()

        # Even with rm, git, shutdown in the allow list, baseline must block them.
        ctx = await AgentContext.create(
            project_id="proj",
            epic_id="EP-1",
            repo_name="repo",
            worktree_path=wt,
            workspace_root=str(wt.parent),
            allow=["rm", "git", "shutdown", "reboot"],
            deny=[],
        )
        (run_command,) = make_command_tools(ctx)

        rm_result = await run_command(command="rm -rf /")
        assert rm_result["status"] == "error"
        assert "baseline" in rm_result["content"][0]["text"]

        reboot_result = await run_command(command="reboot")
        assert reboot_result["status"] == "error"
        assert "baseline" in reboot_result["content"][0]["text"]

        push_result = await run_command(command="git push")
        assert push_result["status"] == "error"
        assert "baseline" in push_result["content"][0]["text"]
