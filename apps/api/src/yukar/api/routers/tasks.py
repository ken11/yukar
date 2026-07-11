"""Tasks router — GET/PUT tasks and the plan-approval operations.

Plan approval (lifecycle redesign P2): approval is an explicit user
operation bound to a task-plan snapshot hash, not something an agent (or a
chat reply) can grant.  GET /tasks reports the current plan hash and the
approval state; POST /plan/approval records an approval for exactly the
hash the client saw (409 on mismatch — the TOCTOU guard against approving
a plan that changed underneath the user); DELETE /plan/approval revokes it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from yukar.api.routers import get_epic_or_404
from yukar.deps import WorkspaceRootDep
from yukar.models.task import PlanApproval, TasksFile, compute_plan_hash
from yukar.storage import plan_approval_repo, tasks_repo

router = APIRouter(
    prefix="/api/projects/{project_id}/epics/{epic_id}",
    tags=["tasks"],
)


class TasksResponse(TasksFile):
    """GET /tasks response — the stored TasksFile plus plan-approval state.

    ``plan_hash`` is computed by the backend only; clients echo it back to
    POST /plan/approval and never compute hashes themselves.
    """

    plan_hash: str
    approved_hash: str | None = None
    plan_approved: bool = False


class PlanApprovalRequest(BaseModel):
    """POST /plan/approval body — the plan hash the user is approving."""

    tasks_hash: str


async def _build_tasks_response(root: str, project_id: str, epic_id: str) -> TasksResponse:
    # Same switch the orchestrator's dispatch gate uses: when the approval
    # gate is disabled (YUKAR_REQUIRE_PLAN_APPROVAL=0) every plan counts as
    # approved, and the REST surface must agree — otherwise the UI renders an
    # approve banner for an approval nothing will ever check.
    from yukar.runs.supervisor import _resolve_require_plan_approval

    tasks_file = await tasks_repo.get_tasks(root, project_id, epic_id)
    approval = await plan_approval_repo.get_plan_approval(root, project_id, epic_id)
    plan_hash = compute_plan_hash(tasks_file.tasks)
    approved_hash = approval.tasks_hash if approval is not None else None
    plan_approved = approved_hash == plan_hash or not _resolve_require_plan_approval()
    return TasksResponse(
        tasks=tasks_file.tasks,
        progress=tasks_file.progress,
        plan_hash=plan_hash,
        approved_hash=approved_hash,
        plan_approved=plan_approved,
    )


@router.get("/tasks", response_model=TasksResponse)
async def get_tasks(project_id: str, epic_id: str, root: WorkspaceRootDep) -> TasksResponse:
    await get_epic_or_404(root, project_id, epic_id)
    return await _build_tasks_response(root, project_id, epic_id)


@router.put("/tasks", response_model=TasksFile)
async def put_tasks(
    project_id: str, epic_id: str, body: TasksFile, root: WorkspaceRootDep
) -> TasksFile:
    await get_epic_or_404(root, project_id, epic_id)
    await tasks_repo.save_tasks(root, project_id, epic_id, body)
    return body


@router.post("/plan/approval", response_model=PlanApproval)
async def approve_plan(
    project_id: str, epic_id: str, body: PlanApprovalRequest, root: WorkspaceRootDep
) -> PlanApproval:
    """Record the user's approval of the current task-plan snapshot.

    The submitted hash must match the hash of the plan as it exists NOW —
    a mismatch means the plan changed after the client rendered it, and the
    approval is refused with 409 so the user can review the updated plan.
    """
    await get_epic_or_404(root, project_id, epic_id)
    tasks_file = await tasks_repo.get_tasks(root, project_id, epic_id)
    if not tasks_file.tasks:
        # An empty plan is nothing to approve — recording one would be inert
        # (the first task_update changes the hash) but misleading.
        raise HTTPException(status_code=409, detail="Plan is empty — nothing to approve")
    current_hash = compute_plan_hash(tasks_file.tasks)
    if body.tasks_hash != current_hash:
        raise HTTPException(
            status_code=409,
            detail=(
                "Plan has changed since it was displayed — refresh the task "
                "list and approve the updated plan."
            ),
        )
    approval = PlanApproval(tasks_hash=current_hash, approved_at=datetime.now(UTC))
    await plan_approval_repo.save_plan_approval(root, project_id, epic_id, approval)
    return approval


@router.delete("/plan/approval", status_code=204)
async def revoke_plan_approval(project_id: str, epic_id: str, root: WorkspaceRootDep) -> None:
    """Revoke the recorded plan approval.  Idempotent."""
    await get_epic_or_404(root, project_id, epic_id)
    await plan_approval_repo.delete_plan_approval(root, project_id, epic_id)
