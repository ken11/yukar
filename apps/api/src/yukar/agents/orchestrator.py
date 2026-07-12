"""EpicOrchestrator — Agent-as-a-Tool implementation of RunnerProtocol.

Architecture
------------
One orchestrator per epic run.  It:

1. Owns the single ``FileSessionManager`` (invariant §6.4).
2. Builds a persistent Manager Agent that drives the Epic end-to-end.
3. Exposes two effector tools to the Manager:
   - ``task_update``   — create/update tasks.yaml
   - ``dispatch``      — execute one or more Worker+Evaluator attempts in parallel
4. Delegates token/tool events to ``StreamTranslator`` (bus).
5. Respects pause/resume (asyncio.Event) and stop (asyncio.CancelledError).

Manager loop (spec §6.2 Agent-as-a-Tool):
    Manager calls task_update() to build the plan.
    Manager calls dispatch([{task_id, repo?, feedback?}, ...]) to execute tasks.
    Host runs Worker+Evaluator per item, enforces sandbox/lease/budget/stop.
    Manager reads per-item verdict: accepted/rejected/blocked.
    Manager retries (with feedback), replans, or gives up per task.
    Questions, reports, and completion summaries are written in the message
    body — there is no completion tool: a conversation has no end.

Turn-end semantics (canonical description):
    A turn ends in exactly two ways, both the agent's own decision: keep
    calling tools (the turn continues), or stop calling tools (the turn
    ends).  EVERY ended turn parks the run in ``waiting`` — it is the user's
    turn — and the next user message resumes the conversation (live inject
    while the run task is alive, or a continuation run after it is gone).
    The host never injects a prompt to keep a run going: the only host-
    authored prompt after turn 0 does not exist — turn-0 initialisation
    (fresh run) and the explicit-restart resume prompt (continuation without
    a seed message) are the only instruction-bearing host prompts, both
    backed by an explicit user operation.  Host-driven infinite loops are
    structurally impossible: one user input drives exactly one turn.

HITL
----
``inject_message(thread_id, text)`` appends to a pending queue.  The
orchestrator drains the queue at each manager turn boundary and prepends the
messages to the next Manager prompt so the LLM sees the human's input.

Session ownership
-----------------
``FileSessionManager(session_id=epic_id, storage_dir=sessions_dir)`` is
created once.  Manager agent gets ``session_manager=fsm``.  Worker and
Evaluator agents do NOT get a session_manager — instead, after each turn, the
orchestrator manually calls ``session_store.append_message`` to record their
conversation into the session file under ``agent_{thread_id}/``.  This keeps
all messages inside the single session and visible via the threads API.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from strands import Agent
from strands.memory import MemoryManager
from strands.memory.types import MemoryInjectionConfig
from strands.session.file_session_manager import FileSessionManager

from yukar.agents.context import AgentContext
from yukar.agents.dispatch import (
    DispatchContext,
    OrchestratorHooks,
    run_dispatch,
)
from yukar.agents.dispatch import (
    register_agent_thread as _register_agent_thread,
)
from yukar.agents.evaluator import run_evaluator
from yukar.agents.mcp_manager import McpClientManager
from yukar.agents.orchestrator_tools import _make_task_update_tool
from yukar.agents.project_extras import (
    build_skills_plugin,
    overlay_profile_instructions,
    overlay_system_prompt,
)
from yukar.agents.prompts import (
    _MANAGER_SYSTEM_PROMPT,
    _REVIEWER_SYSTEM_PROMPT,
    _build_manager_prompt,
    _build_reviewer_prompt,
    _load_epic_docs,
    _load_project_docs,
    _summarise_tasks,
)
from yukar.agents.streaming import AgentUsageRecorder, StreamTranslator
from yukar.agents.tools.agent_config_tools import make_agent_config_tools
from yukar.agents.tools.agent_profile_tools import make_agent_profile_tools
from yukar.agents.tools.docs_tools import make_manager_docs_tools
from yukar.agents.tools.repo_tools import make_repo_tools
from yukar.agents.tools.skill_mcp_tools import make_skill_mcp_tools
from yukar.agents.worker import _extract_agent_final_text, run_worker
from yukar.config import paths as p
from yukar.config.settings import AgentSettings, EmbeddingSettings, LLMSettings, McpSettings
from yukar.events import bus as event_bus
from yukar.indexer.embedder import create_embedder
from yukar.llm.factory import create_conversation_manager, create_model
from yukar.memory.store import EmbedFailedError, ProjectMemoryStore
from yukar.models.epic import Epic
from yukar.models.events import (
    ManagerMessageEvent,
    ManagerTurnStartedEvent,
    PauseEffectiveEvent,
    RunFailedEvent,
    RunStartedEvent,
    RunStoppedEvent,
    UserMessageCommittedEvent,
    YourTurnEndedEvent,
    YourTurnEvent,
)
from yukar.models.roles import AgentRole
from yukar.models.run import RunState
from yukar.models.task import Task, TasksFile, compute_plan_hash
from yukar.models.thread import ThreadEntry
from yukar.runs.scheduler import WorkerScheduler
from yukar.storage import plan_approval_repo, state_repo, tasks_repo, threads_repo
from yukar.storage.epic_repo import get_epic

logger = logging.getLogger(__name__)

# Public re-exports: names that existing tests import from this module.
# _register_agent_thread is imported above and re-exported here so that
# ``from yukar.agents.orchestrator import _register_agent_thread`` works.
__all__ = [
    "EpicOrchestrator",
    "_MAX_ATTEMPTS_PER_TASK",
    "_MAX_MANAGER_TURNS",
    "_register_agent_thread",
]

# Maximum attempts per task before the host automatically blocks it.
# Replaces the removed _MAX_RETRIES (same semantic: attempt = Worker+Evaluator cycle).
_MAX_ATTEMPTS_PER_TASK = 3

# Maximum manager turns per run.  Under park-every-turn semantics one user
# input drives exactly one turn, so this is a pure cost backstop: reaching it
# ends the run task with the state left in ``waiting`` (not an error) and the
# conversation resumes as a continuation run on the next message.
_MAX_MANAGER_TURNS = 50


# ---------------------------------------------------------------------------
# FSM user-message hook helper
# ---------------------------------------------------------------------------


def _register_user_message_hook(
    *,
    manager_agent: Any,
    fsm: Any,
    project_id: str,
    epic_id: str,
    run_id: str,
    thread_id: str,
    pub: Any,
    human_turn_flag: list[bool],
) -> None:
    """Register a MessageAddedEvent hook on manager_agent to publish
    UserMessageCommittedEvent for each committed user (HITL/seed) message.

    The hook runs at SDK_LAST order to ensure it fires *after* FileSessionManager's
    own append_message callback (order=DEFAULT=0), so fsm._latest_agent_message
    already holds the newly assigned message_id when we read it.

    Filtering:
    - Only role == "user" messages are published.
    - Messages containing a toolResult content block are excluded
      (those are tool-reply frames, not human-authored text).
    - Planning/boilerplate turns are excluded via ``human_turn_flag[0]``.
    - Messages sent on a boilerplate/planning turn are excluded.
      The caller sets ``human_turn_flag[0] = True`` before ``stream_async``
      when the prompt is human-authored (HITL inject, a reply that woke a
      waiting run, or seed_prompt from user), and ``False`` for
      orchestrator-generated prompts (_build_manager_prompt, resume
      instructions).  This ensures only genuine human messages are published.
    """
    from strands.hooks.events import MessageAddedEvent
    from strands.hooks.registry import HookOrder

    def _on_message_added(event: MessageAddedEvent) -> None:
        # Skip boilerplate/planning turns — only publish human-authored messages.
        if not human_turn_flag[0]:
            return
        msg = event.message
        if msg.get("role") != "user":
            return
        content = msg.get("content", [])
        # Exclude messages that contain toolResult blocks — those are Strands
        # internal frames where role=="user" wraps a tool response payload.
        if any("toolResult" in block for block in content):
            return
        # Extract plain text from the message.
        text_parts = [block["text"] for block in content if "text" in block]
        text = "".join(text_parts)
        # Retrieve the FSM-assigned message_id from _latest_agent_message.
        # This is safe because we run at SDK_LAST (after FSM's own hook at DEFAULT).
        latest = fsm._latest_agent_message.get(event.agent.agent_id)
        message_id: int = latest.message_id if latest is not None else -1
        pub(
            UserMessageCommittedEvent(
                project_id=project_id,
                epic_id=epic_id,
                run_id=run_id,
                thread_id=thread_id,
                text=text,
                message_id=message_id,
            )
        )

    manager_agent.add_hook(_on_message_added, MessageAddedEvent, order=HookOrder.SDK_LAST)


# ---------------------------------------------------------------------------
# EpicOrchestrator
# ---------------------------------------------------------------------------


class EpicOrchestrator:
    """Strands-based RunnerProtocol implementation (Agent-as-a-Tool pattern).

    The Manager Agent drives the Epic via two effector tools:
    - ``task_update``: plan/replan tasks.yaml
    - ``dispatch``: execute Worker+Evaluator for one or more tasks in parallel
    Everything conversational (questions, reports, completion summaries) is
    plain message text; every ended turn parks the run in ``waiting``.

    Invariants:
    - ``FileSessionManager`` created once; owned here, not by sub-agents.
    - Worker is closed to its assigned worktree (AgentContext + PathGuard).
    - Evaluator is read-only by tool design.
    - ``asyncio.CancelledError`` propagates naturally on stop.
    - pause is implemented via ``_paused`` Event; checked at manager turn
      boundaries and inside each dispatch item.
    """

    def __init__(
        self,
        llm_settings: LLMSettings,
        git_author_name: str,
        git_author_email: str,
        indexer_service: Any | None = None,
        max_parallel_workers: int = 4,
        seed_prompt: str | None = None,
        is_continuation: bool = False,
        agent_settings: AgentSettings | None = None,
        mcp_settings: McpSettings | None = None,
        embedding_settings: EmbeddingSettings | None = None,
        manager_thread_id: str = "manager",
        require_plan_approval: bool = True,
        agent_role: AgentRole = "manager",
        review_context: str = "",
    ) -> None:
        self._llm = llm_settings
        self._git_author_name = git_author_name
        self._git_author_email = git_author_email
        self._agent_settings: AgentSettings = agent_settings or AgentSettings()
        self._mcp_settings: McpSettings = mcp_settings or McpSettings()
        self._embedding_settings: EmbeddingSettings = embedding_settings or EmbeddingSettings()
        # The manager-trial thread id — used to route HITL and worktree paths.
        # Defaults to "manager" for the single-trial (backward-compatible) case.
        self._manager_thread_id: str = manager_thread_id
        # Optional IndexerService — if provided, Manager/Worker get repo_search/
        # repo_summarize tools (spec §6.3). None means no index tools (no-op).
        self._indexer_service = indexer_service
        # Continuation mode: when True, turn-0 uses seed_prompt rather than
        # the standard epic planning prompt. FileSessionManager restores the
        # existing Strands session history so the Manager retains context.
        self._seed_prompt: str | None = seed_prompt
        self._is_continuation: bool = is_continuation

        # Pause/resume support.
        self._paused: asyncio.Event = asyncio.Event()
        self._paused.set()  # not paused initially

        # Guards against emitting more than one PauseEffectiveEvent per pause
        # cycle.  Multiple concurrent _checkpoint() callers (manager loop +
        # N worker tasks) would each emit PauseEffectiveEvent without this flag.
        # Reset to False in pause() so the next pause cycle is fresh.
        # Reset to False in resume() so a subsequent pause emits again.
        self._pause_effective_announced: bool = False

        # Stop flag — set by stop(); checked at task boundaries.
        self._stopped: bool = False

        # Cooperative turn slot (max_parallel_epics semaphore, injected by the
        # supervisor via set_turn_slot).  P3 rule: only an EXECUTING turn
        # holds a slot — the orchestrator acquires it before turn work and
        # releases it while parked in waiting, so long-parked conversations
        # cannot starve other epics' runs.  None → no slot management (tests
        # that drive the orchestrator directly).
        self._turn_slot: asyncio.Semaphore | None = None
        self._turn_slot_held: bool = False

        # Canonical run status as seen by this orchestrator.  Supervisor
        # calls pause()/resume() to keep this in sync so that worker
        # save_state calls do not overwrite the disk status written by the
        # supervisor (the "pause flicker" bug).
        # Values: "running" | "paused" (waiting/terminal are never set here —
        # dispatch only runs while a turn is executing).
        self._run_status: Literal["running", "paused"] = "running"

        # True while the run is parked in ``waiting`` (it is the user's turn).
        # The loop blocks on _pending_messages.get() while this is True.
        self._awaiting_user: bool = False

        # Plan-approval gate (prevents the Manager from dispatching Workers
        # before the user has approved the current task plan).
        #   - Approval is an EXPLICIT user operation (POST /plan/approval)
        #     recorded in plan_approval.yaml as a hash of the plan snapshot;
        #     a chat reply does NOT grant it.
        #   - dispatch is REJECTED by the host while the stored hash does not
        #     match the current plan.  There is no imperative invalidation:
        #     changing the plan changes its hash, so the approval simply no
        #     longer matches.
        #   - The gate reads plan_approval.yaml from disk on every check so an
        #     approval given while a run is live (or parked) takes effect on
        #     the next dispatch without any in-memory hand-off.
        # Reviewer mode: a read-only, conversational agent that reviews the active
        # trial's branch and reports to the user.  It reuses this orchestrator's
        # conversation loop but with a read-only toolset and no task/dispatch/plan
        # machinery.  review_context carries the Manager↔user conversation to seed.
        self._agent_role: AgentRole = agent_role
        self._review_context: str = review_context
        _is_reviewer = agent_role == "reviewer"

        # A reviewer has no dispatch, so the plan-approval gate is irrelevant;
        # disable it so the loop never nudges toward plan approval / dispatch.
        self._require_plan_approval: bool = require_plan_approval and not _is_reviewer

        # HITL: pending messages queued from threads router.
        # Maps thread_id → list of text strings.
        self._pending_messages: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        # Lock protecting shared mutable state (tasks_file, completed_ids,
        # state.active_workers, threads.yaml) accessed from parallel worker tasks.
        # storage/atomic.py only guarantees atomic file writes; it does not
        # serialise read-modify-write cycles across coroutines.
        self._state_lock: asyncio.Lock = asyncio.Lock()

        # Per-epic concurrency gate — created lazily in start().
        self._scheduler: WorkerScheduler | None = None
        self._max_parallel_workers: int = max_parallel_workers

        # Shared state threaded through the manager loop.
        # Initialised in start() / _run_loop() before the manager agent runs.
        self._root: str = ""
        self._project_id: str = ""
        self._epic_id: str = ""
        self._run_id: str = ""
        self._epic: Epic | None = None
        self._state: RunState | None = None
        self._pub: Any = None

        # Tasks file — single source of truth for the running loop.
        self._tasks_holder: list[TasksFile] = [TasksFile(tasks=[])]

        # Per-task attempt counters (host safety upper bound).
        self._attempt_counts: dict[str, int] = {}

        # L3 MCP: McpClientManager owned by this orchestrator (created in _run_loop,
        # stopped in the run's finally block — same single-ownership rule as FSM).
        self._mcp_manager: McpClientManager | None = None
        # MCP tools cached after start — shared with Worker/Evaluator dispatch.
        self._mcp_tools: list[Any] = []
        # Server-name → tools map built alongside _mcp_tools (BE-B profile subsets).
        self._mcp_tools_by_server: dict[str, list[Any]] = {}

        # Project Memory store — created in _run_loop (written via remember()).
        self._memory_store: ProjectMemoryStore | None = None

        # A3-01: sensitive-file write event publisher — set in _run_loop.
        # Callable[(kind, name) -> None] or None.
        self._pub_sensitive: Any | None = None

    # ------------------------------------------------------------------
    # RunnerProtocol interface
    # ------------------------------------------------------------------

    async def start(
        self,
        root: str,
        project_id: str,
        epic_id: str,
        run_id: str,
    ) -> None:
        """Drive the epic to completion (or until cancelled/stopped)."""

        # Initialise the WorkerScheduler for this run.
        self._scheduler = WorkerScheduler(max_parallel_workers=self._max_parallel_workers)

        def pub(event: object) -> None:
            event_bus.publish(project_id, epic_id, event)

        # Update state.yaml → running.  ``role`` records which conversation
        # agent this run belongs to (P4) — fresh AND continuation runs both
        # pass through here, so the role survives every later save of this
        # same state object (park / stop / error).
        state = RunState(
            run_id=run_id,
            status="running",
            role="reviewer" if self._agent_role == "reviewer" else "manager",
            thread_id=self._manager_thread_id,
            started_at=datetime.now(UTC),
        )
        await state_repo.save_state(root, project_id, epic_id, state)

        # Whether the finally block below should close the SSE stream (publish
        # the ``None`` sentinel).  True for stop / error / normal return.
        # Flipped False for a not-stopped CancelledError (shelve / server
        # shutdown): the conversation is not over, so the stream stays open and
        # a continuation run's events arrive on the SAME stream — no forced
        # EventSource reconnect on every shelve (P5).
        close_sse_stream = True

        try:
            pub(RunStartedEvent(project_id=project_id, epic_id=epic_id, run_id=run_id))

            epic = await get_epic(root, project_id, epic_id)
            if epic is None:
                raise RuntimeError(f"Epic not found: {epic_id}")

            await self._run_loop(root, project_id, epic_id, run_id, epic, state, pub)

            if self._stopped:
                # Sentinel-based stop: stop() unblocks _wait_for_user_input (or a
                # turn boundary) and the loop RETURNS NORMALLY — no CancelledError
                # is raised, so the except arm below never runs.  Terminal
                # handling must match that arm's user-stop branch: waiting
                # (the user's turn — the conversation resumes on the next
                # message), thread left active, RunStoppedEvent.
                state.status = "waiting"
                state.active_workers = []
                state.last_event_at = datetime.now(UTC)
                pub(RunStoppedEvent(project_id=project_id, epic_id=epic_id, run_id=run_id))
                await state_repo.save_state(root, project_id, epic_id, state)
                return

            # The only other way _run_loop returns is turn-limit exhaustion
            # (cost brake).  The final turn already parked the run in
            # ``waiting`` — a conversation run never "completes", so there is
            # nothing to finalise here: no state write, no thread transition,
            # no completion event.  The next user message starts a
            # continuation run.

        except asyncio.CancelledError:
            # CancelledError has TWO distinct sources, distinguished by
            # ``self._stopped`` (set True only by an explicit supervisor.stop()):
            #
            # 1. self._stopped == True  → user-initiated stop.  Mark the run
            #    ``waiting`` (the user's turn; restartable).  epic.yaml is
            #    untouched (user-owned — spec §3.2).
            #
            # 2. self._stopped == False → EXTERNAL cancellation, i.e. a graceful
            #    server shutdown (uvicorn cancels the loop's tasks; the lifespan
            #    never calls stop() on runs) — or the supervisor SHELVING a
            #    waiting run (same contract).  We must NOT rewrite state.yaml
            #    here: a waiting run would otherwise be clobbered at the moment
            #    the task is cancelled.  Leaving state.yaml as last persisted
            #    (waiting, or running) lets startup recovery resume/preserve it
            #    correctly on restart.
            if self._stopped:
                state.status = "waiting"
                state.active_workers = []
                state.last_event_at = datetime.now(UTC)
                # Publish the terminal lifecycle event BEFORE the await so that a
                # second CancelledError arriving inside save_state cannot suppress
                # the event.  pub() is synchronous — it cannot be cancelled.
                # RunStoppedEvent is replayable (events/bus._LIFECYCLE_TYPES) so a
                # reconnecting client also converges to the stopped state.
                pub(RunStoppedEvent(project_id=project_id, epic_id=epic_id, run_id=run_id))
                await state_repo.save_state(root, project_id, epic_id, state)
            else:
                # Shelve / shutdown: the conversation continues later, so keep
                # subscriber streams open (no sentinel in the finally below).
                close_sse_stream = False
                logger.info(
                    "Run %s cancelled by shutdown (not a user stop); "
                    "preserving state.yaml (status=%s) for restart recovery",
                    run_id,
                    state.status,
                )
            raise

        except Exception as exc:
            # Internal/unhandled error — mark as error so the UI can surface it.
            logger.exception("EpicOrchestrator error for epic %s", epic_id)
            state.status = "error"
            state.last_event_at = datetime.now(UTC)
            await state_repo.save_state(root, project_id, epic_id, state)
            pub(
                RunFailedEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                    error=str(exc),
                )
            )
            raise

        finally:
            # L3 MCP: stop all MCP servers (always, even on error/cancel).
            #
            # Problem: supervisor.stop() may cancel the run task *twice* —
            # once via asyncio.wait_for(shield(task), ...) timeout path and
            # once via handle.task.cancel() — so a second CancelledError can
            # arrive while we are awaiting stop_async() here.  Plain
            # `except Exception` does NOT catch CancelledError, meaning _stop()
            # (MCPClient.__exit__ / background-thread join) is aborted and the
            # daemon thread leaks for the lifetime of the process.
            #
            # Fix: wrap stop_async() in asyncio.shield() so that an incoming
            # cancel does NOT propagate into the coroutine: _stop() always runs
            # to completion.  We still catch (Exception | CancelledError) so
            # logging is symmetric.  After the cleanup we re-raise any pending
            # CancelledError to honour cooperative cancellation (the *outer*
            # task is still cancelled — we just let _stop() finish first).
            #
            # asyncio.shield() raises CancelledError in the *caller* when the
            # outer task is cancelled, even though the shielded coroutine
            # continues running.  We absorb that here and re-raise after
            # _mcp_manager is cleared.
            # Return the turn slot on ANY exit (stop, error, shelve, turn
            # cap).  A parked exit already released it (no-op here); an
            # executing exit must not leak a max_parallel_epics permit.
            self._release_turn_slot()

            _mcp_cancel: asyncio.CancelledError | None = None
            if self._mcp_manager is not None:
                _mcp = self._mcp_manager
                # Clear the reference first so a second exception path cannot
                # call stop_async() again.
                self._mcp_manager = None
                try:
                    await asyncio.shield(_mcp.stop_async())
                except asyncio.CancelledError as _ce:
                    # Outer task was cancelled while stop_async was running.
                    # _stop() continues to completion inside the shield.
                    # Record to re-raise after the cleanup below.
                    _mcp_cancel = _ce
                    logger.warning(
                        "L3 MCP: outer cancel received during MCP stop for project %s"
                        " — cleanup will continue in background",
                        project_id,
                    )
                except Exception:
                    logger.warning(
                        "L3 MCP: error stopping MCP manager for project %s",
                        project_id,
                        exc_info=True,
                    )
            # Sentinel closes SSE streams — only when this exit really ends
            # the stream's usefulness (stop / error / normal return).  A
            # shelve or server shutdown skips it: state.yaml still says
            # ``waiting`` and the next continuation run publishes to the same
            # (project_id, epic_id) queues, so subscribers keep their stream.
            if close_sse_stream:
                event_bus.publish(project_id, epic_id, None)
            # Re-raise any cancel that arrived during MCP cleanup so the task
            # is correctly marked cancelled by asyncio.
            if _mcp_cancel is not None:
                raise _mcp_cancel

    def set_turn_slot(self, slot: asyncio.Semaphore) -> None:
        """Install the supervisor's max_parallel_epics semaphore.

        The orchestrator manages the slot cooperatively: acquired before the
        turn loop starts (and on every wake), released while parked in
        waiting and on exit.  The supervisor detects this method's presence
        and skips its own ``async with semaphore`` wrapper for such runners —
        exactly one party owns the bookkeeping.
        """
        self._turn_slot = slot

    async def _acquire_turn_slot(self) -> None:
        if self._turn_slot is not None and not self._turn_slot_held:
            await self._turn_slot.acquire()
            self._turn_slot_held = True

    def _release_turn_slot(self) -> None:
        if self._turn_slot is not None and self._turn_slot_held:
            self._turn_slot.release()
            self._turn_slot_held = False

    @property
    def is_parked(self) -> bool:
        """True while this conversation run is parked in ``waiting``.

        Used by the supervisor: a parked run does not hold the epic's run
        slot (``is_executing`` is False) and can be shelved — its task
        cancelled without the stop flag — to yield the slot to another
        operation while state.yaml stays ``waiting``.

        A run with pending input is NOT parked: a queued user message means
        the run is about to wake, and shelving it would cancel the task
        before the message is consumed — silently losing user speech (the
        message exists only in this in-memory queue).  Guards treat this
        about-to-wake state as executing.

        Invariant (park/wake ordering): whenever this property is True, the
        on-disk state.yaml already says ``waiting`` and no message has been
        consumed from the queue — so a shelve at any parked moment preserves
        a consistent, resumable conversation.
        """
        return self._awaiting_user and self._pending_messages.empty()

    async def pause(self) -> None:
        # While parked in waiting the run is already suspended waiting for
        # human input; pause() has no meaningful effect and must not clear
        # _paused (which would leave the run blocked at the next _checkpoint()
        # after the user answers, making disk status "running" while the event
        # loop hangs).
        if self._awaiting_user:
            return
        self._run_status = "paused"
        # Reset the announced flag so the next _checkpoint() that observes the
        # paused state emits PauseEffectiveEvent (exactly once per pause cycle).
        self._pause_effective_announced = False
        self._paused.clear()

    async def resume(self) -> None:
        self._run_status = "running"
        # Reset the flag so a subsequent pause emits PauseEffectiveEvent again.
        self._pause_effective_announced = False
        self._paused.set()

    async def stop(self) -> None:
        self._stopped = True
        # Unblock checkpoint-waiting workers so they can observe _stopped=True
        # and exit cleanly.  Without this, a stop() issued while paused would
        # leave all workers stuck in _paused.wait() forever.
        self._paused.set()
        # Unblock a parked (waiting) run by injecting a sentinel so the
        # _pending_messages.get() call returns and the loop can observe _stopped.
        if self._awaiting_user:
            self._pending_messages.put_nowait(("__stop__", ""))

    # ------------------------------------------------------------------
    # HITL injection
    # ------------------------------------------------------------------

    def inject_message(self, thread_id: str, text: str) -> None:
        """Enqueue a HITL message.  Non-blocking; called from threads router."""
        self._pending_messages.put_nowait((thread_id, text))

    def _drain_pending(self) -> list[tuple[str, str]]:
        """Drain all pending HITL messages and return them."""
        messages: list[tuple[str, str]] = []
        while True:
            try:
                messages.append(self._pending_messages.get_nowait())
            except asyncio.QueueEmpty:
                break
        return messages

    # ------------------------------------------------------------------
    # Core orchestration loop (Agent-as-a-Tool)
    # ------------------------------------------------------------------

    async def _run_loop(
        self,
        root: str,
        project_id: str,
        epic_id: str,
        run_id: str,
        epic: Epic,
        state: RunState,
        pub: Any,
    ) -> None:
        """Main orchestration loop — Manager drives via effector tools."""
        # Store shared context so effector tools can access it via self.
        self._root = root
        self._project_id = project_id
        self._epic_id = epic_id
        self._run_id = run_id
        self._epic = epic
        self._state = state
        self._pub = pub
        self._attempt_counts = {}

        # A3-01: Log SHA-256 checksums of sensitive prompt-injection surfaces at
        # run startup so an operator can detect unexpected changes to agent config
        # files and skill files between runs.
        _log_sensitive_file_checksums(root, project_id, run_id)

        # sessions_dir is the storage_dir for FileSessionManager.
        sessions_dir = str(p.sessions_dir(root, project_id, epic_id))

        # Load existing tasks or start fresh.
        tasks_file = await tasks_repo.get_tasks(root, project_id, epic_id)
        self._tasks_holder = [tasks_file]

        # FileSessionManager — orchestrator owns it exclusively (invariant §6.4).
        fsm = FileSessionManager(
            session_id=epic_id,
            storage_dir=sessions_dir,
        )

        # Build effector tools.
        task_update_tool = _make_task_update_tool(
            root,
            project_id,
            epic_id,
            run_id,
            self._tasks_holder,
        )
        dispatch_tool = self._make_dispatch_tool()
        read_branch_diff_tool = self._make_read_branch_diff_tool()

        # Register this run's root thread in threads.yaml index (UI listing only).
        # parent_thread_id=None — the manager/reviewer is the root of the agent tree (A2).
        # Use self._manager_thread_id (parameterised) instead of "manager" literal.
        # Normally the thread already exists (the manager trial is created on demand;
        # a reviewer thread is created by POST /review before the run starts), so this
        # lazy registration is a fallback.  It MUST honour self._agent_role: registering
        # a reviewer run's thread as role="manager" would mint a phantom manager trial.
        manager_thread_id = self._manager_thread_id
        _is_manager_run = self._agent_role == "manager"
        tf_threads = await threads_repo.get_threads(root, project_id, epic_id)
        if not any(t.id == manager_thread_id for t in tf_threads.threads):
            tf_threads.threads.append(
                ThreadEntry(
                    id=manager_thread_id,
                    title="Trial 1" if _is_manager_run else "Review",
                    role=self._agent_role,
                    status="active",
                    # A manager trial anchors trial_id to its own id so the worktree
                    # resolves to the pre-decoupling path; a reviewer has no trial /
                    # worktree of its own (read_branch_diff uses epic.branch).
                    trial_id=manager_thread_id if _is_manager_run else None,
                    parent_thread_id=None,
                )
            )
            await threads_repo.save_threads(root, project_id, epic_id, tf_threads)

        # Manager gets repo_search / repo_summarize if IndexerService is available.
        manager_repo_tools: list[Any] = []
        if self._indexer_service is not None:
            manager_repo_tools = make_repo_tools(
                project_id,
                self._indexer_service,
                repo_name=None,  # Manager: all repos
            )

        # Manager gets read/write docs tools (project + epic level).
        manager_docs_tools = make_manager_docs_tools(root, project_id, epic_id)

        # A3-01: Sensitive-file write event publisher.
        # Used by write_agent_config, write_skill, write_agent_profile, and
        # remember to surface writes as SSE events.
        from yukar.models.events import SensitiveFileWrittenEvent

        def _pub_sensitive(kind: str, name: str) -> None:
            from typing import cast

            _kind = cast(
                Literal["agent_config", "skill", "agent_profile", "memory"], kind
            )
            pub(
                SensitiveFileWrittenEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                    kind=_kind,
                    name=name,
                )
            )

        # Store _pub_sensitive so helper tools can publish memory-write events too.
        self._pub_sensitive = _pub_sensitive

        # F: Manager-only agent config tools (L1 write/read).
        manager_agent_config_tools = make_agent_config_tools(root, project_id, _pub_sensitive)

        # Wave 5 BE-A: Manager-only named profile tools + skill/MCP tools.
        manager_profile_tools = make_agent_profile_tools(root, project_id, _pub_sensitive)
        manager_skill_mcp_tools = make_skill_mcp_tools(
            root, project_id, self._mcp_settings, _pub_sensitive
        )

        # L3 MCP: start MCP servers and collect tools.
        # McpClientManager is owned by this orchestrator (single-owner, like FSM).
        # MCP tools are outside path_guard scope by design — see mcp_manager.py.
        from yukar.storage.mcp_repo import get_mcp_config

        mcp_cfg = get_mcp_config(root, project_id)
        if mcp_cfg.servers:
            self._mcp_manager = McpClientManager(mcp_cfg.servers, self._mcp_settings)
            try:
                await self._mcp_manager.start_async()
                self._mcp_tools = list(await self._mcp_manager.get_tools_async())
                # Build server→tools map for profile-scoped MCP subsets (BE-B).
                self._mcp_tools_by_server = await self._mcp_manager.get_tools_by_server_async()
                logger.info(
                    "L3 MCP: %d tools loaded for project %s", len(self._mcp_tools), project_id
                )
            except Exception:
                logger.warning(
                    "L3 MCP: failed to start MCP manager for project %s; "
                    "continuing without MCP tools",
                    project_id,
                    exc_info=True,
                )
                self._mcp_tools = []
                self._mcp_tools_by_server = {}
        else:
            self._mcp_manager = None
            self._mcp_tools = []
            self._mcp_tools_by_server = {}

        translator = StreamTranslator(
            project_id=project_id,
            epic_id=epic_id,
            run_id=run_id,
            thread_id=manager_thread_id,
        )

        manager_model = create_model(
            self._llm,
            role=self._agent_role,
            # Reviewer runs without an epic effort override (Manager-only setting).
            effort=(
                self._epic.manager_effort
                if (self._epic is not None and self._agent_role == "manager")
                else None
            ),
        )

        # L1: overlay per-role system prompt.
        _base_system_prompt = (
            _REVIEWER_SYSTEM_PROMPT if self._agent_role == "reviewer" else _MANAGER_SYSTEM_PROMPT
        )
        manager_system_prompt = overlay_system_prompt(
            _base_system_prompt, root, project_id, self._agent_role
        )

        # Continuation on an existing branch: the Manager starts a FRESH
        # conversation but the branch already carries prior (often completed)
        # work.  Surface the existing task snapshot + guardrails in the SYSTEM
        # prompt — never in the user bubble, which must stay one clean human
        # message (see the turn-loop single-writer invariant) — so a fresh-context
        # Manager does not re-plan from scratch and silently overwrite done tasks.
        if self._is_continuation and self._agent_role != "reviewer":
            _existing_tf = self._tasks_holder[0] if self._tasks_holder else None
            if _existing_tf is not None and _existing_tf.tasks:
                manager_system_prompt = manager_system_prompt + (
                    "\n\n## Continuation on an existing branch (READ THIS FIRST)\n"
                    "You are continuing an existing epic on its current branch in a "
                    "FRESH conversation — this is NOT a new epic.  Prior work is "
                    "already committed on this branch and these tasks already exist:\n"
                    f"{_summarise_tasks(_existing_tf)}\n"
                    "- Do NOT recreate, re-plan, or overwrite these tasks; leave "
                    "completed tasks marked done.  Only ADD new tasks (new IDs) for "
                    "genuinely new work the user requests.\n"
                    "- Before planning, inspect what is ALREADY implemented on THIS "
                    "branch with `read_branch_diff`, `fs_read`, and `repo_grep` "
                    "(`fs_read` / `repo_grep` require a `repo=<name>` argument).  Do NOT "
                    "rely on `repo_search`: its index is built from the DEFAULT branch and "
                    "will not reflect this branch's work.\n"
                )

        # L2: build AgentSkills plugin if skills exist.
        skills_plugin = build_skills_plugin(root, project_id)
        manager_plugins = [skills_plugin] if skills_plugin is not None else []

        # Project Memory (cross-Epic): MemoryManager — attached to Manager Agent only.
        # Must NOT be given to Worker / Evaluator (spec §6.4 / invariant).
        # C1: pass project_id / run_id to the embedder so embed usage is charged to this run.
        mem_store = self._memory_store = ProjectMemoryStore(
            jsonl_path=p.memory_jsonl(root, project_id),
            index_dir=p.memory_index_dir(root, project_id),
            embedder=create_embedder(
                self._embedding_settings,
                project_id=project_id,
                epic_id=epic_id,
                run_id=run_id,
            ),
            project_id=project_id,
            epic_id=epic_id,
        )
        # B3: if the index is stale or missing, trigger a lazy rebuild at startup.
        # This ensures manual edits to project.jsonl are reflected in search/injection.
        try:
            await mem_store.ensure_index_fresh()
        except Exception:
            logger.warning(
                "EpicOrchestrator: ensure_index_fresh failed for project=%s; continuing",
                project_id,
                exc_info=True,
            )
        memory_manager = MemoryManager(
            stores=[mem_store],  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            injection=MemoryInjectionConfig(trigger="userTurn", max_entries=5),
            search_tool_config=True,
            add_tool_config=False,  # add only via the remember() tool
        )

        # remember() tool: callable by Manager only. Captures store + epic_id in closure.
        remember_tool = _make_remember_tool(mem_store, epic_id, _pub_sensitive)

        if self._agent_role == "reviewer":
            # Read-only reviewer: inspect the branch diff, search the code, run the
            # tests, and report to the user in the message body.  No task/dispatch/
            # authoring tools, no memory-write (remember).  read_branch_diff
            # resolves the active trial's branch (via epic.branch) and needs no
            # worktree; the worktree-backed read-only tools (run_tests / fs_read /
            # repo_grep) are bound to the active MANAGER trial's worktree(s) so the
            # reviewer can verify first-hand (multi-repo: tools take a `repo` arg —
            # see _build_worktree_ro_tools).
            reviewer_ctx_tools = await self._build_worktree_ro_tools(
                root, project_id, epic_id, include_run_tests=True
            )
            agent_tools: list[Any] = [
                read_branch_diff_tool,
                *manager_repo_tools,
                *reviewer_ctx_tools,
            ]
        else:
            # Manager: read-only worktree tools (fs_read + repo_grep, no run_tests)
            # bound to the active trial's worktree(s) so the Manager can read the
            # branch's ACTUAL implementation across ALL touched repos (tools take a
            # `repo` arg).  read_branch_diff shows only a diff and repo_search
            # indexes the DEFAULT branch, so without these the Manager cannot read
            # the branch's real files.
            manager_ctx_tools = await self._build_worktree_ro_tools(
                root, project_id, epic_id, include_run_tests=False
            )
            agent_tools = [
                task_update_tool,
                dispatch_tool,
                read_branch_diff_tool,
                remember_tool,
                *manager_repo_tools,
                *manager_ctx_tools,
                *manager_docs_tools,
                *manager_agent_config_tools,
                *manager_profile_tools,
                *manager_skill_mcp_tools,
                *self._mcp_tools,
            ]

        manager_agent = Agent(
            model=manager_model,
            agent_id=manager_thread_id,
            session_manager=fsm,
            conversation_manager=create_conversation_manager(self._llm),
            tools=agent_tools,
            callback_handler=translator.callback,
            system_prompt=manager_system_prompt,
            memory_manager=memory_manager,
            **({"plugins": manager_plugins} if manager_plugins else {}),  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        )
        usage_recorder = AgentUsageRecorder(
            project_id=project_id,
            epic_id=epic_id,
            run_id=run_id,
            role=self._agent_role,
        ).bind(manager_agent)

        # Register FSM hook: publish UserMessageCommittedEvent when a clean
        # user (HITL or seed) message is committed by FileSessionManager.
        #
        # Design constraints:
        # - Order=SDK_LAST (100) ensures this callback fires *after* FSM's own
        #   append_message callback (order=DEFAULT=0), so message_id is already
        #   populated in fsm._latest_agent_message when we read it.
        # - Only "user" role messages without toolResult content are published.
        #   toolResult blocks appear when the model returns a tool response;
        #   we only want human-authored text.
        # - This hook is on the manager_agent only (not Workers/Evaluators),
        #   consistent with the invariant that only the Manager thread receives
        #   human-authored user messages.
        # - human_turn_flag[0] is set True only for human-authored prompts
        #   (HITL inject, a reply that woke a waiting run, seed_prompt) so that
        #   planning boilerplate turns are excluded from UserMessageCommittedEvent.
        _human_turn_flag: list[bool] = [False]
        _register_user_message_hook(
            manager_agent=manager_agent,
            fsm=fsm,
            project_id=project_id,
            epic_id=epic_id,
            run_id=run_id,
            thread_id=manager_thread_id,
            pub=pub,
            human_turn_flag=_human_turn_flag,
        )

        # Load project/epic docs context.
        project_docs = _load_project_docs(root, project_id)
        epic_docs = _load_epic_docs(root, project_id, epic_id)

        # --- Manager turn loop ---
        #
        # Shape (lifecycle redesign P3): turn-0 initialisation → run one turn →
        # park in ``waiting`` → block for the next user input → run one turn →
        # park → …  EVERY ended turn parks; the ONLY prompts the host authors
        # are turn-0 initialisation (fresh run) and the explicit-restart resume
        # prompt (continuation without a seed message).  One user input drives
        # exactly one turn, so _MAX_MANAGER_TURNS is a pure per-run cost
        # backstop, not a conversation-ending condition.
        #
        # Turn slot: acquired here for turn-0; every park releases it and
        # every wake re-acquires it (see _park_awaiting_user /
        # _wait_for_user_input), so a parked conversation never occupies one
        # of the max_parallel_epics slots.
        await self._acquire_turn_slot()
        for turn in range(_MAX_MANAGER_TURNS):
            await self._checkpoint()
            if self._stopped:
                break

            # If the previous turn parked, block until the user replies.
            # user_answer is the raw human text (empty string on stop).
            user_answer: str = ""
            if self._awaiting_user:
                user_answer = await self._wait_for_user_input(
                    root, project_id, epic_id, run_id, state, pub
                )
                if self._stopped:
                    break

            # Drain unsolicited HITL messages for manager.
            # Collect raw human texts only (no boilerplate wrapping).
            pending = self._drain_pending()
            hitl_texts: list[str] = []
            for tid, text in pending:
                if tid == manager_thread_id:
                    hitl_texts.append(text)

            # --- Build prompt (single-writer invariant) ---
            #
            # Rule: any human-authored text is passed as the *sole* content of
            # the prompt so that FileSessionManager records exactly one clean
            # user message per human turn.  Planning boilerplate (task state,
            # tool instructions) is NOT mixed into user-role messages.
            _human_authored = False  # whether this turn's prompt is human text
            if user_answer:
                # Reply that woke a parked run: pass the user's text alone.
                # Any additional unsolicited HITL texts are appended (rare but
                # possible if the user sent multiple messages while waiting).
                if hitl_texts:
                    prompt = user_answer + "\n\n" + "\n\n".join(hitl_texts)
                else:
                    prompt = user_answer
                _human_authored = True
            elif hitl_texts and turn > 0:
                # Unsolicited HITL inject(s) on turn > 0: pass the human text(s) alone.
                # The model already has full session context, so no boilerplate needed.
                prompt = "\n\n".join(hitl_texts)
                _human_authored = True
            elif turn == 0 and not self._is_continuation:
                # Fresh run turn 0: always send the full initialisation prompt so
                # the agent receives title/description/acceptance criteria/docs.
                # Any unsolicited HITL that arrived before turn 0 completed is appended
                # via hitl_prefix so it is not lost.
                # This is orchestrator-generated boilerplate — NOT human-authored.
                hitl_prefix = "\n\n".join(hitl_texts) if hitl_texts else ""
                if self._agent_role == "reviewer":
                    prompt = _build_reviewer_prompt(
                        epic, project_docs, epic_docs, self._review_context, hitl_prefix
                    )
                else:
                    prompt = _build_manager_prompt(epic, project_docs, epic_docs, hitl_prefix)
            elif turn == 0 and self._is_continuation:
                # Continuation run — FSM is now the sole writer.
                #
                # If a seed_prompt was provided (user sent a message that triggered
                # the continuation), pass it as the prompt directly so FSM records
                # it as one clean user message.  The model will restore its prior
                # session history via FSM and treat this as the next user turn.
                #
                # If seed_prompt is None (pure POST /run restart), send a resume
                # instruction so the model continues from where it left off — the
                # one instruction-bearing host prompt, backed by the user pressing
                # restart.
                if self._seed_prompt:
                    prompt = self._seed_prompt
                    _human_authored = True
                elif self._agent_role == "reviewer":
                    prompt = (
                        "Continue your review from the prior session. Gather evidence "
                        "with `read_branch_diff` / `repo_grep` / `repo_search`, then "
                        "write your findings for the user in your message and end "
                        "your turn — the run will wait for their reply."
                    )
                else:
                    prompt = (
                        "The previous run ended. Review the existing task state and "
                        "session history, then continue the work. When you have a "
                        "question or something to report, write it in your message "
                        "and end your turn — the run will wait for the user's reply."
                    )
                    # Plan-approval reminder: read the recorded approval from
                    # disk (the dispatch gate re-reads it independently at
                    # dispatch time — this is guidance only).
                    if self._require_plan_approval and not await self._is_plan_approved():
                        prompt += (
                            " Note: the current plan has no recorded user approval, so "
                            "`dispatch` will be rejected. Present the plan in your "
                            "message and wait — approval is an explicit user operation "
                            "(Approve plan) in the UI; a chat reply alone does not "
                            "approve the plan."
                        )
            else:
                # Unreachable by construction: every ended turn parks, so any
                # turn after 0 starts from a user input (user_answer or HITL).
                # Defensive: yield to the user rather than prompt the agent.
                logger.warning(
                    "Manager loop reached turn %d with no user input for epic %s; parking",
                    turn,
                    epic_id,
                )
                await self._park_awaiting_user()
                continue

            # Set the hook flag so the FSM hook publishes only human-authored turns.
            _human_turn_flag[0] = _human_authored

            # NOTE: a human-authored turn does NOT grant plan approval.
            # Approval is bound to the plan snapshot in plan_approval.yaml,
            # written only by the user's explicit Approve-plan operation.

            # Emit turn-started before streaming so the UI shows "Manager is thinking".
            pub(
                ManagerTurnStartedEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                    turn=turn,
                )
            )

            # Run one manager turn.
            try:
                async for _ in manager_agent.stream_async(prompt):
                    pass
            finally:
                await usage_recorder.flush()

            # Emit final natural-language text for live visibility (issue 2).
            # The canonical message is already persisted in fsm; this is UI-only.
            manager_final_text = _extract_agent_final_text(manager_agent)
            pub(
                ManagerMessageEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                    thread_id=manager_thread_id,
                    turn=turn,
                    text=manager_final_text,
                )
            )

            if self._stopped:
                break

            # --- Turn-end semantics: an ended turn is the agent's yield ---
            #
            # The agent stopped calling tools, so its turn is over: park the
            # run in ``waiting`` (it is the user's turn).  Questions, reports,
            # and completion summaries are already visible as the agent's
            # final message — there is nothing else to signal.  The host never
            # re-prompts; the next user message drives the next turn.
            await self._park_awaiting_user()

        else:
            # Turn limit exhausted (cost backstop, not an error): the final
            # turn already parked the run in ``waiting``, so the conversation
            # is intact and resumes as a continuation run on the next user
            # message.  Log for the operator; user notification is the
            # inbox's job (P4).
            if not self._stopped:
                logger.warning(
                    "Manager turn limit (%d) reached for epic %s; run task ends with "
                    "state left in waiting — the next user message starts a "
                    "continuation run",
                    _MAX_MANAGER_TURNS,
                    epic_id,
                )

    # ------------------------------------------------------------------
    # Effector tool factories
    # ------------------------------------------------------------------

    async def _is_plan_approved(self) -> bool:
        """Whether the CURRENT task plan snapshot has a recorded user approval.

        Reads ``plan_approval.yaml`` from disk on every call (no in-memory
        cache) so an approval recorded via REST while this run is live — or
        parked awaiting input — takes effect on the very next check.  With the
        gate disabled (``require_plan_approval=False`` / reviewer role) every
        plan counts as approved.
        """
        if not self._require_plan_approval:
            return True
        approval = await plan_approval_repo.get_plan_approval(
            self._root, self._project_id, self._epic_id
        )
        if approval is None:
            return False
        return approval.tasks_hash == compute_plan_hash(self._tasks_holder[0].tasks)

    def _make_dispatch_tool(self) -> Any:
        """Return the ``dispatch`` Strands tool bound to this orchestrator."""
        from strands import tool

        @tool
        async def dispatch(
            items: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            """Execute Worker+Evaluator for one or more tasks in parallel.

            Each item must have at least a ``task_id``.  Optional fields:
            - ``repo``: override the task's repo (falls back to task.repo then first project repo).
            - ``feedback``: previous Evaluator feedback to pass to the Worker.

            Items assigned to *different* repos run in parallel; items assigned
            to the *same* repo are serialised by the host scheduler.

            IMPORTANT: the host REJECTS this call until the user has approved the
            current task plan via the explicit Approve-plan operation in the UI.
            A chat reply does NOT approve the plan.  Present the plan in your
            message and end your turn; any change to the plan (``task_update``)
            produces a new plan snapshot that needs a fresh approval.

            Args:
                items: List of dispatch items.

            Returns:
                Per-item result list, each with:
                ``{task_id, accepted, status, feedback, worker_id, eval_id, reason?}``.
            """
            if not await self._is_plan_approved():
                # Host-enforced approval gate: the recorded approval (if any)
                # does not match the current plan snapshot.  Returned as a
                # per-item rejection so the Manager sees exactly what was blocked.
                reason = (
                    "Dispatch blocked: the current task plan has not been approved by "
                    "the user. Approval is granted only through the user's explicit "
                    "Approve-plan operation in the UI — a chat reply does NOT approve "
                    "the plan, and dispatch stays rejected until the user performs it. "
                    "Present the plan (and any changes you just made) in your message, "
                    "then end your turn and wait for the user's approval."
                )
                return [
                    {
                        "task_id": (item.get("task_id") if isinstance(item, dict) else None),
                        "accepted": False,
                        "status": "rejected",
                        "reason": reason,
                    }
                    for item in items
                ]
            return await self._run_dispatch(items)

        return dispatch

    def _make_read_branch_diff_tool(self) -> Any:
        """Return the read-only ``read_branch_diff`` tool bound to this orchestrator."""
        from strands import tool

        @tool
        async def read_branch_diff(repo: str | None = None) -> dict[str, Any]:
            """Read the branch diff (epic branch vs default branch) for final review.

            Read-only.  Use this BEFORE reporting to the user to independently
            verify what was actually implemented across the Epic — do not rely
            solely on the Evaluator verdicts.  The returned diff is the full
            change set of this trial's branch versus each repo's default branch
            (the same "branch diff" the user reviews).  If the diff does not
            satisfy the task contracts or acceptance criteria, re-``dispatch`` a
            fix — or describe the gap in your message and ask the user — instead
            of reporting the work as done.

            Args:
                repo: Inspect only this repository.  Omit to inspect every repo
                    the Epic has touched.

            Returns:
                ``{ok, repos: [{repo, branch, total_added, total_deleted,
                files: [{path, added, deleted}], diff, truncated}]}``.  A repo
                whose diff cannot be computed is reported as ``{repo, error}``.
            """
            return await self._do_read_branch_diff(repo)

        return read_branch_diff

    # ------------------------------------------------------------------
    # read_branch_diff implementation
    # ------------------------------------------------------------------

    async def _do_read_branch_diff(self, repo: str | None = None) -> dict[str, Any]:
        """Host-side implementation of the read-only ``read_branch_diff`` tool."""
        from pathlib import Path

        from yukar.git.diff import get_diff
        from yukar.storage import project_repo

        # Per-repo unified-diff cap so a large change set cannot blow the
        # Manager's context window.  File stats are always returned in full.
        max_chars = 60_000

        assert self._epic is not None

        # Resolve the active trial branch with the same precedence as dispatch:
        # the trial-specific ThreadEntry.branch, falling back to epic.branch.
        trial_branch = self._epic.branch
        tf = await threads_repo.get_threads(self._root, self._project_id, self._epic_id)
        entry = next((t for t in tf.threads if t.id == self._manager_thread_id), None)
        if entry is not None and entry.branch:
            trial_branch = entry.branch

        all_repos = await project_repo.list_repos(self._root, self._project_id)
        by_name = {r.name: r for r in all_repos}
        if repo is not None:
            if repo not in by_name:
                return {"ok": False, "reason": f"unknown repo: {repo!r}"}
            targets = [by_name[repo]]
        elif self._epic.touched_repos:
            targets = [by_name[n] for n in self._epic.touched_repos if n in by_name]
        else:
            targets = all_repos

        results: list[dict[str, Any]] = []
        for r in targets:
            try:
                d = await get_diff(
                    repo_path=Path(r.path),
                    mode="epic",
                    repo_name=r.name,
                    branch=trial_branch,
                    default_branch=r.default_branch,
                )
            except Exception as exc:
                # Branch may not exist yet (no commits) or worktree is transient.
                # Report per-repo and keep going rather than failing the tool.
                results.append({"repo": r.name, "error": str(exc)})
                continue
            diff_text = d.unified_diff or ""
            truncated = len(diff_text) > max_chars
            if truncated:
                diff_text = diff_text[:max_chars] + "\n…(diff truncated; inspect per-file)…\n"
            results.append(
                {
                    "repo": d.repo,
                    "branch": trial_branch,
                    "total_added": d.total_added,
                    "total_deleted": d.total_deleted,
                    "files": [
                        {"path": f.path, "added": f.added, "deleted": f.deleted} for f in d.files
                    ],
                    "diff": diff_text,
                    "truncated": truncated,
                }
            )

        out: dict[str, Any] = {"ok": True, "repos": results}
        if not results:
            out["note"] = "No repos to inspect yet (the Epic has touched no repositories)."
        return out

    # ------------------------------------------------------------------
    # Dispatch implementation
    # ------------------------------------------------------------------

    async def _run_dispatch(
        self,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Host-side implementation of the dispatch effector tool.

        Delegates to ``dispatch.run_dispatch`` with an explicit ``DispatchContext``
        so shared mutable state is passed without hidden ``self`` references.
        """
        assert self._scheduler is not None, "scheduler not initialised; call start() first"
        assert self._state is not None
        assert self._epic is not None

        from yukar.agents.trials import trial_id_of

        # Determine the branch for the active manager trial.
        # Prefer ThreadEntry.branch (the trial-specific unique branch set at creation time).
        # Fall back to epic.branch only when the trial has no ThreadEntry yet (backward-compat:
        # the legacy "manager" trial is registered lazily and may not appear in threads.yaml
        # until _run_loop has run at least once).
        _manager_branch = self._epic.branch  # default fallback
        # The worktree is keyed by the *trial* (branch+worktree line), not by this
        # conversation's thread id.  Default to the thread id (fresh trial) and
        # override with the ThreadEntry.trial_id when the entry is registered.
        _manager_trial_id = self._manager_thread_id
        tf_for_branch = await threads_repo.get_threads(self._root, self._project_id, self._epic_id)
        _entry_for_branch = next(
            (t for t in tf_for_branch.threads if t.id == self._manager_thread_id), None
        )
        if _entry_for_branch is not None:
            if _entry_for_branch.branch is not None:
                _manager_branch = _entry_for_branch.branch
            _manager_trial_id = trial_id_of(_entry_for_branch)

        ctx_d = DispatchContext(
            root=self._root,
            project_id=self._project_id,
            epic_id=self._epic_id,
            run_id=self._run_id,
            epic=self._epic,
            state=self._state,
            tasks_holder=self._tasks_holder,
            attempt_counts=self._attempt_counts,
            state_lock=self._state_lock,
            scheduler=self._scheduler,
            # Live callable so in-dispatch stop checks read the current flag
            # rather than the snapshot captured at DispatchContext creation time.
            # This restores the pre-refactor behaviour where dispatch checked
            # self._stopped directly (regression from round-4 refactor).
            is_stopped=lambda: self._stopped,
            run_status=self._run_status,
            pub=self._pub,
            max_attempts=_MAX_ATTEMPTS_PER_TASK,
            git_author_name=self._git_author_name,
            git_author_email=self._git_author_email,
            hooks=OrchestratorHooks(
                checkpoint=self._checkpoint,
                drain_pending=self._drain_pending,
                run_worker=self._run_worker,
                run_evaluator=self._run_evaluator,
            ),
            manager_thread_id=self._manager_thread_id,
            manager_trial_id=_manager_trial_id,
            manager_branch=_manager_branch,
        )
        return await run_dispatch(ctx_d, items)

    # ------------------------------------------------------------------
    # reviewer read-only worktree tools
    # ------------------------------------------------------------------

    async def _build_worktree_ro_tools(
        self, root: str, project_id: str, epic_id: str, *, include_run_tests: bool = True
    ) -> list[Any]:
        """Read-only inspection tools for EVERY repo registered in the project.

        Returns ``fs_read`` + ``repo_grep`` always, plus ``run_tests`` when
        *include_run_tests* is True.  Two callers:
        - Reviewer (``include_run_tests=True``): run the epic's tests and read
          files in the exact tree the Manager produced, verifying claims
          first-hand.
        - Manager (``include_run_tests=False``): read the branch's ACTUAL
          implementation directly.  ``read_branch_diff`` only shows a diff and the
          semantic ``repo_search`` index reflects the DEFAULT branch (not this
          branch's work), so without these the Manager cannot read the branch's
          real files — and would resort to dispatching a Worker just to "check"
          existing work (which the Evaluator then rejects as an empty diff).

        Every repo REGISTERED in the project is inspectable — NOT just repos a
        task has already touched.  For each repo we bind the ACTIVE MANAGER
        TRIAL's worktree when it exists (so results reflect the branch's work);
        otherwise we fall back to the repo's base checkout (the default-branch
        state the epic branch forks from).  Both are read-only views, so the
        tools always work from Turn 0 — before any worktree exists — instead of
        vanishing and leaving the agent to call a non-existent ``repo_grep``
        ("Unknown tool").  (A reviewer / continuation run is mutually exclusive
        with the manager dispatch run, so nothing else is writing those trees.)

        Multi-repo aware: builds one read-only ``AgentContext`` per repo and
        returns overview tools that take a ``repo`` argument and dispatch to the
        right tree (one tool name, no collision).  Returns ``[]`` only when the
        project has no registered repos (or none has a readable tree on disk).
        """
        from pathlib import Path

        from yukar.agents.tools.overview_tools import make_overview_ro_tools
        from yukar.agents.trials import resolve_active_trial_id
        from yukar.storage import project_repo

        assert self._epic is not None

        # Inspect EVERY repo registered in the project, not only epic.touched_repos:
        # a repo the project owns must be greppable/readable (and, for the Reviewer,
        # testable) from Turn 0 — before any task has created a worktree.  Gating on
        # touched_repos left the Manager/Reviewer with ZERO worktree tools until the
        # first dispatch, so the prompt-advertised repo_grep/fs_read/run_tests came
        # back as "Unknown tool".
        all_repos = await project_repo.list_repos(root, project_id)
        if not all_repos:
            return []

        # The active trial keys the (branch+worktree) line of work.  It is None when
        # every manager trial was archived (ghost-worktree fallback refused) — then no
        # trial worktree exists and every repo simply uses its base checkout below.
        trial_id = await resolve_active_trial_id(root, project_id, epic_id, self._epic)

        # Build a read-only AgentContext per repo.  Prefer the trial worktree (reflects
        # the branch's committed work); fall back to the repo's base checkout when no
        # worktree exists yet.  The overview tools take a `repo` argument and dispatch
        # to the right tree, so fixed tool names (fs_read / repo_grep / run_tests) do
        # not collide across repos.  Command allow/deny is the repo-level config (the
        # same security boundary the Evaluator's run_tests uses); Manager/Reviewer have
        # no profile of their own.  Each repo's context is independent, so build them
        # concurrently (AgentContext.create runs a gitignore walk).
        async def _ctx_for(repo_obj: Any) -> tuple[str, AgentContext] | None:
            repo_name = repo_obj.name
            worktree_path = (
                p.worktree_dir(root, project_id, epic_id, trial_id, repo_name)
                if trial_id is not None
                else None
            )
            if worktree_path is not None and await asyncio.to_thread(worktree_path.exists):
                tree_path = worktree_path
            else:
                # No trial worktree yet — inspect the repo's base checkout so the tool
                # still works (read-only; run_tests just executes there).
                base_path = Path(repo_obj.path)
                if not await asyncio.to_thread(base_path.exists):
                    logger.info(
                        "overview tools: neither worktree nor base checkout exists for "
                        "repo %s — skipping",
                        repo_name,
                    )
                    return None
                tree_path = base_path
            ctx = await AgentContext.create(
                project_id=project_id,
                epic_id=epic_id,
                repo_name=repo_name,
                worktree_path=tree_path,
                workspace_root=root,
                allow=list(repo_obj.commands.allow),
                deny=list(repo_obj.commands.deny),
            )
            return repo_name, ctx

        built = await asyncio.gather(*(_ctx_for(r) for r in all_repos))
        contexts: dict[str, AgentContext] = dict(b for b in built if b is not None)

        return make_overview_ro_tools(contexts, include_run_tests=include_run_tests)

    def _resolve_mcp_tools_for_profile(self, server_names: list[str]) -> list[Any]:
        """Return MCP tools filtered to the given server names.

        Called by _run_worker / _run_evaluator when a profile specifies a
        non-empty mcp_servers list.  Uses the pre-built _mcp_tools_by_server map
        (populated in _run_loop) so no additional server connections are made.

        MCP tools are intentionally outside path_guard scope — see mcp_manager.py.
        """
        tools: list[Any] = []
        for name in server_names:
            if name in self._mcp_tools_by_server:
                tools.extend(self._mcp_tools_by_server[name])
            else:
                logger.warning(
                    "profile mcp_servers: server %r not found in running MCP manager — skipping",
                    name,
                )
        return tools

    async def _run_worker(
        self,
        project_id: str,
        epic_id: str,
        run_id: str,
        worker_id: str,
        task: Task,
        ctx: AgentContext,
        feedback: str,
        hitl_prefix: str,
        resolved_profile: Any = None,
    ) -> dict[str, Any]:
        """Run a Worker agent for one task attempt.

        ``create_model`` is called here (not in worker.py) so that the test
        patch ``yukar.agents.orchestrator.create_model`` intercepts the call.

        Profile resolution (BE-B):
        - ``resolved_profile`` is pre-resolved by ``dispatch_attempt.run_one_attempt``
          with base_role=="worker" validation.  When it is ``None`` the defaults apply.
        - Orchestrator no longer performs a second ``get_profile`` call, eliminating
          the former asymmetry between commands (dispatch_attempt) and the other three
          dimensions (orchestrator).
        """
        worker_model = create_model(self._llm, role="worker")
        profile = resolved_profile

        # 1) instructions: project role overlay + profile overlay
        worker_extra_prompt = overlay_system_prompt("", self._root, project_id, "worker")
        if profile and profile.instructions:
            worker_extra_prompt = overlay_profile_instructions(
                worker_extra_prompt, profile.instructions
            )

        # 2) skills: full set unless profile specifies a non-empty subset
        if profile and profile.skills:
            worker_skills_plugin = build_skills_plugin(self._root, project_id, names=profile.skills)
        else:
            worker_skills_plugin = build_skills_plugin(self._root, project_id)
        worker_plugins = [worker_skills_plugin] if worker_skills_plugin is not None else []

        # 3) MCP: full set unless profile specifies a non-empty subset
        if profile and profile.mcp_servers:
            worker_mcp_tools = self._resolve_mcp_tools_for_profile(profile.mcp_servers)
        else:
            worker_mcp_tools = list(self._mcp_tools)

        return await run_worker(
            project_id=project_id,
            epic_id=epic_id,
            run_id=run_id,
            worker_id=worker_id,
            task=task,
            ctx=ctx,
            feedback=feedback,
            hitl_prefix=hitl_prefix,
            worker_model=worker_model,
            conversation_manager=create_conversation_manager(self._llm),
            indexer_service=self._indexer_service,
            git_author_name=self._git_author_name,
            git_author_email=self._git_author_email,
            max_turns=self._agent_settings.worker_max_turns,
            max_total_tokens=self._agent_settings.worker_max_total_tokens,
            extra_system_prompt=worker_extra_prompt,
            extra_tools=worker_mcp_tools,
            plugins=worker_plugins,
        )

    async def _run_evaluator(
        self,
        project_id: str,
        epic_id: str,
        run_id: str,
        eval_id: str,
        task: Task,
        ctx: AgentContext,
        worker_id: str,
        resolved_profile: Any = None,
    ) -> dict[str, Any]:
        """Run an Evaluator agent.

        ``create_model`` is called here (not in evaluator.py) so that the test
        patch ``yukar.agents.orchestrator.create_model`` intercepts the call.

        Profile resolution (BE-B):
        - ``resolved_profile`` is pre-resolved by ``dispatch_attempt.run_one_attempt``
          with base_role=="evaluator" validation.  When it is ``None`` the defaults apply.
        - Orchestrator no longer performs a second ``get_profile`` call, eliminating
          the former asymmetry between commands (dispatch_attempt) and the other three
          dimensions (orchestrator).

        Command allow/deny (AgentContext):
        - The ``ctx`` parameter received here is the *Evaluator-dedicated* AgentContext
          built by ``dispatch_attempt.run_one_attempt``.  Its allow/deny list comes
          solely from the repo-level allow/deny list — a profile never influences
          command scope.
        - This orchestrator method applies only the instructions / skills / MCP dimensions
          from ``resolved_profile``; it does NOT call ``AgentContext.create`` and does NOT
          modify the command configuration carried by ``ctx``.
        """
        eval_model = create_model(self._llm, role="evaluator")
        profile = resolved_profile

        # 1) instructions: project role overlay + profile overlay
        eval_extra_prompt = overlay_system_prompt("", self._root, project_id, "evaluator")
        if profile and profile.instructions:
            eval_extra_prompt = overlay_profile_instructions(
                eval_extra_prompt, profile.instructions
            )

        # 2) skills: full set unless profile specifies a non-empty subset
        if profile and profile.skills:
            eval_skills_plugin = build_skills_plugin(self._root, project_id, names=profile.skills)
        else:
            eval_skills_plugin = build_skills_plugin(self._root, project_id)
        eval_plugins = [eval_skills_plugin] if eval_skills_plugin is not None else []

        # 3) MCP: full set unless profile specifies a non-empty subset
        if profile and profile.mcp_servers:
            eval_mcp_tools = self._resolve_mcp_tools_for_profile(profile.mcp_servers)
        else:
            eval_mcp_tools = list(self._mcp_tools)

        return await run_evaluator(
            project_id=project_id,
            epic_id=epic_id,
            run_id=run_id,
            eval_id=eval_id,
            task=task,
            ctx=ctx,
            worker_id=worker_id,
            eval_model=eval_model,
            conversation_manager=create_conversation_manager(self._llm),
            epic=self._epic,
            indexer_service=self._indexer_service,
            max_turns=self._agent_settings.evaluator_max_turns,
            max_total_tokens=self._agent_settings.evaluator_max_total_tokens,
            extra_system_prompt=eval_extra_prompt,
            extra_tools=eval_mcp_tools,
            plugins=eval_plugins,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _park_awaiting_user(self) -> None:
        """Single writer for the ``waiting`` yield: the turn ended, your turn.

        Sets the in-memory gate, persists ``status=waiting`` immediately (so
        callers that check state.yaml right after the event see the correct
        status, and GET /run/state restores it after a page reload), then
        publishes ``YourTurnEvent`` (a pure "your turn" signal — the agent's
        final message is already visible in the conversation, so there is
        nothing to repeat).

        ``_wait_for_user_input`` re-persists the same state idempotently on
        the next loop iteration and then blocks until the user replies (or
        stop / shelve).

        Ordering contract (see ``is_parked``): the ``waiting`` status is
        persisted BEFORE ``_awaiting_user`` flips True, so the run can only
        be shelved once the disk already says waiting.  A cancellation that
        lands during the save below finds ``is_parked`` False (shelve
        refuses) or is a shutdown, where recovery maps running→waiting.
        The turn slot is released only after the flag flip, in the same
        event-loop step (no await between), so a freed slot always
        corresponds to a shelvable run.
        """
        if self._state is not None:
            self._state.status = "waiting"
            self._state.last_event_at = datetime.now(UTC)
            await state_repo.save_state(self._root, self._project_id, self._epic_id, self._state)

        self._awaiting_user = True
        self._release_turn_slot()

        if self._pub is not None:
            self._pub(
                YourTurnEvent(
                    project_id=self._project_id,
                    epic_id=self._epic_id,
                    run_id=self._run_id,
                    thread_id=self._manager_thread_id,
                )
            )
        logger.info("Run %s parked in waiting (turn ended — the user's turn)", self._run_id)

    async def _wait_for_user_input(
        self,
        root: str,
        project_id: str,
        epic_id: str,
        run_id: str,
        state: RunState,
        pub: Any,
    ) -> str:
        """Block until the user sends a reply via inject_message.

        Transitions state.yaml to ``waiting``, waits for the first message
        from ``_pending_messages``, then transitions back to ``running``.

        Returns the raw user reply text.  Returns an empty string if stop() was
        called before a reply arrived.

        The caller passes the returned text as the sole prompt to
        ``stream_async`` so FSM records exactly one clean user message
        (single-writer invariant — no boilerplate mixed into the user bubble).

        Stop safety:
          ``stop()`` injects a ``("__stop__", "")`` sentinel into the queue
          so this ``await`` unblocks immediately.  The caller checks
          ``self._stopped`` after return.  (A SHELVE cancels the task outright
          — CancelledError propagates from the ``get()`` below and start()'s
          not-stopped cancel arm preserves state.yaml as ``waiting``.)
        """
        # Persist waiting status (idempotent — _park_awaiting_user already
        # wrote it, but write again to be safe and to refresh last_event_at).
        state.status = "waiting"
        state.last_event_at = datetime.now(UTC)
        await state_repo.save_state(root, project_id, epic_id, state)

        logger.info("Run %s waiting for user input", run_id)

        # Block until a manager-addressed message (or stop sentinel) arrives.
        # Messages destined for other threads (workers etc.) are placed back on the
        # queue so that _drain_pending() in later turns can handle them normally.
        # This prevents a worker-thread HITL message from accidentally clearing the
        # approval gate and losing the message.
        deferred: list[tuple[str, str]] = []
        while True:
            thread_id, text = await self._pending_messages.get()

            # Stop sentinel — unblock immediately regardless of thread_id.
            if thread_id == "__stop__" or self._stopped:
                # Return deferred non-manager messages to the queue so they are
                # not permanently lost.
                for item in deferred:
                    self._pending_messages.put_nowait(item)
                logger.info("Run %s received stop signal while waiting", run_id)
                return ""

            # Manager-addressed message — this is the reply we were waiting for.
            if thread_id == self._manager_thread_id:
                # Restore deferred non-manager messages to the front of the queue
                # so they are available in subsequent _drain_pending() calls.
                for item in deferred:
                    self._pending_messages.put_nowait(item)
                break

            # Non-manager thread message — defer it and keep waiting.
            logger.debug(
                "Run %s received message for thread %r while awaiting manager input; deferring",
                run_id,
                thread_id,
            )
            deferred.append((thread_id, text))

        # The reply is consumed: leave the parked state IMMEDIATELY (same
        # event-loop step as the get() return — no await in between), so a
        # concurrent shelve can never cancel the task after the message was
        # taken off the queue (that would silently lose user speech).  Only
        # then re-acquire the turn slot and persist running: a cancellation
        # while waiting for the slot leaves disk = waiting (consistent), and
        # the queued message was never consumed-and-dropped because the flag
        # flip happens before any await.
        self._awaiting_user = False
        await self._acquire_turn_slot()

        # Restore running status — the user's reply starts the next turn.
        state.status = "running"
        state.last_event_at = datetime.now(UTC)
        await state_repo.save_state(root, project_id, epic_id, state)

        # Publish your_turn_ended so the replay buffer contains both
        # your_turn→your_turn_ended.  Late subscribers that reconnect mid-run
        # will replay both events in order and end up with a "running" final
        # state — not the stale "waiting" that YourTurnEvent alone would leave.
        if self._pub is not None:
            self._pub(
                YourTurnEndedEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                    thread_id=self._manager_thread_id,
                )
            )

        logger.info("Run %s resuming from waiting; user_reply=%r", run_id, text)
        # Return raw human text only — caller passes it as the sole prompt to
        # stream_async, so FSM records one clean user message (no boilerplate).
        return text

    async def _checkpoint(self) -> None:
        """Pause-check point.  Awaits until resumed.  Yields to event loop.

        Emits exactly one ``PauseEffectiveEvent`` per pause cycle.  Because
        the manager loop and each worker task all call ``_checkpoint()``, without
        the ``_pause_effective_announced`` guard a single ``pause()`` call would
        produce N+1 events.  The flag is reset by ``pause()`` and ``resume()``
        so the next pause cycle is fresh.
        """
        await asyncio.sleep(0)  # yield to event loop
        # We are about to actually block — emit PauseEffectiveEvent exactly
        # once to distinguish "pause API call received" (RunPausedEvent)
        # from "run actually stopped at a checkpoint" (PauseEffectiveEvent).
        # _state_lock is not needed here: _pause_effective_announced is only
        # written under the pause/resume path (single-caller, no contention)
        # and workers=1 means no true concurrency.
        if (
            not self._paused.is_set()
            and self._pub is not None
            and not self._pause_effective_announced
        ):
            self._pause_effective_announced = True
            self._pub(
                PauseEffectiveEvent(
                    project_id=self._project_id,
                    epic_id=self._epic_id,
                    run_id=self._run_id,
                )
            )
        await self._paused.wait()


# ---------------------------------------------------------------------------
# remember() tool (module-level factory)
# ---------------------------------------------------------------------------


def _make_remember_tool(
    store: ProjectMemoryStore,
    epic_id: str,
    pub_event: Any | None = None,
) -> Any:
    """Generate the Manager-only remember() tool.

    Captures store, epic_id, and an optional pub_event callback in a closure
    and writes explicit knowledge to project memory.
    Must not be passed to Worker / Evaluator (invariant).

    Args:
        store: The ProjectMemoryStore to write to.
        epic_id: Current epic ID (stored in memory metadata).
        pub_event: Optional ``(kind, name) -> None`` callable to publish a
            ``SensitiveFileWrittenEvent`` after a successful write.  Receives
            ``kind="memory"`` and the category string.
    """
    from strands import tool

    @tool
    async def remember(
        fact: str,
        category: Literal["convention", "fact", "lesson"] = "fact",
        repo: str | None = None,
    ) -> dict[str, Any]:
        """Persist cross-project knowledge to memory.

        Records conventions, facts, and lessons to be reused across multiple Epics.

        Args:
            fact: The text to record.
            category: One of "convention" (coding convention) / "fact" (project fact)
                / "lesson" (lesson from a past Epic).
            repo: Target repository name (optional).

        Returns:
            ``{stored: True, id: <record_id>}`` or ``{stored: False, reason: "duplicate"}``.
        """
        try:
            result = await store.add(
                fact,
                metadata={
                    "category": category,
                    "repo": repo or "-",
                    "epic_id": epic_id,
                    "source": "remember",
                },
            )
        except EmbedFailedError:
            return {"stored": False, "reason": "embed_failed"}
        if result is None:
            return {"stored": False, "reason": "duplicate"}
        if pub_event is not None:
            pub_event("memory", category)
        return {"stored": True, "id": result}

    return remember


# ---------------------------------------------------------------------------
# Startup integrity visibility (A3-01)
# ---------------------------------------------------------------------------


def _log_sensitive_file_checksums(root: str, project_id: str, run_id: str) -> None:
    """Log SHA-256 checksums of sensitive agent-prompt files at run startup.

    Covers `.yukar/agents/*.md` (per-role custom instructions written by the
    Manager via ``write_agent_config``) and all SKILL.md files (written by
    ``write_skill``).  Operators can compare these log lines across runs to
    detect unexpected modifications introduced by a prompt-injected Manager.

    Failures (file not found, permission error) are silently skipped so a
    missing config file does not block the run.  The function is best-effort
    and synchronous — checksums are computed with stdlib hashlib.
    """
    import hashlib
    from pathlib import Path as _Path

    def _sha256(path: _Path) -> str:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return "<unreadable>"

    # Per-role custom instruction files: .yukar/agents/{role}.md
    agents_dir = p.project_agents_dir(root, project_id)
    if agents_dir.is_dir():
        for md_file in sorted(agents_dir.glob("*.md")):
            logger.info(
                "run_startup integrity: %s sha256=%s run_id=%s",
                md_file.relative_to(agents_dir.parent.parent),
                _sha256(md_file),
                run_id,
            )

    # SKILL.md files: skills/{name}/SKILL.md (project-level skills directory)
    skills_parent = p.project_skills_dir(root, project_id)
    if skills_parent.is_dir():
        for skill_md in sorted(skills_parent.rglob("SKILL.md")):
            logger.info(
                "run_startup integrity: %s sha256=%s run_id=%s",
                skill_md.relative_to(skills_parent.parent),
                _sha256(skill_md),
                run_id,
            )
