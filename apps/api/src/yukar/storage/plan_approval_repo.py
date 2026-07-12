"""Plan-approval CRUD — plan_approval.yaml per epic (lifecycle redesign).

The record binds the user's approval to a task-plan snapshot hash
(see ``yukar.models.task.compute_plan_hash``).  Absence of the file means
"not approved"; a record whose hash no longer matches the current plan is
treated as unapproved by readers, so a stale file is harmless.
"""

from __future__ import annotations

import asyncio

from yukar.config import paths
from yukar.models.task import PlanApproval
from yukar.storage.yaml_io import load_model_async, save_model


async def get_plan_approval(root: str, project_id: str, epic_id: str) -> PlanApproval | None:
    yaml_path = paths.plan_approval_yaml(root, project_id, epic_id)
    return await load_model_async(yaml_path, PlanApproval, default=None)


async def save_plan_approval(
    root: str, project_id: str, epic_id: str, approval: PlanApproval
) -> None:
    yaml_path = paths.plan_approval_yaml(root, project_id, epic_id)
    await save_model(yaml_path, approval)


async def delete_plan_approval(root: str, project_id: str, epic_id: str) -> None:
    """Remove the approval record.  Idempotent — missing file is a no-op."""
    yaml_path = paths.plan_approval_yaml(root, project_id, epic_id)
    await asyncio.to_thread(yaml_path.unlink, missing_ok=True)
