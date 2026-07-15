"""Multi-repo read-only worktree tools for Manager / Reviewer (overview roles).

The single-repo factories in this package (``make_fs_tools`` / ``make_grep_tools``
/ ``make_evaluator_tools``) each bind ONE tool to ONE worktree, so their fixed
tool names (``fs_read`` / ``repo_grep`` / ``run_tests``) would collide if an epic
touches more than one repo.  A Manager or Reviewer, however, oversees the WHOLE
epic and must be able to inspect every touched repo's branch worktree — and
multi-repo epics are the common case.

``make_overview_ro_tools`` solves this by exposing ONE tool of each kind that
takes a ``repo`` argument and dispatches to that repo's worktree.  Each wrapper
delegates to the shared read/grep/test core so there is exactly one
implementation of the logic:

- ``fs_read``   → ``fs.read_file``
- ``repo_grep`` → ``grep_tools.grep_worktree``
- ``run_tests`` → ``command.make_command_tools``'s ``run_command`` (the same
  direct-callable path the Evaluator's ``run_tests`` already uses)

The target tree is resolved PER CALL (via the caller-supplied ``resolve_ctx``),
NOT frozen when the bundle is built.  The active trial's worktree for a repo is
created lazily by the first dispatch that touches it — possibly in the MIDDLE of
the Manager run — so a context frozen at build time would keep answering from
the base checkout even after a Worker has committed real work on the branch (the
"repo_grep/fs_read show old code after I worked in the worktree" bug).  Mirrors
``browser_overview_tools``, which resolves its browser target the same way.

``repo`` is ALWAYS required — name the repo whose branch worktree to inspect (no
default/auto-pick, which could silently answer from the wrong worktree).  An
omitted or unknown value returns an error that lists the valid repo names so the
agent can self-correct.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from strands import tool

from yukar.agents.context import AgentContext
from yukar.agents.tools.command import make_command_tools
from yukar.agents.tools.fs import read_file
from yukar.agents.tools.grep_tools import grep_worktree
from yukar.agents.tools.response_builder import make_error

# Async resolver: given a repo name, return the read-only ``AgentContext`` for
# the tree to inspect RIGHT NOW (the active trial's worktree once it exists on
# disk, else the repo's base checkout), or ``None`` when neither tree is on disk.
# It is awaited on EVERY tool call so a worktree the first dispatch creates
# mid-run is picked up immediately instead of the tool staying frozen on the
# base checkout it started against.
CtxResolver = Callable[[str], Awaitable["AgentContext | None"]]


def make_overview_ro_tools(
    repos: list[str],
    resolve_ctx: CtxResolver,
    *,
    include_run_tests: bool = True,
) -> list[Any]:
    """Return read-only worktree tools that dispatch by ``repo``, resolving the
    target tree PER CALL via *resolve_ctx*.

    Args:
        repos: Valid repo names — used for the ``repo`` argument's membership
            check and the error messages that list what is available.
        resolve_ctx: Async callable mapping a repo name to its CURRENT read-only
            ``AgentContext`` (the active trial's worktree if it exists now, else
            the repo's base checkout), or ``None`` when neither tree is on disk.
            Awaited on every tool invocation so a worktree created mid-run is
            reflected on the next call.
        include_run_tests: When ``True`` (Reviewer), also expose ``run_tests``.
            When ``False`` (Manager), expose only ``fs_read`` + ``repo_grep``.

    Returns:
        ``[fs_read, repo_grep]`` (plus ``run_tests`` when *include_run_tests*),
        each taking an extra ``repo`` argument.  ``[]`` if *repos* is empty.
    """
    if not repos:
        return []

    repo_names = sorted(repos)

    async def _resolve(repo: str) -> tuple[AgentContext | None, dict[str, Any] | None]:
        # `repo` is ALWAYS required — there is no default/auto-pick.  Auto-selecting
        # a repo silently answers from the wrong worktree when the intended repo
        # differs (or has no worktree yet), so make the agent name it explicitly.
        if not repo:
            return None, make_error(
                f"`repo` is required — name the repo to inspect. Available: {repo_names}.",
                results=[],
            )
        if repo not in repo_names:
            return None, make_error(
                f"unknown or unavailable repo {repo!r}. Available: {repo_names}.",
                results=[],
            )
        ctx = await resolve_ctx(repo)
        if ctx is None:
            return None, make_error(
                f"repo {repo!r} has neither a trial worktree nor a readable base "
                "checkout on disk — nothing to inspect.",
                results=[],
            )
        return ctx, None

    @tool
    async def fs_read(path: str, repo: str = "") -> dict[str, Any]:
        """Read a full file from a branch worktree (read-only, always current).

        Resolves its target on every call, so it reflects the branch's LATEST
        state: the active trial's live worktree once any task has created it —
        including a worktree the first dispatch creates partway through THIS run
        — and the repo's base checkout before that (so it always works from Turn
        0).  Unlike ``repo_search``, whose index is the default branch.

        Args:
            path: File path relative to the repo's worktree root.
            repo: Required — name the touched repo to read from (e.g. a repo seen
                in read_branch_diff / repo_summarize).
        """
        ctx, err = await _resolve(repo)
        if err is not None:
            return err
        assert ctx is not None  # _resolve returns a ctx whenever err is None
        return read_file(ctx, path)

    @tool
    async def repo_grep(
        pattern: str, path: str = ".", max_results: int = 200, context: int = 0, repo: str = ""
    ) -> dict[str, Any]:
        """ripgrep search over a branch worktree (read-only, always current).

        Returns the matching lines themselves as ``path:lineno:text`` (not just
        a count), optionally with surrounding lines of context.

        Resolves its target on every call, so results reflect the branch's
        LATEST state: the active trial's live worktree once any task has created
        it — including a worktree the first dispatch creates partway through THIS
        run — and the repo's base checkout before that (so it always works from
        Turn 0).  Unlike ``repo_search``, whose index is the default branch.

        Args:
            pattern: Regex or literal pattern to search for.
            path: Sub-path within the repo's worktree (default: whole worktree).
            max_results: Maximum matching lines to return (default 200).
            context: Surrounding lines to show before/after each match
                (like ``rg -C``; default 0, capped at 10).
            repo: Required — name the touched repo to search (e.g. a repo seen in
                read_branch_diff / repo_summarize).
        """
        ctx, err = await _resolve(repo)
        if err is not None:
            return err
        assert ctx is not None  # _resolve returns a ctx whenever err is None
        return await grep_worktree(ctx, pattern, path, max_results, context)

    tools: list[Any] = [fs_read, repo_grep]

    if include_run_tests:

        @tool
        async def run_tests(command: str, cwd: str = ".", repo: str = "") -> dict[str, Any]:
            """Run a test command inside a branch worktree (subject to allow/deny).

            Resolves its target on every call (active trial worktree once it
            exists, else base checkout), so tests run against the branch's
            current state — including a worktree created earlier in this run.

            Args:
                command: Test command to execute (e.g. ``pytest tests/``).
                cwd: Working directory relative to the repo's worktree root.
                repo: Required — name the touched repo to run in (e.g. a repo seen
                    in read_branch_diff / repo_summarize).
            """
            ctx, err = await _resolve(repo)
            if err is not None:
                return err
            assert ctx is not None  # _resolve returns a ctx whenever err is None
            run_command = make_command_tools(ctx)[0]
            return await run_command(command=command, cwd=cwd)

        tools.append(run_tests)

    return tools
