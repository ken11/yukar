"""Low-level git helpers for in-worktree conflict resolution.

These functions support the "reverse merge + agent-assisted resolution"
pattern described in spec §5.2:

1. ``start_conflict_merge`` merges default_branch INTO the worktree (reverse
   direction), leaving conflict markers in place for the agent to resolve.
2. ``merge_in_progress`` / ``abort_merge`` let the caller inspect and clean
   up a partial merge state.
3. ``list_unmerged_files`` returns the current unresolved-file list via
   ``git diff --name-only --diff-filter=U``.

All subprocess calls use ``asyncio.create_subprocess_exec`` (via
``git.runner.run_git``) so they participate in the event loop's cancellation
chain.
"""

from __future__ import annotations

from pathlib import Path

from yukar.git.runner import run_git, validate_git_ref


async def start_conflict_merge(
    worktree_path: Path,
    default_branch: str,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Merge *default_branch* into the current HEAD of *worktree_path*.

    Unlike the standard merge flow (epic-branch → default), this merges in the
    reverse direction so that conflict markers land inside the worktree where
    the sandboxed resolve-agent can fix them.

    The merge is run with ``--no-ff`` (no fast-forward) so the caller can
    distinguish a clean merge commit from a conflicted state.  When conflicts
    exist git stops and leaves conflict markers in the working tree without
    creating a commit; the caller's agent resolves the markers and commits
    manually.  If the merge completes cleanly a merge commit is created
    automatically and the function returns an empty list.

    Args:
        worktree_path: Absolute path to the git worktree (the epic branch).
        default_branch: Branch name to merge in (e.g. ``"main"``).
        env: Optional extra environment variables forwarded to git (e.g.
            ``GIT_AUTHOR_NAME``).

    Returns:
        List of conflicting file paths (relative to the repo root).
        Empty list if the merge completed without conflicts.
    """
    # Vet the worktree for dangerous driver config before the merge.  This
    # mirrors the same check that git/diff.py merge() performs for the forward
    # merge (host-context operation, same risk profile).  Import lazily to
    # avoid a circular dependency (diff.py already imports _resolve_git_dir
    # from this module).
    from yukar.git.diff import _vet_host_git_context

    # default_branch is config-derived; reject a leading-dash ref and fence it
    # behind --end-of-options so it cannot be parsed as a git merge option.
    validate_git_ref(default_branch, what="default_branch")

    await _vet_host_git_context(worktree_path, merge_branch=default_branch)

    full_env = env or {}
    result = await run_git(
        "merge",
        "--no-ff",
        "--end-of-options",
        default_branch,
        cwd=worktree_path,
        check=False,
        env=full_env,
    )

    if result.ok:
        # Clean merge — no conflicts.
        return []

    # Merge failed.  If there are unmerged paths it was a conflict.
    if "CONFLICT" in result.stdout or "CONFLICT" in result.stderr:
        return await list_unmerged_files(worktree_path)

    # Some other git error (e.g. unknown branch).  Re-raise via a check call
    # so the caller gets a useful GitError.
    from yukar.git.runner import GitError

    raise GitError(result, ["git", "merge", "--no-ff", "--end-of-options", default_branch])


async def list_unmerged_files(worktree_path: Path) -> list[str]:
    """Return paths of all unresolved (U-stage) files in *worktree_path*.

    Uses ``git diff --name-only --diff-filter=U`` which is the canonical way to
    list files with unresolved conflict markers without relying on porcelain
    output parsing.

    Args:
        worktree_path: Absolute path to the git worktree.

    Returns:
        Sorted list of relative file paths that still have conflict markers.
    """
    result = await run_git(
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--name-only",
        "--diff-filter=U",
        cwd=worktree_path,
        check=False,
    )
    return sorted(p.strip() for p in result.stdout.splitlines() if p.strip())


def _resolve_git_dir(worktree_path: Path) -> Path:
    """Return the effective .git directory for *worktree_path*.

    For a linked worktree (``git worktree add``), ``.git`` is a **file**
    containing ``gitdir: /path/to/main-repo/.git/worktrees/<name>``.
    Git stores per-worktree metadata (MERGE_HEAD, HEAD, index, etc.) inside
    that worktree-specific directory, NOT in the main ``.git``.

    For a regular repository (the primary checkout), ``.git`` is a directory
    and is returned directly.

    Args:
        worktree_path: Absolute path to the git worktree (linked or primary).

    Returns:
        Absolute ``Path`` to the directory containing MERGE_HEAD, HEAD, etc.
    """
    dot_git = worktree_path / ".git"
    if dot_git.is_file():
        # Linked worktree: parse "gitdir: /path"
        content = dot_git.read_text(encoding="utf-8").strip()
        if content.startswith("gitdir:"):
            return Path(content.split(":", 1)[1].strip())
    # Primary repo: .git is a directory.
    return dot_git


async def merge_in_progress(worktree_path: Path) -> bool:
    """Return ``True`` if a merge is in progress (MERGE_HEAD exists).

    A MERGE_HEAD file is created by ``git merge`` when the merge cannot be
    completed automatically and left for the user to resolve.

    Handles both linked worktrees (where ``.git`` is a file pointing to the
    worktree-specific git dir) and primary checkouts (where ``.git`` is a
    directory).

    Args:
        worktree_path: Absolute path to the git worktree.
    """
    git_dir = _resolve_git_dir(worktree_path)
    merge_head = git_dir / "MERGE_HEAD"
    return merge_head.exists()


async def abort_merge(worktree_path: Path) -> None:
    """Abort an in-progress merge, restoring the worktree to pre-merge state.

    This is a no-op if no merge is in progress (``check=False``).

    Args:
        worktree_path: Absolute path to the git worktree.
    """
    await run_git("merge", "--abort", cwd=worktree_path, check=False)
