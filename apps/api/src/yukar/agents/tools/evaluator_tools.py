"""Evaluator tools — read-only diff and test execution.

``make_evaluator_tools(ctx, ...)`` returns tools for the Evaluator agent role.
The Evaluator is read-only: it can inspect diffs and run tests, but it cannot
modify files or commit.  The same sandbox constraints (worktree scope,
allow/deny) apply as for Worker tools.

Tools
-----
- ``read_diff`` — read the current unified diff for the worktree.  Accepts
  ``paths`` (git pathspecs) so a large diff can be read file group by file
  group, and guards against oversized output: past ``_DIFF_CHAR_LIMIT`` it
  returns the ``--stat`` summary plus a truncated head and tells the caller
  to narrow with ``paths``.
- ``run_tests`` — execute a test command inside the worktree
"""

from __future__ import annotations

import re
from typing import Any

from strands import tool

from yukar.agents.context import AgentContext
from yukar.agents.tools.command import (
    _DEFAULT_TIMEOUT_SECONDS,
    describe_command_permissions,
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

#: Above this many characters a diff is not returned whole: the tool answers
#: with the ``--stat`` summary and a truncated head instead, so one oversized
#: tool result cannot blow the Evaluator's context or push it into max_tokens.
_DIFF_CHAR_LIMIT = 60_000

#: Upper bound on ``paths`` entries per call (argv stays far below OS limits).
_MAX_PATHS = 500


def validate_pathspec(spec: str) -> str | None:
    """Return an error message when *spec* is not a safe git pathspec, else None."""
    if not isinstance(spec, str) or not spec.strip():
        return f"invalid path {spec!r}: must be a non-empty string"
    if any(ord(ch) < 32 or ch == "\x7f" for ch in spec):
        return f"invalid path {spec!r}: control characters are not permitted"
    if spec.startswith("-"):
        return f"invalid path {spec!r}: paths must not start with '-'"
    if spec.startswith("/") or spec.startswith("~"):
        return f"invalid path {spec!r}: paths are relative to the repo root"
    if ".." in spec.split("/"):
        return f"invalid path {spec!r}: '..' segments are not permitted"
    if ":" in spec.split("/", 1)[0] and spec.startswith(":"):
        return f"invalid path {spec!r}: pathspec magic (':...') is not permitted"
    return None


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
    async def read_diff(
        staged: bool = False,
        base_branch: str | None = None,
        paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read the unified diff for the assigned worktree (read-only).

        By default (no *base_branch*, *staged* ignored for default path) returns
        the staged diff (``--cached`` / index vs HEAD).  The host stages all
        Worker changes with ``git add -A`` before calling the Evaluator, so the
        staged diff represents the complete set of Worker changes including new
        files.  An empty staged diff means the Worker made no changes.

        A diff longer than about 60k characters is NOT returned whole: you get
        the ``--stat`` file summary plus a truncated head, and must read it in
        parts with ``paths`` (one file group per call).

        Args:
            staged: Accepted for backwards compatibility when *base_branch* is
                ``None``, but the default path always uses ``--cached`` regardless
                of this flag.  Has no effect unless *base_branch* is set.
            base_branch: If provided, show the full diff between the current
                branch and *base_branch* (e.g. ``"main"``).  This is the
                "epic diff" view used for final evaluation.  When set, the
                *staged* flag is ignored.
            paths: Optional list of paths/globs (git pathspecs, relative to the
                repo root, e.g. ``["src/app/", "tests/test_x.py"]``) to limit
                the diff to.  Omit to read everything.

        Returns:
            A dict with ``"diff"`` (unified diff text), ``"files"`` (the files
            covered, from ``--stat``) and ``"truncated"`` (True when the diff
            was cut — narrow with ``paths``).
        """
        selected = list(paths) if paths else []
        if len(selected) > _MAX_PATHS:
            return make_error(
                f"too many paths ({len(selected)}); pass at most {_MAX_PATHS} per call "
                "(use directory prefixes or globs instead)"
            )
        for spec in selected:
            err = validate_pathspec(spec)
            if err is not None:
                return make_error(err)
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
            # token even if validation is somehow bypassed.  Options that vary
            # per call (--stat / --numstat) must come BEFORE it, so the revision
            # part is kept separate from the option part.
            opts = ["diff", "--no-ext-diff", "--no-textconv"]
            rev = ["--end-of-options", f"{base_branch}...HEAD"]
        else:
            # Default: show staged diff (index vs HEAD).
            # The host runs ``git add -A`` before the Evaluator, so --cached
            # captures all Worker changes including new files.
            opts = ["diff", "--no-ext-diff", "--no-textconv", "--cached"]
            rev = []
        path_args = ["--", *selected] if selected else []
        try:
            result = await run_git(*opts, *rev, *path_args, cwd=worktree)
            # File list from --numstat -z --no-renames: exact paths (no
            # "old => new" rename syntax, no quoting/escaping, no truncation)
            # so each entry can be fed straight back into ``paths``.
            numstat = await run_git(
                *opts, "--numstat", "-z", "--no-renames", *rev, *path_args, cwd=worktree
            )
        except (GitError, OSError) as exc:
            return make_error(f"git diff failed: {exc}")
        diff = result.stdout or "(no diff)"
        files = [
            entry.split("\t", 2)[2]
            for entry in (numstat.stdout or "").split("\0")
            if entry.count("\t") >= 2
        ]
        if len(diff) <= _DIFF_CHAR_LIMIT:
            return make_success(diff, diff=diff, files=files, truncated=False)
        try:
            stat = await run_git(*opts, "--stat=120", *rev, *path_args, cwd=worktree)
        except (GitError, OSError) as exc:
            return make_error(f"git diff failed: {exc}")
        stat_text = (stat.stdout or "").rstrip()
        head = diff[: _DIFF_CHAR_LIMIT // 2]
        note = (
            f"DIFF TOO LARGE TO RETURN WHOLE ({len(diff)} characters across {len(files)} "
            f"files). Below: the --stat summary, then the first {len(head)} characters. "
            "Read the rest in parts by calling read_diff again with `paths` set to one "
            "file group at a time — do not try to review the whole diff from this "
            "truncated view."
        )
        text = f"{note}\n\n{stat_text}\n\n{head}\n\n…(truncated — use `paths`)"
        return make_success(text, diff=text, files=files, truncated=True, total_chars=len(diff))

    # Delegate run_tests to the shared run_command infrastructure so the same
    # allow/deny and timeout rules apply.
    _cmd_tools = make_command_tools(ctx, timeout=timeout)
    _run_command = _cmd_tools[0]  # the sole tool returned

    # Dynamic description: the concrete allow/deny lists must be visible BEFORE
    # the first call, not only in a rejection message.
    _run_tests_description = (
        "Run a test command inside the worktree (read-only from the "
        "Evaluator's view; nothing is committed by this tool).\n\n"
        + describe_command_permissions(ctx.command_config.allow, ctx.command_config.deny)
    )

    @tool(description=_run_tests_description)
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
