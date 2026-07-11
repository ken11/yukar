"""Schema endpoint — exposes RunEvent union for OpenAPI / type generation.

GET /api/_schema/run-event
Returns a dummy RunEvent response so the full discriminated union appears
in the OpenAPI schema. Frontend type generation (pnpm gen:types) uses this
to derive SSE payload types (architecture §3.4 risk②).
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from yukar.models.events import (
    BudgetExceededEvent,
    DelegationEvent,
    DiffUpdateEvent,
    EpicMergedEvent,
    EpicMergeProgressEvent,
    EpicStatusChangedEvent,
    EvalResultEvent,
    EvaluatorStartedEvent,
    ManagerMessageEvent,
    ManagerTurnStartedEvent,
    PauseEffectiveEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunPausedEvent,
    RunPreparingEvent,
    RunResumedEvent,
    RunStartedEvent,
    RunStoppedEvent,
    SensitiveFileWrittenEvent,
    TaskUpdateEvent,
    TokenEvent,
    TokenUsageEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserInputRequestedEvent,
    UserInputResolvedEvent,
    UserMessageCommittedEvent,
    WorkerCompletedEvent,
    WorkerFailedEvent,
    WorkerStartedEvent,
)

router = APIRouter(prefix="/api/_schema", tags=["schema"])


class RunEventSchema(BaseModel):
    """Wrapper that holds one of each event type so all appear in OpenAPI."""

    run_preparing: RunPreparingEvent | None = None
    run_started: RunStartedEvent | None = None
    run_completed: RunCompletedEvent | None = None
    run_failed: RunFailedEvent | None = None
    run_stopped: RunStoppedEvent | None = None
    run_paused: RunPausedEvent | None = None
    run_resumed: RunResumedEvent | None = None
    task_update: TaskUpdateEvent | None = None
    worker_started: WorkerStartedEvent | None = None
    worker_completed: WorkerCompletedEvent | None = None
    worker_failed: WorkerFailedEvent | None = None
    eval_result: EvalResultEvent | None = None
    token: TokenEvent | None = None
    tool_call: ToolCallEvent | None = None
    tool_result: ToolResultEvent | None = None
    diff_update: DiffUpdateEvent | None = None
    token_usage: TokenUsageEvent | None = None
    budget_exceeded: BudgetExceededEvent | None = None
    # New events (UX fix A1)
    manager_turn_started: ManagerTurnStartedEvent | None = None
    manager_message: ManagerMessageEvent | None = None
    delegation: DelegationEvent | None = None
    evaluator_started: EvaluatorStartedEvent | None = None
    pause_effective: PauseEffectiveEvent | None = None
    # HITL approval gate
    user_input_requested: UserInputRequestedEvent | None = None
    user_input_resolved: UserInputResolvedEvent | None = None
    # Epic lifecycle (user status toggle / merge fact)
    epic_status_changed: EpicStatusChangedEvent | None = None
    epic_merged: EpicMergedEvent | None = None
    epic_merge_progress: EpicMergeProgressEvent | None = None
    # HITL user message committed (PR-B)
    user_message_committed: UserMessageCommittedEvent | None = None
    # Security: sensitive-file (agent config / skill / profile / memory) write
    sensitive_file_written: SensitiveFileWrittenEvent | None = None


@router.get("/run-event", response_model=RunEventSchema)
async def run_event_schema() -> RunEventSchema:
    """Dummy endpoint — only exists to anchor all RunEvent types in OpenAPI."""
    return RunEventSchema()
