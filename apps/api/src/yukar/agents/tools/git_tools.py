"""Worker git tools — scoped to the assigned worktree branch.

``make_git_tools(ctx, author_name, author_email)`` returns git Strands tools
whose closures are bound to the agent's worktree:

- ``git_status``  — show working tree status
- ``git_diff``    — show unstaged or staged diff
- ``git_add``     — stage files (each path validated through PathGuard)
- ``git_commit``  — commit staged changes to the epic branch

All operations run with ``cwd=worktree_path`` so they are confined to the
correct worktree.  Push / fetch / clone are deliberately **not** provided
(spec §0.1, §5.2).

``git_add`` explicitly validates each supplied path through ``ctx.path_guard``
before passing them to git.  We do not rely solely on git's "outside
repository" error because:
1. git's error is non-deterministic (varies with worktree configuration).
2. Our PathGuard provides a uniform, testable rejection surface.
"""

from __future__ import annotations

from typing import Any

from strands import tool

from yukar.agents.context import AgentContext
from yukar.agents.tools.response_builder import make_error, make_success
from yukar.git.runner import GitError, git_author_env, run_git
from yukar.sandbox.path_guard import PathGuardError


def make_git_tools(
    ctx: AgentContext,
    author_name: str = "yukar",
    author_email: str = "yukar@localhost",
    include_commit: bool = True,
) -> list[Any]:
    """Return git tools bound to *ctx*.

    Args:
        ctx: Agent context (worktree_path is used as cwd for all git calls).
        author_name: Git author name for commits.
        author_email: Git author email for commits.
        include_commit: If ``True`` (default), the returned list is
            ``[git_status, git_diff, git_add, git_commit]``.  If ``False``,
            ``git_commit`` is omitted and ``[git_status, git_diff, git_add]``
            is returned.  Pass ``False`` for Worker agents where the host
            commits on their behalf after Evaluator acceptance.

    Returns:
        A list of Strands ``AgentTool`` objects (three or four depending on
        *include_commit*).
    """
    worktree = ctx.worktree_path

    @tool
    async def git_status() -> dict[str, Any]:
        """Show the working tree status of the assigned worktree.

        Returns:
            A dict with ``"output"`` (porcelain status text).
        """
        try:
            result = await run_git("status", "--porcelain", cwd=worktree)
        except GitError as exc:
            return make_error(f"git status failed: {exc}")
        output = result.stdout.strip() or "(clean)"
        return make_success(output, output=output)

    @tool
    async def git_diff(staged: bool = False) -> dict[str, Any]:
        """Show diff of changes in the assigned worktree.

        Args:
            staged: If ``True``, show staged diff (``--cached``).
                    If ``False`` (default), show unstaged diff.

        Returns:
            A dict with ``"diff"`` (unified diff text).
        """
        args: list[str] = ["diff", "--no-ext-diff", "--no-textconv"]
        if staged:
            args.append("--cached")
        try:
            result = await run_git(*args, cwd=worktree)
        except GitError as exc:
            return make_error(f"git diff failed: {exc}")
        diff = result.stdout or "(no diff)"
        return make_success(diff, diff=diff)

    @tool
    async def git_add(paths: str = ".") -> dict[str, Any]:
        """Stage files in the assigned worktree.

        Each supplied path is resolved and validated through ``ctx.path_guard``
        before being passed to git.  This ensures that absolute paths or
        traversal sequences like ``../evil`` cannot escape the worktree sandbox
        — we do not rely on git's own "outside repository" detection alone.

        Args:
            paths: Space-separated list of paths to stage, relative to the
                worktree root.  Defaults to ``"."`` (stage all changes).

        Returns:
            Status of the staging operation.
        """
        raw_paths = paths.split()
        resolved_paths: list[str] = []
        for raw in raw_paths:
            try:
                resolved = ctx.path_guard.resolve(raw)
                resolved_paths.append(str(resolved))
            except PathGuardError as exc:
                return make_error(f"git add path error: {exc}")
        try:
            result = await run_git("add", *resolved_paths, cwd=worktree)
        except GitError as exc:
            return make_error(f"git add failed: {exc}")
        return make_success(result.stdout.strip() or "Staged successfully.")

    @tool
    async def git_commit(message: str) -> dict[str, Any]:
        """Create a commit on the epic branch in the assigned worktree.

        The commit author is set to the configured yukar git identity.
        The commit is applied to the current branch of the worktree (the
        epic branch) — push is not performed.

        Args:
            message: Commit message.

        Returns:
            A dict with ``"commit_hash"`` (short SHA) on success.
        """
        try:
            result = await run_git(
                "commit",
                "-m",
                message,
                cwd=worktree,
                env=git_author_env(author_name, author_email),
            )
        except GitError as exc:
            return make_error(f"git commit failed: {exc}")

        # Resolve the short hash via rev-parse rather than scraping the commit
        # summary line.  The summary format ("[<branch> <hash>] msg", or
        # "[<branch> (root-commit) <hash>] msg") is fragile: a naive token split
        # yields "<hash>]" (trailing bracket) for a normal commit and
        # "(root-commit)" for the very first commit.  rev-parse is unambiguous.
        output = result.stdout.strip()
        commit_hash: str | None = None
        try:
            short = await run_git("rev-parse", "--short", "HEAD", cwd=worktree)
        except GitError:
            short = None
        if short is not None:
            commit_hash = short.stdout.strip() or None

        return make_success(output or "Committed.", commit_hash=commit_hash)

    if include_commit:
        return [git_status, git_diff, git_add, git_commit]
    return [git_status, git_diff, git_add]
