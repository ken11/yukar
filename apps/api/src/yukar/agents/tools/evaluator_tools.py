"""Evaluator tools — read-only diff and test execution.

``make_evaluator_tools(ctx, ...)`` returns tools for the Evaluator agent role.
The Evaluator is read-only: it can inspect diffs and run tests, but it cannot
modify files or commit.  The same sandbox constraints (worktree scope,
allow/deny) apply as for Worker tools.

Tools
-----
- ``read_diff`` — read the current unified diff for the worktree
- ``run_tests`` — execute a test command inside the worktree
"""

from __future__ import annotations

import re
from typing import Any

from strands import tool

from yukar.agents.context import AgentContext
from yukar.agents.tools.command import (
    _DEFAULT_TIMEOUT_SECONDS,
    make_command_tools,
)
from yukar.agents.tools.response_builder import make_error, make_success
from yukar.git.runner import GitError, run_git

# Allowlist pattern for base_branch values passed to git diff.
# Permits plain branch/tag names like "main", "feature/x", "v1.2.3".
# Deliberately conservative: revision syntax such as "HEAD~2" or "HEAD@{0}" is
# rejected (no "~", "@", "{", "}"), as are values starting with "-" (option
# injection) and path traversal ("..").
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def make_evaluator_tools(
    ctx: AgentContext,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> list[Any]:
    """Return [read_diff, run_tests] tools bound to *ctx*'s worktree.

    Args:
        ctx: Agent context (worktree scope, allow/deny config).
        timeout: Maximum seconds for test subprocess.

    Returns:
        Two Strands ``AgentTool`` objects.
    """
    worktree = ctx.worktree_path

    @tool
    async def read_diff(staged: bool = False, base_branch: str | None = None) -> dict[str, Any]:
        """Read the unified diff for the assigned worktree (read-only).

        By default (no *base_branch*, *staged* ignored for default path) returns
        the staged diff (``--cached`` / index vs HEAD).  The host stages all
        Worker changes with ``git add -A`` before calling the Evaluator, so the
        staged diff represents the complete set of Worker changes including new
        files.  An empty staged diff means the Worker made no changes.

        Args:
            staged: Accepted for backwards compatibility when *base_branch* is
                ``None``, but the default path always uses ``--cached`` regardless
                of this flag.  Has no effect unless *base_branch* is set.
            base_branch: If provided, show the full diff between the current
                branch and *base_branch* (e.g. ``"main"``).  This is the
                "epic diff" view used for final evaluation.  When set, the
                *staged* flag is ignored.

        Returns:
            A dict with ``"diff"`` (unified diff text).
        """
        if base_branch is not None:
            # Validate base_branch before interpolating into git argv.
            # Reject: values starting with "-" (option injection), values
            # containing ".." (path traversal / ambiguous range), and anything
            # not matching the safe branch-name pattern.
            if base_branch.startswith("-"):
                return make_error(
                    f"invalid base_branch {base_branch!r}: "
                    "branch names must not start with '-'"
                )
            if ".." in base_branch:
                return make_error(
                    f"invalid base_branch {base_branch!r}: "
                    "branch names must not contain '..'"
                )
            if not re.fullmatch(_BRANCH_RE, base_branch):
                return make_error(
                    f"invalid base_branch {base_branch!r}: "
                    "only alphanumerics and . _ / - are permitted"
                )
            # --end-of-options ensures base_branch is treated as a revision
            # token even if validation is somehow bypassed.
            args = [
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--end-of-options",
                f"{base_branch}...HEAD",
            ]
        else:
            # Default: show staged diff (index vs HEAD).
            # The host runs ``git add -A`` before the Evaluator, so --cached
            # captures all Worker changes including new files.
            args = ["diff", "--no-ext-diff", "--no-textconv", "--cached"]
        try:
            result = await run_git(*args, cwd=worktree)
        except GitError as exc:
            return make_error(f"git diff failed: {exc}")
        diff = result.stdout or "(no diff)"
        return make_success(diff, diff=diff)

    # Delegate run_tests to the shared run_command infrastructure so the same
    # allow/deny and timeout rules apply.
    _cmd_tools = make_command_tools(ctx, timeout=timeout)
    _run_command = _cmd_tools[0]  # the sole tool returned

    @tool
    async def run_tests(command: str, cwd: str = ".") -> dict[str, Any]:
        """Run a test command inside the worktree (read-only from Evaluator's view).

        The command is subject to the same allow/deny restrictions as
        ``run_command`` for Workers.  No file modifications are committed
        by this tool.

        Args:
            command: Test command to execute (e.g. ``"pytest tests/"``).
            cwd: Working directory relative to the worktree root.

        Returns:
            A dict with ``"stdout"``, ``"stderr"``, ``"returncode"``, and
            ``"status"``.
        """
        # Call the underlying run_command tool directly.
        return await _run_command(command=command, cwd=cwd)

    return [read_diff, run_tests]
