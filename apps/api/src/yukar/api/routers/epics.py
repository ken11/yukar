"""Epics router — CRUD under /api/projects/{p}/epics/."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from yukar.api.routers import get_epic_or_404, get_project_or_404, get_repo_or_404, shelve_or_409
from yukar.config import paths as p
from yukar.config.paths import PathSegmentError
from yukar.deps import SupervisorDep, WorkspaceRootDep
from yukar.events import bus as event_bus
from yukar.git.runner import run_git
from yukar.git.worktree import remove_worktree
from yukar.models.epic import Epic
from yukar.models.events import EpicStatusChangedEvent
from yukar.models.run import RunStatus
from yukar.storage import epic_repo, project_repo, state_repo
from yukar.storage.trash import discard_to_trash

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects/{project_id}/epics", tags=["epics"])


def _slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:50]


# ---------------------------------------------------------------------------
# Request/Response
# ---------------------------------------------------------------------------


class CreateEpicRequest(BaseModel):
    title: str
    description: str = ""
    acceptance_criteria: str = ""
    manager_effort: Literal["high", "xhigh", "max"] = "high"


class PatchEpicRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    acceptance_criteria: str | None = None
    # The epic lifecycle is a single user-owned bit: "open" reopens the epic,
    # "completed" finishes it (including abandoning unfinished work).
    status: Literal["open", "completed"] | None = None
    manager_effort: Literal["high", "xhigh", "max"] | None = None


class ArchiveEpicsRequest(BaseModel):
    epic_ids: list[str]


ArchiveErrorCode = Literal[
    "not_found", "invalid_id", "run_active", "merge_active", "dest_exists", "worktree_failed",
    "internal",
]


class EpicArchiveResult(BaseModel):
    """Per-epic outcome of a batch archive; errors never fail the whole batch.

    ``error_code`` is a stable machine-readable code so the frontend can
    localise the expected failures; ``error`` stays the human-readable detail.
    """

    epic_id: str
    archived: bool
    error: str | None = None
    error_code: ArchiveErrorCode | None = None


class RunSummary(BaseModel):
    """Digest of an epic's state.yaml, embedded in the epic-list response.

    Lets the board render "your turn" markers (``status == "waiting"`` with a
    non-empty ``run_id``) without N+1 ``GET /run/state`` calls.  Pure current
    state — there is no read/unread persistence.
    """

    status: RunStatus
    run_id: str
    # The conversation thread the run rides on (RunState.thread_id).
    thread_id: str | None = None
    # Which conversation agent the user would be replying to.
    role: Literal["manager", "reviewer"] = "manager"
    last_event_at: datetime | None = None


class EpicWithRunSummary(Epic):
    """Epic + run digest for the list endpoint.

    The storage model (``Epic`` / epic.yaml) is unchanged — ``run_summary`` is
    derived from state.yaml at read time and never persisted.  ``None`` means
    the epic has no state.yaml yet (never run) or it could not be read.
    """

    run_summary: RunSummary | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def _load_run_summary(root: str, project_id: str, epic_id: str) -> RunSummary | None:
    """Read one epic's state.yaml into a RunSummary; degrade to None on failure.

    A corrupt state.yaml must not kill the whole epic list (log-and-degrade —
    the same lesson as the EP-6 disappearance bug): the epic is still listed,
    just without a run digest.
    """
    try:
        state = await state_repo.get_state(root, project_id, epic_id)
    except Exception:
        logger.warning(
            "Unreadable state.yaml for epic %s/%s — listing it without a run summary",
            project_id,
            epic_id,
            exc_info=True,
        )
        return None
    if state is None:
        return None
    return RunSummary(
        status=state.status,
        run_id=state.run_id,
        thread_id=state.thread_id,
        role=state.role,
        last_event_at=state.last_event_at,
    )


@router.get("", response_model=list[EpicWithRunSummary])
async def list_epics(
    project_id: str,
    root: WorkspaceRootDep,
    include_completed: bool = False,
) -> list[EpicWithRunSummary]:
    """List epics with a per-epic run digest (``run_summary``).

    ``run_summary`` is derived from each epic's state.yaml (read concurrently);
    it is ``null`` for epics that have never run or whose state.yaml cannot be
    read.  The epic storage model itself is unchanged.
    """
    await get_project_or_404(root, project_id)
    epics = await epic_repo.list_epics(root, project_id)
    if not include_completed:
        epics = [e for e in epics if e.status != "completed"]
    summaries = await asyncio.gather(
        *(_load_run_summary(root, project_id, e.id) for e in epics)
    )
    return [
        EpicWithRunSummary(**epic.model_dump(), run_summary=summary)
        for epic, summary in zip(epics, summaries, strict=True)
    ]


@router.post("", response_model=Epic, status_code=201)
async def create_epic(project_id: str, body: CreateEpicRequest, root: WorkspaceRootDep) -> Epic:
    await get_project_or_404(root, project_id)

    counter = await project_repo.increment_epic_counter(root, project_id)
    epic_id = epic_repo.make_epic_id(counter)
    slug = _slugify(body.title)
    branch = epic_repo.make_branch_name(epic_id, slug)

    now = datetime.now(UTC)
    epic = Epic(
        id=epic_id,
        slug=slug,
        title=body.title,
        description=body.description,
        acceptance_criteria=body.acceptance_criteria,
        status="open",
        branch=branch,
        touched_repos=[],
        manager_effort=body.manager_effort,
        created_at=now,
        updated_at=now,
    )
    await epic_repo.save_epic(root, project_id, epic)
    return epic


@router.get("/{epic_id}", response_model=Epic)
async def get_epic(project_id: str, epic_id: str, root: WorkspaceRootDep) -> Epic:
    return await get_epic_or_404(root, project_id, epic_id)


async def _apply_epic_patch(
    root: str, project_id: str, epic_id: str, body: PatchEpicRequest
) -> Epic:
    """Load, mutate, persist, and announce an epic patch (shared by both paths)."""
    epic = await get_epic_or_404(root, project_id, epic_id)
    previous_status = epic.status
    if body.title is not None:
        epic.title = body.title
    if body.description is not None:
        epic.description = body.description
    if body.acceptance_criteria is not None:
        epic.acceptance_criteria = body.acceptance_criteria
    if body.status is not None:
        epic.status = body.status
    if body.manager_effort is not None:
        epic.manager_effort = body.manager_effort
    epic.updated_at = datetime.now(UTC)
    await epic_repo.save_epic(root, project_id, epic)
    if body.status is not None and epic.status != previous_status:
        event_bus.publish(
            project_id,
            epic_id,
            EpicStatusChangedEvent(
                project_id=project_id,
                epic_id=epic_id,
                run_id="",
                status=epic.status,
            ),
        )
    return epic


@router.patch("/{epic_id}", response_model=Epic)
async def patch_epic(
    project_id: str,
    epic_id: str,
    body: PatchEpicRequest,
    root: WorkspaceRootDep,
    supervisor: SupervisorDep,
) -> Epic:
    # Guard: completing an epic (the user's single "finish" action — approving
    # done work or abandoning unfinished work) must not race an in-flight run.
    # The whole check + write runs inside the supervisor's run-start lock so a
    # concurrent run start cannot slip between the guard and the status write
    # (TOCTOU closed): start/start_continuation re-read
    # epic.status under the same lock.  An EXECUTING turn is a 409; a live run
    # merely parked in ``waiting`` is shelved (state preserved) before the
    # epic is completed.  Reopening ("open") needs no guard.
    if body.status == "completed":
        async with supervisor.epic_mutation_lock():
            if supervisor.is_executing(project_id, epic_id):
                raise HTTPException(
                    status_code=409, detail="A run is executing — completing is not allowed"
                )
            await shelve_or_409(supervisor, project_id, epic_id)
            return await _apply_epic_patch(root, project_id, epic_id, body)
    return await _apply_epic_patch(root, project_id, epic_id, body)


# ---------------------------------------------------------------------------
# Archive (move out of sight — NOT a status)
# ---------------------------------------------------------------------------


async def _remove_all_worktrees(root: str, project_id: str, epic_id: str) -> str | None:
    """Deregister and delete every trial worktree of an epic.

    Worktrees are registered by ABSOLUTE path in each source repo's
    ``.git/worktrees/``; moving the epic directory with a live registration
    would leave a stale entry that blocks checking out the branch elsewhere.
    Returns an error message on failure, ``None`` on success.
    """
    wt_root = p.worktrees_dir(root, project_id, epic_id)
    if not wt_root.is_dir():
        return None
    for trial_dir in sorted(d for d in wt_root.iterdir() if d.is_dir()):
        for wt_path in sorted(d for d in trial_dir.iterdir() if d.is_dir()):
            repo_name = wt_path.name
            try:
                repo_info = await get_repo_or_404(root, project_id, repo_name)
            except HTTPException:
                # Repo no longer configured in the project — there is no repo
                # to deregister from; just drop the orphan checkout.
                if not await discard_to_trash(root, wt_path):
                    await asyncio.to_thread(shutil.rmtree, wt_path, ignore_errors=True)
                continue
            repo_path = Path(repo_info.path)
            # Fast path: deleting the checkout in-request can take tens of
            # seconds (node_modules etc.) and times the request out at the
            # Next.js proxy.  Rename it into the workspace trash (instant,
            # deleted in the background) and prune the now-dangling
            # registration — same net effect as `git worktree remove --force`.
            if await discard_to_trash(root, wt_path):
                # A stale registration blocks checking out the branch elsewhere,
                # so a failed prune must fail the archive like the slow path did.
                prune = await run_git("worktree", "prune", cwd=repo_path, check=False)
                if not prune.ok:
                    error = prune.stderr.strip() or f"rc={prune.returncode}"
                    return f"worktree prune failed for {repo_name}: {error}"
                continue
            # Rename failed (should not happen on a same-volume workspace) —
            # fall back to the synchronous removal.
            removed, wt_error = await remove_worktree(
                repo_path=repo_path, worktree_path=wt_path, force=True
            )
            if not removed and "is not a working tree" in (wt_error or ""):
                # Stale checkout git no longer recognises: delete it and prune
                # the (already broken) registration.
                await asyncio.to_thread(shutil.rmtree, wt_path, ignore_errors=True)
                await run_git("worktree", "prune", cwd=repo_path, check=False)
                removed = not wt_path.exists()
            if not removed:
                return f"worktree remove failed for {repo_name}: {wt_error}"
    return None


async def _archive_one_epic(
    root: str, project_id: str, epic_id: str, supervisor: SupervisorDep
) -> EpicArchiveResult:
    """Archive a single epic; every failure is captured as a per-epic error."""

    def _fail(msg: str, code: ArchiveErrorCode) -> EpicArchiveResult:
        return EpicArchiveResult(epic_id=epic_id, archived=False, error=msg, error_code=code)

    try:
        await get_epic_or_404(root, project_id, epic_id)
    except HTTPException as e:
        return _fail(f"epic not found ({e.detail})", "not_found")
    except PathSegmentError as e:
        # A malformed id must stay a per-epic error — letting it propagate
        # would 422 the whole batch after earlier epics already moved.
        return _fail(str(e), "invalid_id")

    try:
        # The whole check + move runs inside the run-start lock so a run cannot
        # start while the epic directory is being torn down (same TOCTOU guard
        # as completing an epic).
        async with supervisor.epic_mutation_lock():
            if supervisor.is_running(project_id, epic_id):
                return _fail("A run is active — archive is not allowed", "run_active")
            # A batch merge registers under the project-wide MERGE_SENTINEL key
            # (never the epic's own key) and works INSIDE the epics' trial
            # worktrees — archiving mid-merge would tear the ground out from
            # under the arbiter.  Same conservative project-wide guard as
            # start/start_resolve.
            if supervisor.is_arbiter_running(project_id):
                return _fail(
                    "A batch merge is in progress — archive is not allowed", "merge_active"
                )

            # Cheap failure conditions come BEFORE the destructive teardown so
            # an archive that cannot complete leaves worktrees untouched.
            src = p.epic_dir(root, project_id, epic_id)
            dest = p.archived_epic_dir(root, project_id, epic_id)
            if dest.exists():
                return _fail("archive destination already exists", "dest_exists")

            # A leaked dev server / browser session holding files inside the
            # epic directory would block worktree removal and the move.
            from yukar.preview import get_dev_server_manager
            from yukar.preview.browser import get_browser_session_manager

            browser_sessions = get_browser_session_manager()
            if browser_sessions is not None:
                with contextlib.suppress(Exception):
                    await browser_sessions.close_for_epic(project_id, epic_id)
            dev_manager = get_dev_server_manager()
            if dev_manager is not None:
                with contextlib.suppress(Exception):
                    await dev_manager.stop_for_epic(project_id, epic_id)

            wt_error = await _remove_all_worktrees(root, project_id, epic_id)
            if wt_error is not None:
                return _fail(wt_error, "worktree_failed")

            dest.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.move, str(src), str(dest))
    except Exception as e:
        logger.warning("Archive failed for %s/%s", project_id, epic_id, exc_info=True)
        return _fail(str(e), "internal")
    return EpicArchiveResult(epic_id=epic_id, archived=True)


@router.post("/archive", response_model=list[EpicArchiveResult])
async def archive_epics(
    project_id: str,
    body: ArchiveEpicsRequest,
    root: WorkspaceRootDep,
    supervisor: SupervisorDep,
) -> list[EpicArchiveResult]:
    """Move epics to ``archives/`` so they drop out of every listing.

    Archiving is a LOCATION, not a status: epic.yaml is untouched (the open ⇄
    completed bit stays user-owned) and the epic simply stops being enumerated
    because the list API scans ``epics/`` only.  Branches in the source repos
    are deliberately left alone (prune deletes them explicitly if wanted);
    trial worktrees ARE removed because their absolute-path registrations
    would go stale on the move.  There is no un-archive endpoint — moving the
    directory back into ``epics/`` by hand restores visibility.
    """
    await get_project_or_404(root, project_id)
    results: list[EpicArchiveResult] = []
    # Sequential on purpose: each epic serialises on the run-start lock anyway,
    # and per-epic results keep their request order.
    for epic_id in body.epic_ids:
        results.append(await _archive_one_epic(root, project_id, epic_id, supervisor))
    return results
