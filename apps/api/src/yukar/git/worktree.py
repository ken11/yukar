"""Git worktree management — lazy creation per spec §5.1.

The orchestrator calls ``ensure_worktree`` the first time a Worker needs a
repo.  If the worktree already exists it is returned immediately (idempotent).
If it does not exist, the worktree (and branch) are created atomically via
``git worktree add``.

Invariants
----------
- ``ensure_worktree`` never pushes, fetches, or clones (spec §0.1).
- Branch creation falls back to checkout-without-``-b`` when the branch
  already exists locally (e.g. after a crash-recovery).
- ``remove_worktree`` returns a ``(removed, error_message)`` tuple so the
  caller can distinguish a successful removal from a git-level refusal (e.g.
  dirty worktree with force=False).  It never raises.
"""

from __future__ import annotations

from pathlib import Path

from yukar.git.runner import run_git, validate_git_ref


async def ensure_worktree(
    repo_path: Path,
    worktree_path: Path,
    branch: str,
    default_branch: str,
) -> Path:
    """Create a git worktree at *worktree_path* if it does not already exist.

    The function is idempotent: if *worktree_path* already exists as a git
    worktree directory it is returned immediately without any git invocations.

    Args:
        repo_path: Absolute path of the existing local git repository (not a
            worktree).  The working directory for all ``git`` calls.
        worktree_path: Desired absolute path for the new worktree.
        branch: Branch name for the worktree (e.g. ``yukar/EP-1-my-epic``).
        default_branch: The base branch from which to create *branch* when it
            does not yet exist (e.g. ``main``).

    Returns:
        The resolved ``worktree_path`` (created or pre-existing).

    Raises:
        GitError: If git returns a non-zero exit code for an unexpected reason.
    """
    # branch + default_branch are config/LLM-derived and reach git as
    # positional/-b tokens.  ``worktree add`` re-invokes ``git branch``
    # internally where ``--end-of-options`` does NOT propagate, so a
    # leading-dash branch (e.g. ``-b -evil``) leaks as a switch into that
    # inner call.  Reject such refs up front rather than relying on the
    # separator alone.
    validate_git_ref(branch, what="branch")
    validate_git_ref(default_branch, what="default_branch")

    # Fast path: worktree directory already present.
    if worktree_path.exists():
        return worktree_path.resolve()

    # Ensure the parent directory exists (worktrees/ dir).
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    # Determine whether the branch already exists locally.
    branch_exists = await _branch_exists(repo_path, branch)

    # Vet the repo before adding a worktree — consistent with commit() and
    # merge() which also vet before any host-context operation.
    # For existing branches, check the branch too (it may have dangerous attrs).
    # For new branches, check against default_branch (the base).
    # This is a host-context operation (worktree add runs hooks at checkout);
    # vetting ensures no driver-execution before the branch is checked out.
    from yukar.git.diff import _vet_host_git_context

    vet_branch = branch if branch_exists else default_branch
    await _vet_host_git_context(repo_path, merge_branch=vet_branch)

    if branch_exists:
        # Checkout existing branch without re-creating it.
        # isolate_config=False: preserve operator global config (git-lfs etc.)
        # while still applying harden flags (hooks/fsmonitor/env-scrub).
        # --end-of-options fences the positional commit-ish (the branch).
        await run_git(
            "worktree",
            "add",
            str(worktree_path),
            "--end-of-options",
            branch,
            cwd=repo_path,
            isolate_config=False,
        )
    else:
        # Create a new branch from default_branch.  ``-b <branch>`` is an
        # option pair that must precede --end-of-options; the positional
        # start-point (default_branch) follows the separator.
        await run_git(
            "worktree",
            "add",
            str(worktree_path),
            "-b",
            branch,
            "--end-of-options",
            default_branch,
            cwd=repo_path,
            isolate_config=False,
        )

    return worktree_path.resolve()


async def remove_worktree(
    repo_path: Path, worktree_path: Path, *, force: bool = False
) -> tuple[bool, str | None]:
    """Remove a worktree and prune the worktree list.

    This function never raises.  The caller must inspect the return value to
    determine whether removal actually succeeded.

    Args:
        repo_path: Absolute path of the main git repository.
        worktree_path: Absolute path of the worktree to remove.
        force: Pass ``--force`` to ``git worktree remove`` (needed when the
            worktree has untracked / uncommitted changes).

    Returns:
        A ``(removed, error_message)`` tuple.  ``removed`` is ``True`` when
        the worktree directory no longer exists after the call.  On failure,
        ``error_message`` contains the git stderr output; on success it is
        ``None``.
    """
    if not worktree_path.exists():
        return True, None

    args = ["worktree", "remove", str(worktree_path)]
    if force:
        args.append("--force")

    result = await run_git(*args, cwd=repo_path, check=False)

    if not result.ok:
        # Removal was refused by git (e.g. dirty worktree, MERGE_HEAD present).
        # Do NOT prune here: the worktree still exists and is in a known state.
        error = result.stderr.strip() or f"git worktree remove exited with rc={result.returncode}"
        return False, error

    # Prune stale administrative files only after a confirmed removal.
    await run_git("worktree", "prune", cwd=repo_path, check=False)
    return True, None


async def delete_branch(
    repo_path: Path,
    branch: str,
    *,
    force: bool = False,
) -> None:
    """Delete a local git branch.

    Args:
        repo_path: Absolute path of the main git repository.
        branch: Branch name to delete.
        force: Use ``-D`` (force-delete) instead of ``-d`` (safe-delete).
            ``-d`` refuses to delete an unmerged branch; ``-D`` always deletes.

    Raises:
        GitError: If the branch deletion fails (e.g. unmerged with force=False).
    """
    flag = "-D" if force else "-d"
    await run_git("branch", flag, branch, cwd=repo_path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _branch_exists(repo_path: Path, branch: str) -> bool:
    """Return True if *branch* exists as a local git ref."""
    result = await run_git(
        "branch",
        "--list",
        branch,
        cwd=repo_path,
        check=False,
    )
    return bool(result.stdout.strip())
