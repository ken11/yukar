"""Tests for command-prefix matching in the run_command allow/deny lists.

Regression for the reported bug: the operator UI invites multi-token entries
(its allow placeholder is ``pnpm test`` / ``pnpm lint`` / ``pytest`` and its deny
placeholder is ``rm -rf``), but the old matcher compared only the command's first
token against the list.  An allow entry like ``make generate`` therefore never
matched a ``make generate`` invocation — argv[0] is ``make``, and ``"make"`` is
not in ``("make generate",)`` — so the agent's correct ``make generate`` call was
rejected with "make is not permitted".

``RepoCommandConfig.is_allowed`` now matches each entry as a command prefix:
single-token entries keep their command-name semantics, while multi-token entries
allow a specific subcommand without allowing the whole command.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from yukar.agents.context import RepoCommandConfig

# ---------------------------------------------------------------------------
# Pure-unit tests of RepoCommandConfig.is_allowed (no subprocess spawned)
# ---------------------------------------------------------------------------


class TestIsAllowedPrefixMatching:
    def test_single_token_allows_any_invocation(self) -> None:
        """A bare command name matches the command with any arguments."""
        cfg = RepoCommandConfig(allow=("pytest",))
        assert cfg.is_allowed(["pytest"])
        assert cfg.is_allowed(["pytest", "-q", "tests/"])

    def test_multi_token_allows_exact_subcommand(self) -> None:
        """The reported bug: ``make generate`` must allow ``make generate``."""
        cfg = RepoCommandConfig(allow=("make generate",))
        assert cfg.is_allowed(["make", "generate"])

    def test_multi_token_allows_trailing_args(self) -> None:
        """Trailing args beyond the entry are permitted (prefix, not exact)."""
        cfg = RepoCommandConfig(allow=("pnpm test",))
        assert cfg.is_allowed(["pnpm", "test", "--filter", "web"])

    def test_multi_token_rejects_other_subcommand(self) -> None:
        """``make generate`` must NOT allow a different subcommand."""
        cfg = RepoCommandConfig(allow=("make generate",))
        assert not cfg.is_allowed(["make", "build"])

    def test_multi_token_rejects_bare_command(self) -> None:
        """``make generate`` is strictly narrower than ``make`` — bare ``make`` denied."""
        cfg = RepoCommandConfig(allow=("make generate",))
        assert not cfg.is_allowed(["make"])

    def test_multiple_entries_any_match_allows(self) -> None:
        cfg = RepoCommandConfig(allow=("pnpm test", "pnpm lint", "pytest"))
        assert cfg.is_allowed(["pnpm", "lint"])
        assert cfg.is_allowed(["pytest", "-x"])
        assert not cfg.is_allowed(["pnpm", "install"])

    def test_argv0_basename_matches_entry(self) -> None:
        """Absolute argv[0] matches a bare-name entry token (``/usr/bin/make`` == ``make``)."""
        cfg = RepoCommandConfig(allow=("make generate",))
        assert cfg.is_allowed(["/usr/bin/make", "generate"])

    def test_empty_allow_denies_all(self) -> None:
        cfg = RepoCommandConfig(allow=())
        assert not cfg.is_allowed(["pytest"])
        assert not cfg.is_allowed(["make", "generate"])

    def test_empty_tokens_denied(self) -> None:
        cfg = RepoCommandConfig(allow=("pytest",))
        assert not cfg.is_allowed([])

    def test_blank_entry_never_matches(self) -> None:
        """A blank/whitespace allow line must not silently allow everything."""
        cfg = RepoCommandConfig(allow=("",))
        assert not cfg.is_allowed(["pytest"])

    # --- deny precedence ---

    def test_single_token_deny_blocks_all(self) -> None:
        cfg = RepoCommandConfig(allow=("rm",), deny=("rm",))
        assert not cfg.is_allowed(["rm", "-rf", "build"])

    def test_multi_token_deny_blocks_specific_subcommand(self) -> None:
        """``rm -rf`` denies ``rm -rf x`` but leaves a plain ``rm x`` allowed."""
        cfg = RepoCommandConfig(allow=("rm",), deny=("rm -rf",))
        assert not cfg.is_allowed(["rm", "-rf", "build"])
        assert cfg.is_allowed(["rm", "build"])

    def test_deny_takes_priority_over_allow(self) -> None:
        cfg = RepoCommandConfig(allow=("pnpm publish",), deny=("pnpm publish",))
        assert not cfg.is_allowed(["pnpm", "publish"])


# ---------------------------------------------------------------------------
# Integration: end-to-end through the run_command tool
# ---------------------------------------------------------------------------


class TestMultiTokenAllowlistRunCommand:
    async def _make_ctx(self, worktree: Path, allow: list[str]) -> Any:
        from yukar.agents.context import AgentContext

        return await AgentContext.create(
            project_id="proj",
            epic_id="EP-allowlist",
            repo_name="repo",
            worktree_path=worktree,
            workspace_root=str(worktree.parent),
            allow=allow,
            deny=[],
        )

    async def test_multi_token_subcommand_runs(self, tmp_path: Path) -> None:
        """A multi-token allow entry permits its subcommand end-to-end.

        Uses the interpreter (an absolute argv[0]) with a ``-c`` subcommand so
        the test does not depend on ``make`` being installed.
        """
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        exe = sys.executable
        exe_name = Path(exe).name
        ctx = await self._make_ctx(wt, allow=[f"{exe_name} -c"])
        (run_command,) = make_command_tools(ctx)

        result = await run_command(command=f'{exe} -c "print(42)"')
        assert result["status"] == "success", result["content"][0]["text"]
        assert "42" in result["stdout"]

    async def test_multi_token_other_subcommand_denied(self, tmp_path: Path) -> None:
        """A different subcommand is rejected with the allowlist denial."""
        from yukar.agents.tools.command import make_command_tools

        wt = tmp_path / "wt"
        wt.mkdir()
        exe = sys.executable
        exe_name = Path(exe).name
        ctx = await self._make_ctx(wt, allow=[f"{exe_name} -c"])
        (run_command,) = make_command_tools(ctx)

        result = await run_command(command=f"{exe} --version")
        assert result["status"] == "error"
        assert "is not permitted" in result["content"][0]["text"]
