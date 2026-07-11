"""Git router — status, diff, commit, merge, resolve, prune, summary."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from yukar.api.routers import get_epic_or_404, get_repo_or_404, shelve_or_409
from yukar.config import paths as p
from yukar.deps import SettingsDep, SupervisorDep, UsageTrackerDep, WorkspaceRootDep
from yukar.git.diff import (
    GitVettingError,
    MergeConflictError,
    commit,
    get_diff,
    get_diff_summary,
    is_branch_merged,
    merge,
)
from yukar.git.runner import GitError
from yukar.git.status import get_status
from yukar.git.worktree import delete_branch, remove_worktree
from yukar.models.diff import DiffResult, DiffSummary, FileStat, RepoPruneResult
from yukar.models.epic import Epic
from yukar.runs.merge_facts import record_epic_merged
from yukar.storage.epic_repo import get_epic
from yukar.storage.project_repo import get_repo, list_repos

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/projects/{project_id}/epics/{epic_id}",
    tags=["git"],
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CommitRequest(BaseModel):
    message: str
    repo: str  # repo name


class MergeRequest(BaseModel):
    repo: str  # repo name
    message: str | None = None


class ResolveRequest(BaseModel):
    repo: str  # repo name


class PruneRequest(BaseModel):
    repos: list[str] | None = None  # None → use epic.touched_repos
    force: bool = False


class ResolveStarted(BaseModel):
    run_id: str
    status: str = "started"


# ---------------------------------------------------------------------------
# Existing endpoints
# ---------------------------------------------------------------------------


@router.get("/git/status", response_model=list[FileStat])
async def git_status(
    project_id: str,
    epic_id: str,
    root: WorkspaceRootDep,
    repo: str = Query(..., description="Repo name"),
) -> list[FileStat]:
    repo_info = await get_repo_or_404(root, project_id, repo)
    try:
        return await get_status(Path(repo_info.path))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/git/diff", response_model=DiffResult)
async def git_diff(
    project_id: str,
    epic_id: str,
    root: WorkspaceRootDep,
    mode: Literal["working", "epic"] = Query("working"),
    repo: str = Query(..., description="Repo name"),
) -> DiffResult:
    repo_info = await get_repo_or_404(root, project_id, repo)

    epic = await get_epic(root, project_id, epic_id)
    branch = epic.branch if epic else None

    try:
        return await get_diff(
            repo_path=Path(repo_info.path),
            mode=mode,
            repo_name=repo,
            branch=branch,
            default_branch=repo_info.default_branch,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/git/commit")
async def git_commit(
    project_id: str,
    epic_id: str,
    body: CommitRequest,
    root: WorkspaceRootDep,
    settings: SettingsDep,
) -> dict[str, str]:
    repo_info = await get_repo_or_404(root, project_id, body.repo)
    try:
        sha = await commit(
            repo_path=Path(repo_info.path),
            message=body.message,
            author_name=settings.git.author_name,
            author_email=settings.git.author_email,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"sha": sha}


async def _finalize_epic_if_all_merged(
    root: str,
    project_id: str,
    epic: Epic,
) -> None:
    """Record the merge fact (``merged_at``) once every repo is merged.

    Called after a successful single-repo merge (``POST /git/merge``).  Records
    the fact when EVERY repo in the project's repo list has the epic branch
    already merged into its default branch, or the branch does not exist in
    that repo (branch absent means it was never created or already pruned —
    both are semantically equivalent to "merged" for the per-repo check).

    The merge fact is an attribute, not a status: the epic stays open and only
    the user completes it.  Recording is idempotent — once ``merged_at`` is
    set, this function is a no-op (``record_epic_merged`` enforces it).

    Errors in the per-repo ancestor check are logged and treated as
    ``not-yet-merged`` (fail-safe: we never prematurely declare an epic merged).
    """
    # Idempotence: the merge fact is recorded (and announced) at most once.
    if epic.merged_at is not None:
        return

    branch = epic.branch
    if not branch:
        return

    # Fetch all repos registered in the project.
    all_repos = await list_repos(root, project_id)
    if not all_repos:
        return

    # For every repo, check whether the epic branch is merged into its default
    # branch.  We require ALL repos to pass, not just touched_repos, to avoid
    # declaring the epic merged when a repo was added after work started.
    # If branch is absent from a repo, that repo is considered merged (the
    # branch was pruned or never created there — common for single-repo epics
    # that also have a second repo registered in the project).
    for repo in all_repos:
        try:
            merged = await is_branch_merged(
                repo_path=Path(repo.path),
                branch=branch,
                default_branch=repo.default_branch,
            )
        except Exception:
            logger.warning(
                "is_branch_merged check failed for repo %s (epic %s); "
                "treating as not-yet-merged",
                repo.name,
                epic.id,
                exc_info=True,
            )
            return  # fail-safe: do not mark merged

        if not merged:
            return  # at least one repo is not yet merged

    # All repos are merged — record the merge fact (the epic stays open).
    logger.info("All repos merged for epic %s; recording merged_at", epic.id)
    await record_epic_merged(root, project_id, epic)


@router.post("/git/merge")
async def git_merge(
    project_id: str,
    epic_id: str,
    body: MergeRequest,
    root: WorkspaceRootDep,
    settings: SettingsDep,
    supervisor: SupervisorDep,
) -> dict[str, str]:
    # A merge needs the epic quiescent: an EXECUTING turn is a 409; a live run
    # merely parked in ``waiting`` is shelved (task cancelled, state.yaml stays
    # waiting, conversation intact) so the merge can proceed.
    if supervisor.is_executing(project_id, epic_id):
        raise HTTPException(status_code=409, detail="A run is executing for this epic")
    if supervisor.is_arbiter_running(project_id):
        raise HTTPException(
            status_code=409,
            detail="A batch merge (arbiter) is in progress for this project",
        )
    await shelve_or_409(supervisor, project_id, epic_id)

    epic = await get_epic(root, project_id, epic_id)
    if epic is None or not epic.branch:
        raise HTTPException(status_code=404, detail="Epic branch not found")
    # A merge mutates the default branch — completed epics are read-only
    # until the user reopens them (merge, then complete, is the normal order).
    if epic.status == "completed":
        raise HTTPException(
            status_code=409, detail="Epic is completed — reopen it before merging"
        )

    repo_info = await get_repo_or_404(root, project_id, body.repo)

    try:
        sha = await merge(
            repo_path=Path(repo_info.path),
            branch=epic.branch,
            message=body.message,
            author_name=settings.git.author_name,
            author_email=settings.git.author_email,
        )
    except MergeConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={"message": "Merge conflict", "conflicts": e.conflicts},
        ) from e
    except GitVettingError as e:
        raise HTTPException(
            status_code=422,
            detail=(
                "manual merge required: repo has custom git driver config "
                "(.gitattributes filter/merge/diff)"
            ),
        ) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    # After a successful single-repo merge, check if all repos are now merged.
    # Errors here must not fail the response — the merge itself succeeded.
    try:
        await _finalize_epic_if_all_merged(root, project_id, epic)
    except Exception:
        logger.warning(
            "Failed to check/finalize epic merge status for epic %s; "
            "merge result is unaffected",
            epic_id,
            exc_info=True,
        )

    return {"sha": sha}


# ---------------------------------------------------------------------------
# M4: Conflict resolution run
# ---------------------------------------------------------------------------


@router.post("/git/resolve", response_model=ResolveStarted, status_code=202)
async def git_resolve(
    project_id: str,
    epic_id: str,
    body: ResolveRequest,
    root: WorkspaceRootDep,
    supervisor: SupervisorDep,
    usage_tracker: UsageTrackerDep,
) -> ResolveStarted:
    """Start an agent-assisted conflict resolution run (spec §5.2).

    The agent merges the default branch INTO the epic worktree, resolves
    conflict markers, and creates a merge commit.  A subsequent
    ``POST /git/merge`` will then succeed cleanly.

    Returns 409 if a run is already active for this epic or the budget limit
    has been reached.
    """
    # Validate repo exists.
    await get_repo_or_404(root, project_id, body.repo)
    # Validate epic exists and is not completed (a conflict-resolve run mutates
    # the epic worktree — completed epics are read-only until reopened).
    epic = await get_epic_or_404(root, project_id, epic_id)
    if epic.status == "completed":
        raise HTTPException(
            status_code=409, detail="Epic is completed — reopen it before resolving conflicts"
        )

    # An EXECUTING turn blocks the resolve run; a live run parked in
    # ``waiting`` is shelved so the resolve run can take the slot.
    if supervisor.is_executing(project_id, epic_id):
        raise HTTPException(status_code=409, detail="A run is executing for this epic")
    if usage_tracker.is_over_budget():
        raise HTTPException(status_code=409, detail="Budget limit reached")
    await shelve_or_409(supervisor, project_id, epic_id)

    try:
        run_id = await supervisor.start_resolve(
            root=root,
            project_id=project_id,
            epic_id=epic_id,
            repo_name=body.repo,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    return ResolveStarted(run_id=run_id)


# ---------------------------------------------------------------------------
# M4: Worktree / branch prune
# ---------------------------------------------------------------------------


@router.post("/git/prune", response_model=list[RepoPruneResult])
async def git_prune(
    project_id: str,
    epic_id: str,
    body: PruneRequest,
    root: WorkspaceRootDep,
    supervisor: SupervisorDep,
) -> list[RepoPruneResult]:
    """Remove worktrees and delete epic branches for completed repos (spec §5.2).

    ``repos`` defaults to ``epic.touched_repos`` when omitted.
    ``force=True`` uses ``git branch -D`` and ``git worktree remove --force``
    so unmerged branches can be pruned.

    Returns per-repo structured results.  Individual errors are captured in
    ``error`` and do not cause a 500; the caller can inspect each entry.
    Returns 409 if a run is active.
    """
    if supervisor.is_running(project_id, epic_id):
        raise HTTPException(status_code=409, detail="A run is active — prune is not allowed")

    epic = await get_epic_or_404(root, project_id, epic_id)

    target_repos = body.repos if body.repos is not None else epic.touched_repos
    if not target_repos:
        return []

    results: list[RepoPruneResult] = []

    for repo_name in target_repos:
        worktree_removed = False
        branch_deleted = False
        error_msg: str | None = None

        try:
            repo_info = await get_repo_or_404(root, project_id, repo_name)
            repo_path = Path(repo_info.path)
            # Prune targets the active manager trial's worktree.
            # Falls back to "manager" for single-trial (backward-compatible) epics.
            # Additional archived trials are pruned via POST /threads/{thread_id}/archive.
            # Ghost-worktree guard is centralised in agents.trials.resolve_active_trial_id.
            from yukar.agents.trials import resolve_active_trial_id

            _resolved = await resolve_active_trial_id(root, project_id, epic_id, epic)
            if _resolved is None:
                results.append(
                    RepoPruneResult(
                        repo=repo_name,
                        worktree_removed=False,
                        branch_deleted=False,
                        error=(
                            "Epic has no active manager trial (all trials are archived); "
                            "prune skipped. Use POST /threads/{thread_id}/archive to prune "
                            "individual trial worktrees."
                        ),
                    )
                )
                continue
            active_trial_id: str = _resolved

            worktree_path = p.worktree_dir(root, project_id, epic_id, active_trial_id, repo_name)

            # Remove worktree first (must happen before branch delete).
            # remove_worktree never raises — inspect the returned tuple.
            worktree_removed, wt_error = await remove_worktree(
                repo_path=repo_path,
                worktree_path=worktree_path,
                force=body.force,
            )
            if wt_error:
                error_msg = f"worktree remove failed: {wt_error}"

            # Delete branch only if worktree was successfully removed.
            # Skipping here prevents losing a branch while the worktree
            # is still in an indeterminate state.
            if worktree_removed and epic.branch:
                try:
                    await delete_branch(
                        repo_path=repo_path,
                        branch=epic.branch,
                        force=body.force,
                    )
                    branch_deleted = True
                except GitError as e:
                    # Preserve the error per-repo; do not raise.
                    branch_error = f"branch delete failed: {e}"
                    error_msg = f"{error_msg}; {branch_error}" if error_msg else branch_error

        except HTTPException as e:
            error_msg = f"repo not found: {repo_name} ({e.detail})"
        except Exception as e:
            error_msg = str(e)

        results.append(
            RepoPruneResult(
                repo=repo_name,
                worktree_removed=worktree_removed,
                branch_deleted=branch_deleted,
                error=error_msg,
            )
        )

    return results


# ---------------------------------------------------------------------------
# M4: Multi-repo diff summary
# ---------------------------------------------------------------------------


@router.get("/git/diff/summary", response_model=DiffSummary)
async def git_diff_summary(
    project_id: str,
    epic_id: str,
    root: WorkspaceRootDep,
    mode: Literal["working", "epic"] = Query("working"),
) -> DiffSummary:
    """Return aggregated diff statistics across all touched repos (spec §5.3).

    Uses ``epic.touched_repos`` to determine which repos to include.
    Repos with no epic branch yet are included with zero counts (not an error).
    Returns an empty summary if ``touched_repos`` is empty.
    """
    epic = await get_epic_or_404(root, project_id, epic_id)

    if not epic.touched_repos:
        return DiffSummary(repos=[], total_files=0, total_added=0, total_deleted=0)

    repo_tuples: list[tuple[Path, str, str | None, str]] = []

    for repo_name in epic.touched_repos:
        repo_info = await get_repo(root, project_id, repo_name)
        if repo_info is None:
            logger.warning(
                "Repo %s not found in project %s; skipping summary", repo_name, project_id
            )
            continue
        branch = epic.branch if epic.branch else None
        repo_tuples.append((Path(repo_info.path), repo_name, branch, repo_info.default_branch))

    try:
        return await get_diff_summary(repo_tuples, mode=mode)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
