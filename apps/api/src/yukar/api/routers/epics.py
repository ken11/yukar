"""Epics router — CRUD under /api/projects/{p}/epics/."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from yukar.api.routers import get_epic_or_404, get_project_or_404, shelve_or_409
from yukar.deps import SupervisorDep, WorkspaceRootDep
from yukar.events import bus as event_bus
from yukar.models.epic import Epic
from yukar.models.events import EpicStatusChangedEvent
from yukar.models.run import RunStatus
from yukar.storage import epic_repo, project_repo, state_repo

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
