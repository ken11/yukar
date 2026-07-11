"""Merge router — batch-merge arbiter (Feature 2).

Endpoints:
  POST /api/projects/{project_id}/merge        → start batch merge, 202
  POST /api/projects/{project_id}/merge/stop   → stop batch merge, 200
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from yukar.api.routers import get_epic_or_404, get_project_or_404
from yukar.deps import SupervisorDep, UsageTrackerDep, WorkspaceRootDep

router = APIRouter(
    prefix="/api/projects/{project_id}",
    tags=["merge"],
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class StartMergeRequest(BaseModel):
    epic_ids: list[str]


class StartMergeResponse(BaseModel):
    run_id: str
    status: str = "started"


class StopMergeResponse(BaseModel):
    status: str = "stopped"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/merge", response_model=StartMergeResponse, status_code=202)
async def start_merge(
    project_id: str,
    body: StartMergeRequest,
    root: WorkspaceRootDep,
    supervisor: SupervisorDep,
    usage_tracker: UsageTrackerDep,
) -> StartMergeResponse:
    """Start a batch-merge arbiter run for the selected epics (Feature 2).

    Merges each epic's branch into the default branch in order, using an
    agent to resolve any conflicts.  Progress is streamed via
    ``GET /api/projects/{project_id}/events`` as ``EpicMergeProgressEvent``.

    Returns 202 on success with the arbiter ``run_id``.
    Returns 409 if:
    - an arbiter is already running for this project, OR
    - any selected epic has an active run.

    Returns 400 if ``epic_ids`` is empty.
    """
    if not body.epic_ids:
        raise HTTPException(status_code=400, detail="epic_ids must not be empty")

    # Validate project exists.
    await get_project_or_404(root, project_id)

    # Validate each epic exists and is still open — a merge mutates the
    # default branch, and completed epics are read-only until reopened
    # (same rule as the single-repo POST /git/merge).
    for epic_id in body.epic_ids:
        epic = await get_epic_or_404(root, project_id, epic_id)
        if epic.status == "completed":
            raise HTTPException(
                status_code=409,
                detail=f"Epic {epic_id} is completed — reopen it before merging",
            )

    if supervisor.is_arbiter_running(project_id):
        raise HTTPException(
            status_code=409,
            detail="A merge (arbiter) is already running for this project",
        )

    if usage_tracker.is_over_budget():
        raise HTTPException(status_code=409, detail="Budget limit reached")

    try:
        run_id = await supervisor.start_merge(
            root=root,
            project_id=project_id,
            epic_ids=body.epic_ids,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return StartMergeResponse(run_id=run_id)


@router.post("/merge/stop", response_model=StopMergeResponse)
async def stop_merge(
    project_id: str,
    supervisor: SupervisorDep,
) -> StopMergeResponse:
    """Stop the active batch-merge arbiter run for this project.

    Returns 404 if no arbiter is currently running.
    """
    if not supervisor.is_arbiter_running(project_id):
        raise HTTPException(
            status_code=404,
            detail="No merge (arbiter) is running for this project",
        )

    await supervisor.stop_merge(project_id)
    return StopMergeResponse()
