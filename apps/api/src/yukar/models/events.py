"""RunEvent discriminated union — spec §8 SSE payload.

Each event type uses `type` as the discriminator field.
The full union is exposed via GET /api/_schema/run-event so the OpenAPI schema
covers SSE payload types (architecture §3.4 risk②).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from yukar.models.epic import EpicStatus


class BaseEvent(BaseModel):
    project_id: str
    epic_id: str
    run_id: str
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RunPreparingEvent(BaseEvent):
    """Emitted when a run enters the index-refresh phase (before Manager starts).

    Published by the supervisor inside ``_run_with_semaphore`` immediately
    before ``_ensure_repos_indexed`` is awaited.  Lets the UI show a
    "preparing / indexing" indicator while the run is not yet fully started.

    This event is intentionally lightweight: it does NOT update state.yaml
    (orchestrator owns state.yaml; epic.yaml's status is user-owned via
    PATCH — runs never write it).  The frontend
    can use it as a transient "preparing" signal until ``RunStartedEvent``
    arrives from the orchestrator.
    """

    type: Literal["run_preparing"] = "run_preparing"


class RunStartedEvent(BaseEvent):
    type: Literal["run_started"] = "run_started"


class RunCompletedEvent(BaseEvent):
    type: Literal["run_completed"] = "run_completed"


class RunFailedEvent(BaseEvent):
    type: Literal["run_failed"] = "run_failed"
    error: str = ""


class RunStoppedEvent(BaseEvent):
    """User-initiated stop (CancelledError).  The run halts and the epic becomes
    re-runnable (state.yaml → ``idle``); not a failure.  Distinct from
    ``run_completed`` so the UI can show "stopped" rather than "completed"."""

    type: Literal["run_stopped"] = "run_stopped"


class RunPausedEvent(BaseEvent):
    type: Literal["run_paused"] = "run_paused"


class RunResumedEvent(BaseEvent):
    type: Literal["run_resumed"] = "run_resumed"


class TaskUpdateEvent(BaseEvent):
    type: Literal["task_update"] = "task_update"
    task_id: str
    status: str
    title: str = ""


class WorkerStartedEvent(BaseEvent):
    type: Literal["worker_started"] = "worker_started"
    worker_id: str
    task_id: str | None = None
    repo: str | None = None


class WorkerCompletedEvent(BaseEvent):
    type: Literal["worker_completed"] = "worker_completed"
    worker_id: str
    task_id: str | None = None
    repo: str | None = None


class WorkerFailedEvent(BaseEvent):
    type: Literal["worker_failed"] = "worker_failed"
    worker_id: str
    task_id: str | None = None
    repo: str | None = None
    reason: str = ""  # e.g. "max_tokens" / "context_overflow" / exception class name


class EvalResultEvent(BaseEvent):
    type: Literal["eval_result"] = "eval_result"
    worker_id: str
    eval_id: str = ""
    accepted: bool
    feedback: str = ""


class TokenEvent(BaseEvent):
    type: Literal["token"] = "token"
    thread_id: str
    delta: str
    # Utterance-segment index: all events of one assistant message share the same value (issue②).
    msg_index: int = 0


class ToolCallEvent(BaseEvent):
    type: Literal["tool_call"] = "tool_call"
    thread_id: str
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    # Strands toolUseId — used by the frontend to correlate ToolCallEvent with
    # the matching ToolResultEvent (same tool_use_id on both sides).
    tool_use_id: str = ""
    # Utterance-segment index: all events of one assistant message share the same value (issue②).
    msg_index: int = 0


class ToolResultEvent(BaseEvent):
    type: Literal["tool_result"] = "tool_result"
    thread_id: str
    tool_name: str
    result: str = ""
    # Strands toolUseId — correlates this result with its originating ToolCallEvent.
    tool_use_id: str = ""
    # Utterance-segment index: all events of one assistant message share the same value (issue②).
    msg_index: int = 0


class DiffUpdateEvent(BaseEvent):
    type: Literal["diff_update"] = "diff_update"
    repo: str
    files_changed: int = 0


class TokenUsageEvent(BaseEvent):
    """Emitted after each LLM invocation (per cycle) and embedding batch."""

    type: Literal["token_usage"] = "token_usage"
    role: str  # manager / worker / evaluator / embedding
    model_id: str
    delta: dict[str, int]  # {input, output, cache_read, cache_write, embedding}
    run_totals: dict[str, Any]  # {input_tokens, output_tokens, …, cost_usd, cost_jpy}
    global_totals: dict[str, Any]  # {cost_usd, cost_jpy, budget_limit_usd, budget_spent_usd, …}


class BudgetExceededEvent(BaseEvent):
    """Fired once when the current month's USD spend reaches the monthly budget.

    spent_usd is the month-to-date USD spend; limit_usd is the monthly USD budget.
    """

    type: Literal["budget_exceeded"] = "budget_exceeded"
    spent_usd: float
    limit_usd: float


# ---------------------------------------------------------------------------
# New events (UX fix A1)
# ---------------------------------------------------------------------------


class ManagerTurnStartedEvent(BaseEvent):
    """Emitted at the start of each Manager streaming turn.

    Signals that the Manager is currently thinking/speaking.
    ``turn`` is the 0-based turn index within the current run.
    """

    type: Literal["manager_turn_started"] = "manager_turn_started"
    turn: int


class ManagerMessageEvent(BaseEvent):
    """Emitted after each Manager turn completes with the final natural-language text.

    This is a live-visibility event only — the canonical message is already
    persisted in the Strands session (FileSessionManager).  Do NOT use this
    to reconstruct session state.
    """

    type: Literal["manager_message"] = "manager_message"
    thread_id: str  # thread_id of the Manager trial
    turn: int
    text: str


class DelegationItem(BaseModel):
    """One task entry within a DelegationEvent."""

    task_id: str
    repo: str | None = None
    title: str | None = None


class DelegationEvent(BaseEvent):
    """Emitted when the Manager calls ``dispatch()``, before Workers start.

    Signals that tasks have been delegated and Workers are about to begin.
    Useful for showing a "delegated / waiting" state in the UI tree.
    """

    type: Literal["delegation"] = "delegation"
    items: list[DelegationItem]


class EvaluatorStartedEvent(BaseEvent):
    """Emitted when an Evaluator agent is launched for a Worker's output."""

    type: Literal["evaluator_started"] = "evaluator_started"
    eval_id: str
    worker_id: str
    task_id: str
    repo: str


class PauseEffectiveEvent(BaseEvent):
    """Emitted inside ``_checkpoint()`` immediately before actually blocking on _paused.

    Distinct from ``RunPausedEvent`` (which fires when the pause API call is received).
    This event confirms the run *actually stopped* at a checkpoint.
    """

    type: Literal["pause_effective"] = "pause_effective"


class UserInputRequestedEvent(BaseEvent):
    """Emitted when the Manager calls ``ask_user()`` and the run enters awaiting_input.

    The run blocks until the user sends a reply via POST /threads/{thread_id}/messages.
    ``question`` is the natural-language question/plan summary the Manager wants to
    present to the user.  ``thread_id`` is the Manager trial's thread_id.
    """

    type: Literal["user_input_requested"] = "user_input_requested"
    thread_id: str
    question: str


class UserInputResolvedEvent(BaseEvent):
    """Emitted when the Manager receives the user's reply and resumes from awaiting_input.

    Paired with ``UserInputRequestedEvent`` — publish this immediately after the run
    transitions back to ``running``.  Because both events land in the replay buffer,
    a subscriber that reconnects mid-run will receive request→resolved in order,
    so the final replayed state is ``running`` rather than ``awaiting_input``.

    ``thread_id`` is the Manager trial's thread_id.
    """

    type: Literal["user_input_resolved"] = "user_input_resolved"
    thread_id: str


# ---------------------------------------------------------------------------
# Epic lifecycle events (Close / Merge)
# ---------------------------------------------------------------------------


class EpicStatusChangedEvent(BaseEvent):
    """Emitted when the user flips the epic's 1-bit status (open ⇄ completed).

    The only writer is ``PATCH /epics/{id}`` — the epic status is user-owned
    and never transitions automatically.  Pass ``run_id=""`` (there is no
    active run when the status is changed; a completed-switch is rejected
    while a run is active).
    """

    type: Literal["epic_status_changed"] = "epic_status_changed"
    status: EpicStatus  # the new epic status


class EpicMergedEvent(BaseEvent):
    """Emitted once when the epic's merge fact is recorded (``merged_at`` set).

    Fired by the shared helper when EVERY repo's epic branch has been merged
    into its default branch (single-repo merge via ``POST /git/merge`` or a
    batch arbiter merge).  This is a fact notification, not a lifecycle
    transition: the epic stays open and the UI shows a "merged" badge.
    Idempotent at the source — recorded (and published) at most once per epic.
    """

    type: Literal["epic_merged"] = "epic_merged"
    merged_at: datetime


class EpicMergeResult(BaseModel):
    """Per-epic outcome within a batch merge operation."""

    epic_id: str
    status: Literal["merged", "conflict_unresolved", "vetting_refused", "skipped", "error"]
    detail: str = ""
    repos: list[str] = Field(default_factory=list)


class EpicMergeProgressEvent(BaseEvent):
    """Progress event for a batch merge operation (arbiter — Feature 2).

    Published repeatedly during a merge run so the UI can show a live progress
    panel.  ``run_id`` is the arbiter run id (pass "" before it is assigned).
    ``current_epic_id`` is the epic currently being processed (``None`` when
    between epics or after completion).
    ``phase`` is a free-form string describing the current step:
    "started" | "resolving" | "merging" | "epic_done" | "finished".
    ``results`` accumulates as each epic finishes.
    """

    type: Literal["epic_merge_progress"] = "epic_merge_progress"
    total: int
    completed: int
    current_epic_id: str | None = None
    phase: str = ""
    results: list[EpicMergeResult] = Field(default_factory=list)


class UserMessageCommittedEvent(BaseEvent):
    """Emitted by the FSM hook immediately after a user message is persisted.

    This event fires once per committed user message (HITL inject or seed) on
    the manager thread.  ``message_id`` is the FSM-assigned index of the message
    in the agent's session (0-based, monotonically increasing).  ``text`` is the
    plain-text content of the message.

    This is a live-visibility event — the canonical message is already
    persisted by FileSessionManager.  Do NOT use this to reconstruct session
    state; use GET /threads/{thread_id} for that.

    Filtering guarantees (enforced by the publisher in orchestrator.py):
    - Only ``role == "user"`` messages are published.
    - Messages whose content contains a ``toolResult`` block are excluded
      (those are tool-use reply frames, not human-authored text).
    - Assistant messages are NOT published.
    - Orchestrator-generated planning prompts (Epic initialisation boilerplate,
      task-state summaries, resume instructions) are NOT published — only
      genuine human-authored text (HITL inject, ask_user reply, seed_prompt
      from user) triggers this event.
    """

    type: Literal["user_message_committed"] = "user_message_committed"
    thread_id: str  # thread_id of the Manager trial
    text: str
    message_id: int  # FSM-assigned index (0-based)


# ---------------------------------------------------------------------------
# Security-visibility events (A3-01/-02)
# ---------------------------------------------------------------------------


class SensitiveFileWrittenEvent(BaseEvent):
    """Emitted when the Manager writes a sensitive prompt-injection surface.

    Covers: ``write_agent_config``, ``write_skill``, ``write_agent_profile``,
    ``remember``, and ``complete_epic`` learnings.  These files are appended
    verbatim to agent system prompts or future Manager turns, so unexpected
    writes by a prompt-injected Manager are surfaced here for operator visibility.

    ``kind`` identifies the type of write:
    - ``"agent_config"``   → per-role custom instructions (.yukar/agents/{role}.md)
    - ``"skill"``          → project skill (skills/{name}/SKILL.md)
    - ``"agent_profile"``  → named agent profile (.yukar/agent_profiles/{name}.yaml)
    - ``"memory"``         → project memory entry (remember() or complete_epic learning)

    ``name`` is the role/skill name/profile name/memory-category for context.
    """

    type: Literal["sensitive_file_written"] = "sensitive_file_written"
    kind: Literal["agent_config", "skill", "agent_profile", "memory"]
    name: str  # role / skill name / profile name / memory category


# Discriminated union — used by GET /api/_schema/run-event
RunEvent = Annotated[
    RunPreparingEvent
    | RunStartedEvent
    | RunCompletedEvent
    | RunFailedEvent
    | RunStoppedEvent
    | RunPausedEvent
    | RunResumedEvent
    | TaskUpdateEvent
    | WorkerStartedEvent
    | WorkerCompletedEvent
    | WorkerFailedEvent
    | EvalResultEvent
    | TokenEvent
    | ToolCallEvent
    | ToolResultEvent
    | DiffUpdateEvent
    | TokenUsageEvent
    | BudgetExceededEvent
    | ManagerTurnStartedEvent
    | ManagerMessageEvent
    | DelegationEvent
    | EvaluatorStartedEvent
    | PauseEffectiveEvent
    | UserInputRequestedEvent
    | UserInputResolvedEvent
    | EpicStatusChangedEvent
    | EpicMergedEvent
    | EpicMergeProgressEvent
    | UserMessageCommittedEvent
    | SensitiveFileWrittenEvent,
    Field(discriminator="type"),
]
