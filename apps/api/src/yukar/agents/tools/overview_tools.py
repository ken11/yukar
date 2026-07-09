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

``repo`` is ALWAYS required — name the repo whose branch worktree to inspect (no
default/auto-pick, which could silently answer from the wrong worktree).  An
omitted or unknown value returns an error that lists the valid repo names so the
agent can self-correct.
"""

from __future__ import annotations

from typing import Any

from strands import tool

from yukar.agents.context import AgentContext
from yukar.agents.tools.command import make_command_tools
from yukar.agents.tools.fs import read_file
from yukar.agents.tools.grep_tools import grep_worktree
from yukar.agents.tools.response_builder import make_error


def make_overview_ro_tools(
    contexts: dict[str, AgentContext],
    *,
    include_run_tests: bool = True,
) -> list[Any]:
    """Return read-only worktree tools that dispatch by ``repo`` across *contexts*.

    Args:
        contexts: Mapping of repo name → its ``AgentContext`` (worktree + path
            guard + command allow/deny).  Only repos whose worktree exists should
            be included.
        include_run_tests: When ``True`` (Reviewer), also expose ``run_tests``.
            When ``False`` (Manager), expose only ``fs_read`` + ``repo_grep``.

    Returns:
        ``[fs_read, repo_grep]`` (plus ``run_tests`` when *include_run_tests*),
        each taking an extra ``repo`` argument.  ``[]`` if *contexts* is empty.
    """
    if not contexts:
        return []

    repos = sorted(contexts)

    run_command_by_repo = (
        {r: make_command_tools(c)[0] for r, c in contexts.items()} if include_run_tests else {}
    )

    def _resolve(repo: str) -> tuple[str | None, dict[str, Any] | None]:
        # `repo` is ALWAYS required — there is no default/auto-pick.  Auto-selecting
        # a repo silently answers from the wrong worktree when the intended repo
        # differs (or has no worktree yet), so make the agent name it explicitly.
        if not repo:
            return None, make_error(
                f"`repo` is required — name the repo to inspect. Available: {repos}.",
                results=[],
            )
        if repo not in contexts:
            return None, make_error(
                f"unknown or unavailable repo {repo!r}. Available: {repos}.",
                results=[],
            )
        return repo, None

    @tool
    def fs_read(path: str, repo: str = "") -> dict[str, Any]:
        """Read a full file from a branch worktree (read-only).

        Reads the CURRENT epic branch's live worktree, so it reflects work
        already committed on the branch (unlike ``repo_search``, whose index is
        the default branch).  Before any worktree exists (e.g. no task has run
        yet) it reads the repo's base checkout, so it always works.

        Args:
            path: File path relative to the repo's worktree root.
            repo: Required — name the touched repo to read from (e.g. a repo seen
                in read_branch_diff / repo_summarize).
        """
        name, err = _resolve(repo)
        if err is not None:
            return err
        assert name is not None  # _resolve returns a name whenever err is None
        return read_file(contexts[name], path)

    @tool
    async def repo_grep(
        pattern: str, path: str = ".", max_results: int = 200, repo: str = ""
    ) -> dict[str, Any]:
        """ripgrep search over a branch worktree (read-only, always current).

        Searches the CURRENT epic branch's live worktree, so results reflect the
        latest branch state (unlike ``repo_search``, whose index is the default
        branch).  Before any worktree exists (e.g. no task has run yet) it
        searches the repo's base checkout, so it always works.

        Args:
            pattern: Regex or literal pattern to search for.
            path: Sub-path within the repo's worktree (default: whole worktree).
            max_results: Maximum matching lines to return (default 200).
            repo: Required — name the touched repo to search (e.g. a repo seen in
                read_branch_diff / repo_summarize).
        """
        name, err = _resolve(repo)
        if err is not None:
            return err
        assert name is not None  # _resolve returns a name whenever err is None
        return await grep_worktree(contexts[name], pattern, path, max_results)

    tools: list[Any] = [fs_read, repo_grep]

    if include_run_tests:

        @tool
        async def run_tests(command: str, cwd: str = ".", repo: str = "") -> dict[str, Any]:
            """Run a test command inside a branch worktree (subject to allow/deny).

            Args:
                command: Test command to execute (e.g. ``pytest tests/``).
                cwd: Working directory relative to the repo's worktree root.
                repo: Required — name the touched repo to run in (e.g. a repo seen
                    in read_branch_diff / repo_summarize).
            """
            name, err = _resolve(repo)
            if err is not None:
                return err
            assert name is not None  # _resolve returns a name whenever err is None
            return await run_command_by_repo[name](command=command, cwd=cwd)

        tools.append(run_tests)

    return tools
