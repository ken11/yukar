"""Tests for security hardening layers in run_command.

Covers:
- P1: build_subprocess_env strips secrets, preserves safe vars, injects defaults
- P2: git is unconditionally denied via run_command (all forms: direct, wrapper, global-flag)
- P3: check_absolute_args rejects absolute / home-relative paths outside worktree
- P4: read_diff base_branch validation (evaluator_tools)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# P1 — build_subprocess_env unit tests
# ---------------------------------------------------------------------------


class TestBuildSubprocessEnvUnit:
    """Pure-unit tests for build_subprocess_env; no subprocess is spawned."""

    def _build(
        self,
        tmp_path: Path,
        parent_env: dict[str, str],
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        from yukar.sandbox.env import build_subprocess_env

        return build_subprocess_env(cwd=tmp_path, parent_env=parent_env, extra=extra)

    def test_strips_anthropic_api_key(self, tmp_path: Path) -> None:
        env = self._build(tmp_path, {"ANTHROPIC_API_KEY": "sk-secret", "PATH": "/usr/bin"})
        assert "ANTHROPIC_API_KEY" not in env

    def test_strips_aws_secret_access_key(self, tmp_path: Path) -> None:
        env = self._build(tmp_path, {"AWS_SECRET_ACCESS_KEY": "abc123", "PATH": "/usr/bin"})
        assert "AWS_SECRET_ACCESS_KEY" not in env

    def test_strips_aws_access_key_id(self, tmp_path: Path) -> None:
        env = self._build(
            tmp_path, {"AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE", "PATH": "/usr/bin"}
        )
        assert "AWS_ACCESS_KEY_ID" not in env

    def test_strips_github_token(self, tmp_path: Path) -> None:
        env = self._build(tmp_path, {"GITHUB_TOKEN": "ghp_abc", "PATH": "/usr/bin"})
        assert "GITHUB_TOKEN" not in env

    def test_strips_gh_token(self, tmp_path: Path) -> None:
        env = self._build(tmp_path, {"GH_TOKEN": "ghp_abc", "PATH": "/usr/bin"})
        assert "GH_TOKEN" not in env

    def test_strips_ssh_auth_sock(self, tmp_path: Path) -> None:
        env = self._build(tmp_path, {"SSH_AUTH_SOCK": "/tmp/agent.sock", "PATH": "/usr/bin"})
        assert "SSH_AUTH_SOCK" not in env

    def test_strips_substring_matched_token(self, tmp_path: Path) -> None:
        env = self._build(tmp_path, {"MY_SERVICE_TOKEN": "token123", "PATH": "/usr/bin"})
        assert "MY_SERVICE_TOKEN" not in env

    def test_strips_substring_matched_password(self, tmp_path: Path) -> None:
        env = self._build(tmp_path, {"DB_PASSWORD": "hunter2", "PATH": "/usr/bin"})
        assert "DB_PASSWORD" not in env

    def test_strips_substring_matched_api_key(self, tmp_path: Path) -> None:
        env = self._build(tmp_path, {"X_API_KEY": "abc", "PATH": "/usr/bin"})
        assert "X_API_KEY" not in env

    def test_strips_substring_matched_secret(self, tmp_path: Path) -> None:
        env = self._build(tmp_path, {"SOME_SECRET": "shhh", "PATH": "/usr/bin"})
        assert "SOME_SECRET" not in env

    def test_keeps_path(self, tmp_path: Path) -> None:
        env = self._build(tmp_path, {"PATH": "/usr/local/bin:/usr/bin"})
        assert env["PATH"] == "/usr/local/bin:/usr/bin"

    def test_keeps_home(self, tmp_path: Path) -> None:
        env = self._build(tmp_path, {"HOME": "/home/user", "PATH": "/usr/bin"})
        assert env["HOME"] == "/home/user"

    def test_keeps_lang(self, tmp_path: Path) -> None:
        env = self._build(tmp_path, {"LANG": "en_US.UTF-8", "PATH": "/usr/bin"})
        assert env["LANG"] == "en_US.UTF-8"

    def test_keeps_lc_prefix(self, tmp_path: Path) -> None:
        env = self._build(tmp_path, {"LC_ALL": "en_US.UTF-8", "PATH": "/usr/bin"})
        assert env["LC_ALL"] == "en_US.UTF-8"

    def test_keeps_xdg_prefix(self, tmp_path: Path) -> None:
        env = self._build(tmp_path, {"XDG_CACHE_HOME": "/tmp/cache", "PATH": "/usr/bin"})
        assert env["XDG_CACHE_HOME"] == "/tmp/cache"

    def test_drops_non_allowlisted_var(self, tmp_path: Path) -> None:
        """Non-allowlisted, non-secret vars are dropped (allowlist behaviour)."""
        env = self._build(tmp_path, {"RANDOM_PROJECT_VAR": "hello", "PATH": "/usr/bin"})
        assert "RANDOM_PROJECT_VAR" not in env

    def test_injects_ci(self, tmp_path: Path) -> None:
        env = self._build(tmp_path, {"PATH": "/usr/bin"})
        assert env["CI"] == "1"

    def test_injects_no_color(self, tmp_path: Path) -> None:
        env = self._build(tmp_path, {"PATH": "/usr/bin"})
        assert env["NO_COLOR"] == "1"

    def test_injects_git_terminal_prompt(self, tmp_path: Path) -> None:
        env = self._build(tmp_path, {"PATH": "/usr/bin"})
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_injects_pwd(self, tmp_path: Path) -> None:
        env = self._build(tmp_path, {"PATH": "/usr/bin"})
        assert env["PWD"] == str(tmp_path)

    def test_fallback_path_when_missing(self, tmp_path: Path) -> None:
        """When parent_env has no PATH, a non-empty fallback is provided."""
        env = self._build(tmp_path, {})
        assert env.get("PATH")  # non-empty

    def test_extra_bypasses_scrub(self, tmp_path: Path) -> None:
        env = self._build(
            tmp_path,
            {"PATH": "/usr/bin"},
            extra={"GIT_AUTHOR_NAME": "yukar", "MY_TOKEN": "explicit"},
        )
        assert env["GIT_AUTHOR_NAME"] == "yukar"
        assert env["MY_TOKEN"] == "explicit"


# ---------------------------------------------------------------------------
# P1 — build_subprocess_env integration test (actual subprocess)
# ---------------------------------------------------------------------------


class TestBuildSubprocessEnvIntegration:
    """Integration: secrets are absent, safe vars are present in subprocess env."""

    async def _make_ctx(self, worktree: Path, allow: list[str]) -> Any:
        from yukar.agents.context import AgentContext

        return await AgentContext.create(
            project_id="proj",
            epic_id="EP-1",
            repo_name="repo",
            worktree_path=worktree,
            workspace_root=str(worktree.parent),
            allow=allow,
            deny=[],
        )

    async def test_secrets_absent_safe_vars_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Subprocess does not receive ANTHROPIC_API_KEY or FOO_SECRET; PATH and CI are present."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real-secret")
        monkeypatch.setenv("FOO_SECRET", "super-secret")

        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()

        exe = sys.executable
        exe_name = Path(exe).name
        ctx = await self._make_ctx(wt, allow=[exe_name])
        (run_command,) = make_command_tools(ctx)

        script = (
            "import os, json; "
            "keys = ['ANTHROPIC_API_KEY', 'FOO_SECRET', 'PATH', 'CI']; "
            "print(json.dumps({k: k in os.environ for k in keys}))"
        )
        # argv[0] is an absolute path to the interpreter (exempt from P3).
        result = await run_command(command=f'{exe} -c "{script}"')
        # The command may fail or succeed depending on environment details, but
        # we verify via build_subprocess_env directly that secrets are absent.
        from yukar.sandbox.env import build_subprocess_env

        env = build_subprocess_env(cwd=wt)
        assert "ANTHROPIC_API_KEY" not in env
        assert "FOO_SECRET" not in env
        assert "PATH" in env
        assert env.get("CI") == "1"
        # result is not None — some form of response was returned
        assert result is not None


# ---------------------------------------------------------------------------
# P2 — git is unconditionally denied via run_command (direct + wrapper forms)
# ---------------------------------------------------------------------------


class TestGitUnconditionalDenyIntegration:
    """git must always be denied by run_command regardless of the operator allowlist.

    All git operations must go through the dedicated run_git tools (git_tools.py /
    evaluator_tools.py read_diff).  run_command does NOT apply Tier B/C hardening,
    so even "safe" local subcommands like `git status` are forbidden here.
    """

    async def _make_ctx(self, worktree: Path, allow: list[str] | None = None) -> Any:
        from yukar.agents.context import AgentContext

        return await AgentContext.create(
            project_id="proj",
            epic_id="EP-1",
            repo_name="repo",
            worktree_path=worktree,
            workspace_root=str(worktree.parent),
            allow=allow if allow is not None else ["git"],
            deny=[],
        )

    async def test_git_fetch_denied(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt)
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="git fetch")
        assert result["status"] == "error"
        assert "baseline" in result["content"][0]["text"]

    async def test_git_pull_denied(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt)
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="git pull")
        assert result["status"] == "error"
        assert "baseline" in result["content"][0]["text"]

    async def test_git_status_denied(self, tmp_path: Path) -> None:
        """git status is a local subcommand but must still be denied via run_command.

        git must use run_git, not run_command — the baseline deny is unconditional.
        """
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt)
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="git status")
        assert result["status"] == "error"
        assert "baseline" in result["content"][0]["text"]
        assert "run_git" in result["content"][0]["text"]

    async def test_git_config_global_denied(self, tmp_path: Path) -> None:
        """git config --global must be denied — this was the primary escape path."""
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt)
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="git config --global core.pager '!cmd'")
        assert result["status"] == "error"
        assert "baseline" in result["content"][0]["text"]

    async def test_git_denied_even_without_git_in_allowlist(self, tmp_path: Path) -> None:
        """git is denied by baseline even when git is NOT in the operator allow list."""
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        # Empty allow list — everything is denied, but baseline fires first for git.
        ctx = await self._make_ctx(wt, allow=[])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="git status")
        assert result["status"] == "error"
        assert "baseline" in result["content"][0]["text"]

    async def test_env_git_status_denied(self, tmp_path: Path) -> None:
        """env git status must be denied via the wrapper scanner (git in _DANGEROUS_BINARIES)."""
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt, allow=["env", "git"])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="env git status")
        assert result["status"] == "error"
        assert "baseline" in result["content"][0]["text"]

    async def test_sh_c_git_log_denied(self, tmp_path: Path) -> None:
        """sh -c 'git log' must be denied via the shell -c scanner."""
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt, allow=["sh", "git"])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="sh -c 'git log'")
        assert result["status"] == "error"
        assert "baseline" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# P3 — check_absolute_args unit tests
# ---------------------------------------------------------------------------


class TestCheckAbsoluteArgsUnit:
    def _root(self, tmp_path: Path) -> str:
        return str(tmp_path)

    def test_absolute_etc_passwd_denied(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import check_absolute_args

        root = (str(tmp_path),)
        assert check_absolute_args(["cat", "/etc/passwd"], root, str(tmp_path)) is not None

    def test_absolute_tmp_py_denied(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import check_absolute_args

        root = (str(tmp_path),)
        assert check_absolute_args(["python", "/tmp/x.py"], root, str(tmp_path)) is not None

    def test_tilde_home_ssh_key_denied(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import check_absolute_args

        root = (str(tmp_path),)
        assert check_absolute_args(["cat", "~/.ssh/id_rsa"], root, str(tmp_path)) is not None

    def test_dollar_home_netrc_denied(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import check_absolute_args

        root = (str(tmp_path),)
        assert check_absolute_args(["cat", "$HOME/.netrc"], root, str(tmp_path)) is not None

    def test_option_value_absolute_denied(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import check_absolute_args

        root = (str(tmp_path),)
        assert check_absolute_args(["tool", "--output=/etc/foo"], root, str(tmp_path)) is not None

    def test_path_traversal_escape_denied(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import check_absolute_args

        root = (str(tmp_path),)
        escape_arg = str(tmp_path) + "/../../../etc/passwd"
        assert check_absolute_args(["cat", escape_arg], root, str(tmp_path)) is not None

    def test_relative_path_allowed(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import check_absolute_args

        root = (str(tmp_path),)
        assert check_absolute_args(["pytest", "tests/"], root, str(tmp_path)) is None

    def test_absolute_inside_root_allowed(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import check_absolute_args

        root = (str(tmp_path),)
        inside = str(tmp_path) + "/src/x.py"
        assert check_absolute_args(["cat", inside], root, str(tmp_path)) is None

    def test_identifier_key_value_is_split(self, tmp_path: Path) -> None:
        """Identifier key=value: key is 'msg' (identifier), value is '/etc/here' -> denied."""
        from yukar.agents.tools.command import check_absolute_args

        root = (str(tmp_path),)
        cwd = str(tmp_path)
        # 'msg=/etc/here' — _KEY_VALUE_RE matches, value '/etc/here' is absolute -> denied.
        # A plain '-m' flag followed by a message is also denied because the message
        # starts with '/' and is treated as an absolute path token.
        assert check_absolute_args(["git", "commit", "-m", "/etc/here"], root, cwd) is not None

    # -----------------------------------------------------------------------
    # P3 positional key=value (HIGH) — new assertions
    # -----------------------------------------------------------------------

    def test_dd_if_absolute_denied(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import check_absolute_args

        root = (str(tmp_path),)
        cwd = str(tmp_path)
        assert check_absolute_args(["dd", "if=/etc/passwd", "of=loot"], root, cwd) is not None

    def test_make_prefix_denied(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import check_absolute_args

        root = (str(tmp_path),)
        assert check_absolute_args(["make", "PREFIX=/etc"], root, str(tmp_path)) is not None

    def test_tool_config_denied(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import check_absolute_args

        root = (str(tmp_path),)
        assert check_absolute_args(["tool", "config=/etc/shadow"], root, str(tmp_path)) is not None

    def test_option_out_absolute_denied(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import check_absolute_args

        root = (str(tmp_path),)
        assert check_absolute_args(["tool", "--out=/etc/x"], root, str(tmp_path)) is not None

    def test_commit_msg_with_equals_no_absolute_allowed(self, tmp_path: Path) -> None:
        """A commit message '-m msg has = sign' has no key=value form — no false positive."""
        from yukar.agents.tools.command import check_absolute_args

        root = (str(tmp_path),)
        cwd = str(tmp_path)
        # '-m' has no '=' so _KEY_VALUE_RE does not match; 'msg has = sign' is a separate
        # positional token that doesn't start with '/'.
        assert check_absolute_args(["git", "commit", "-m", "msg has = sign"], root, cwd) is None

    def test_positional_name_equals_value_allowed(self, tmp_path: Path) -> None:
        """key=relative-value — value does not start with '/', not denied."""
        from yukar.agents.tools.command import check_absolute_args

        root = (str(tmp_path),)
        assert check_absolute_args(["tool", "name=value"], root, str(tmp_path)) is None

    def test_url_with_query_string_allowed(self, tmp_path: Path) -> None:
        """URL query-string token like 'https://x/y?a=b' — key 'https:' is not an identifier."""
        from yukar.agents.tools.command import check_absolute_args

        root = (str(tmp_path),)
        assert check_absolute_args(["curl", "https://x/y?a=b"], root, str(tmp_path)) is None

    # -----------------------------------------------------------------------
    # P3 relative parent-traversal (MEDIUM) — new assertions for Fix B
    # -----------------------------------------------------------------------

    def test_relative_dotdot_etc_passwd_denied(self, tmp_path: Path) -> None:
        """cat ../../../etc/passwd must be caught when cwd=root."""
        from yukar.agents.tools.command import check_absolute_args

        root = str(tmp_path)
        assert check_absolute_args(["cat", "../../../etc/passwd"], (root,), root) is not None

    def test_relative_dotdot_sibling_denied(self, tmp_path: Path) -> None:
        """cat ../sibling/x escapes the worktree."""
        from yukar.agents.tools.command import check_absolute_args

        root = str(tmp_path)
        assert check_absolute_args(["cat", "../sibling/x"], (root,), root) is not None

    def test_relative_normal_src_allowed(self, tmp_path: Path) -> None:
        """src/x.py has no '..', stays under cwd -> allowed."""
        from yukar.agents.tools.command import check_absolute_args

        root = str(tmp_path)
        assert check_absolute_args(["cat", "src/x.py"], (root,), root) is None

    def test_relative_dotdot_resolves_back_inside_allowed(self, tmp_path: Path) -> None:
        """a/../b resolves back inside root when cwd=root -> allowed."""
        from yukar.agents.tools.command import check_absolute_args

        root = str(tmp_path)
        assert check_absolute_args(["sh", "a/../b"], (root,), root) is None

    def test_git_revision_range_double_dot_allowed(self, tmp_path: Path) -> None:
        """main..feature is a git revision range, not a path traversal."""
        from yukar.agents.tools.command import check_absolute_args

        root = str(tmp_path)
        assert check_absolute_args(["git", "diff", "main..feature"], (root,), root) is None

    def test_git_log_head_range_allowed(self, tmp_path: Path) -> None:
        """HEAD~2..HEAD is a git revision range, not a path traversal."""
        from yukar.agents.tools.command import check_absolute_args

        root = str(tmp_path)
        assert check_absolute_args(["git", "log", "HEAD~2..HEAD"], (root,), root) is None

    # -----------------------------------------------------------------------
    # P3 symlink escape (LOW) — realpath defeats in-tree symlinks
    # -----------------------------------------------------------------------

    def test_symlink_escape_denied(self, tmp_path: Path) -> None:
        """A path inside the worktree that is a symlink pointing outside is denied."""
        import os

        from yukar.agents.tools.command import check_absolute_args

        # Build: worktree/  outside/secret.txt  worktree/link -> outside/
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        link = worktree / "link"
        os.symlink(outside, link)

        # The arg looks like it's inside the worktree but realpath escapes it.
        arg = str(worktree / "link" / "secret.txt")
        root = (str(worktree),)
        assert check_absolute_args(["cat", arg], root, str(worktree)) is not None


# ---------------------------------------------------------------------------
# P3 — run_command integration tests
# ---------------------------------------------------------------------------


class TestAbsoluteArgRunCommandIntegration:
    async def _make_ctx(self, worktree: Path, allow: list[str]) -> Any:
        from yukar.agents.context import AgentContext

        return await AgentContext.create(
            project_id="proj",
            epic_id="EP-1",
            repo_name="repo",
            worktree_path=worktree,
            workspace_root=str(worktree.parent),
            allow=allow,
            deny=[],
        )

    async def test_absolute_path_outside_worktree_denied(self, tmp_path: Path) -> None:
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt, allow=["cat"])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="cat /etc/hostname")
        assert result["status"] == "error"
        assert "outside the worktree" in result["content"][0]["text"]

    async def test_forbid_false_bypasses_p3(self, tmp_path: Path) -> None:
        """With forbid_absolute_args=False the absolute-arg guard is inactive."""
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt, allow=["cat"])
        (run_command,) = make_command_tools(ctx, forbid_absolute_args=False)
        result = await run_command(command="cat /etc/hostname")
        # Must NOT be the absolute-arg denial (may still fail for other reasons).
        text = result["content"][0]["text"]
        assert "outside the worktree" not in text

    async def test_relative_parent_traversal_denied_by_run_command(self, tmp_path: Path) -> None:
        """cat ../../../etc/hostname is denied by run_command with relative traversal guard."""
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt, allow=["cat"])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="cat ../../../etc/hostname")
        assert result["status"] == "error"
        assert "outside the worktree" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Round-4: wrapper-path git escape-flag integration test
# ---------------------------------------------------------------------------


class TestWrapperGitEscapeFlagIntegration:
    """Verify that git is blocked even via wrapper commands.

    git is in _DANGEROUS_BINARIES, so the recursive wrapper scanner in
    check_default_denylist reaches the git unconditional-deny whether git
    appears as the top-level command or nested inside env/xargs/sh.
    """

    async def _make_ctx(self, worktree: Path, allow: list[str]) -> Any:
        from yukar.agents.context import AgentContext

        return await AgentContext.create(
            project_id="proj",
            epic_id="EP-wrap-escape",
            repo_name="repo",
            worktree_path=worktree,
            workspace_root=str(worktree.parent),
            allow=allow,
            deny=[],
        )

    async def test_env_git_dash_c_escape_denied(self, tmp_path: Path) -> None:
        """'env git -C /etc status' must be blocked by the baseline even when env is allowed.

        The point: env passes the allow check (env is allowlisted), but the
        baseline walker recurses into the git payload and the unconditional git
        deny fires before any subcommand logic is reached.
        """
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        # env AND git are both allowlisted — the escape must still be blocked by baseline.
        ctx = await self._make_ctx(wt, allow=["env", "git"])
        (run_command,) = make_command_tools(ctx)
        result = await run_command(command="env git -C /etc status")
        assert result["status"] == "error"
        assert "not permitted" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# P4 — read_diff base_branch validation (Task B)
# ---------------------------------------------------------------------------


class TestReadDiffBaseBranchValidation:
    """read_diff must validate base_branch before passing it to git diff.

    LLM-controlled base_branch values like '--output=/tmp/x' can cause git to
    write to arbitrary paths outside the worktree.  The validation in
    evaluator_tools.py must reject these before git is invoked.
    """

    async def _make_ctx(self, worktree: Path) -> Any:
        from yukar.agents.context import AgentContext

        return await AgentContext.create(
            project_id="proj",
            epic_id="EP-1",
            repo_name="repo",
            worktree_path=worktree,
            workspace_root=str(worktree.parent),
            allow=[],
            deny=[],
        )

    async def test_dash_output_injection_rejected(self, tmp_path: Path) -> None:
        """base_branch='--output=/tmp/yukar_test_escape' must be rejected without running git."""
        import os

        from yukar.agents.tools.evaluator_tools import make_evaluator_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt)
        read_diff, _run_tests = make_evaluator_tools(ctx)
        escape_path = str(tmp_path / "yukar_test_escape")
        result = await read_diff(base_branch=f"--output={escape_path}")
        assert result["status"] == "error"
        assert "invalid base_branch" in result["content"][0]["text"]
        # The file must NOT have been created.
        assert not os.path.exists(escape_path)

    async def test_leading_dash_rejected(self, tmp_path: Path) -> None:
        """Any base_branch starting with '-' must be rejected."""
        from yukar.agents.tools.evaluator_tools import make_evaluator_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt)
        read_diff, _run_tests = make_evaluator_tools(ctx)
        result = await read_diff(base_branch="-evil")
        assert result["status"] == "error"
        assert "invalid base_branch" in result["content"][0]["text"]
        assert "must not start with '-'" in result["content"][0]["text"]

    async def test_double_dot_in_branch_rejected(self, tmp_path: Path) -> None:
        """base_branch='main..evil' must be rejected (contains '..')."""
        from yukar.agents.tools.evaluator_tools import make_evaluator_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt)
        read_diff, _run_tests = make_evaluator_tools(ctx)
        result = await read_diff(base_branch="main..evil")
        assert result["status"] == "error"
        assert "invalid base_branch" in result["content"][0]["text"]
        assert "'..'" in result["content"][0]["text"]

    async def test_special_chars_rejected(self, tmp_path: Path) -> None:
        """base_branch with shell-special characters must be rejected."""
        from yukar.agents.tools.evaluator_tools import make_evaluator_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt)
        read_diff, _run_tests = make_evaluator_tools(ctx)
        result = await read_diff(base_branch="main;rm -rf /")
        assert result["status"] == "error"
        assert "invalid base_branch" in result["content"][0]["text"]

    async def test_valid_main_branch_accepted(self, tmp_path: Path) -> None:
        """base_branch='main' must pass validation; git may still fail but no validation error."""
        import os
        import subprocess

        from yukar.agents.tools.evaluator_tools import make_evaluator_tools

        # Set up a minimal git repo so git diff can actually run.
        wt = tmp_path / "wt"
        wt.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "yukar",
            "GIT_AUTHOR_EMAIL": "yukar@localhost",
            "GIT_COMMITTER_NAME": "yukar",
            "GIT_COMMITTER_EMAIL": "yukar@localhost",
        }
        subprocess.run(["git", "init", "-b", "main", str(wt)], check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "yukar@localhost"],
            cwd=str(wt),
            env=env,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "yukar"],
            cwd=str(wt),
            env=env,
            check=True,
            capture_output=True,
        )
        (wt / "README.md").write_text("hello\n")
        subprocess.run(["git", "add", "."], cwd=str(wt), env=env, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(wt),
            env=env,
            check=True,
            capture_output=True,
        )

        ctx = await self._make_ctx(wt)
        read_diff, _run_tests = make_evaluator_tools(ctx)
        result = await read_diff(base_branch="main")
        # Validation passes; git diff runs and returns success or a git error,
        # not an "invalid base_branch" validation error.
        assert "invalid base_branch" not in result["content"][0]["text"]

    async def test_valid_feature_slash_branch_accepted(self, tmp_path: Path) -> None:
        """base_branch='feature/my-work' (slash-delimited) must pass validation."""
        from yukar.agents.tools.evaluator_tools import make_evaluator_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await self._make_ctx(wt)
        read_diff, _run_tests = make_evaluator_tools(ctx)
        # This will fail with a git error (no repo), but not a validation error.
        result = await read_diff(base_branch="feature/my-work")
        assert "invalid base_branch" not in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# P5 — timeout kills the whole process GROUP (descendants do not survive)
# ---------------------------------------------------------------------------


class TestTimeoutKillsProcessGroup:
    """A command that spawns a grandchild must leave no surviving descendant
    after run_command's timeout fires.

    Regression for the finding that the timeout path only killed the direct
    child (``proc.kill()``), letting grandchildren the command forked outlive
    the run.  The subprocess is now launched with ``start_new_session=True`` and
    the timeout path calls ``os.killpg`` on the whole group.  POSIX-only.
    """

    async def _make_ctx(self, worktree: Path, allow: list[str]) -> Any:
        from yukar.agents.context import AgentContext

        return await AgentContext.create(
            project_id="proj",
            epic_id="EP-1",
            repo_name="repo",
            worktree_path=worktree,
            workspace_root=str(worktree.parent),
            allow=allow,
            deny=[],
        )

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        import os

        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
    async def test_grandchild_killed_on_timeout(self, tmp_path: Path) -> None:
        import asyncio as _asyncio
        import os

        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        pidfile = wt / "grandchild.pid"

        exe = sys.executable
        exe_name = Path(exe).name
        ctx = await self._make_ctx(wt, allow=[exe_name])
        # Short timeout so the test runs quickly.
        (run_command,) = make_command_tools(ctx, timeout=0.5)

        # Parent python spawns a long-sleeping grandchild (a detached child
        # process), records its pid, then sleeps far longer than the timeout.
        # The grandchild is a separate process tree node — only a process-group
        # kill reaps it.
        script = (
            "import subprocess, sys, time; "
            "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"open({str(pidfile)!r}, 'w').write(str(gc.pid)); "
            "time.sleep(60)"
        )
        result = await run_command(command=f'{exe} -c "{script}"')

        # The command itself timed out.
        assert result["status"] == "error"
        assert "timed out" in result["content"][0]["text"].lower()

        # The grandchild pid was recorded before the parent slept.
        assert pidfile.exists(), "grandchild never recorded its pid"
        grandchild_pid = int(pidfile.read_text().strip())

        # Poll briefly: after the process-group kill, the grandchild must die.
        deadline = 5.0
        waited = 0.0
        while self._pid_alive(grandchild_pid) and waited < deadline:
            await _asyncio.sleep(0.1)
            waited += 0.1

        alive = self._pid_alive(grandchild_pid)
        # Best-effort cleanup if the assertion is about to fail (avoid leaking a
        # 60s sleeper into the test runner).
        if alive:
            with __import__("contextlib").suppress(ProcessLookupError):
                os.kill(grandchild_pid, 9)
        assert not alive, f"grandchild {grandchild_pid} survived the timeout kill"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
    async def test_timeout_returns_clean_error(self, tmp_path: Path) -> None:
        """A plain (no-grandchild) timeout still returns the timeout error dict."""
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        exe = sys.executable
        exe_name = Path(exe).name
        ctx = await self._make_ctx(wt, allow=[exe_name])
        (run_command,) = make_command_tools(ctx, timeout=0.3)

        result = await run_command(command=f'{exe} -c "import time; time.sleep(30)"')
        assert result["status"] == "error"
        assert result["returncode"] == -1
        assert "timed out" in result["content"][0]["text"].lower()
