"""EpicOrchestrator — Agent-as-a-Tool implementation of RunnerProtocol.

Architecture
------------
One orchestrator per epic run.  It:

1. Owns the single ``FileSessionManager`` (invariant §6.4).
2. Builds a persistent Manager Agent that drives the Epic end-to-end.
3. Exposes three effector tools to the Manager:
   - ``task_update``   — create/update tasks.yaml
   - ``dispatch``      — execute one or more Worker+Evaluator attempts in parallel
   - ``complete_epic`` — signal that all work is done (host validates)
4. Delegates token/tool events to ``StreamTranslator`` (bus).
5. Respects pause/resume (asyncio.Event) and stop (asyncio.CancelledError).

Manager loop (spec §6.2 Agent-as-a-Tool):
    Manager calls task_update() to build the plan.
    Manager calls dispatch([{task_id, repo?, feedback?}, ...]) to execute tasks.
    Host runs Worker+Evaluator per item, enforces sandbox/lease/budget/stop.
    Manager reads per-item verdict: accepted/rejected/blocked.
    Manager retries (with feedback), replans, or gives up per task.
    Manager calls complete_epic() when satisfied.

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
    publish_task_update as _publish_task_update,
)
from yukar.agents.dispatch import (
    register_agent_thread as _register_agent_thread,
)
from yukar.agents.evaluator import run_evaluator
from yukar.agents.mcp_manager import McpClientManager
from yukar.agents.orchestrator_tools import (
    _make_task_update_tool,
    _ManagerTurnLimitError,
)
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
    RunCompletedEvent,
    RunFailedEvent,
    RunStartedEvent,
    RunStoppedEvent,
    UserInputRequestedEvent,
    UserInputResolvedEvent,
    UserMessageCommittedEvent,
)
from yukar.models.roles import AgentRole
from yukar.models.run import RunState
from yukar.models.task import Task, TaskProgress, TasksFile
from yukar.models.thread import ThreadEntry
from yukar.runs.scheduler import WorkerScheduler
from yukar.storage import state_repo, tasks_repo, threads_repo
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

# Maximum manager turns in the agent loop before force-exiting.
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
      when the prompt is human-authored (HITL inject, ask_user reply, or
      seed_prompt from user), and ``False`` for orchestrator-generated
      planning prompts (_build_manager_prompt, task-state summaries, resume
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

    The Manager Agent drives the Epic via three effector tools:
    - ``task_update``: plan/replan tasks.yaml
    - ``dispatch``: execute Worker+Evaluator for one or more tasks in parallel
    - ``complete_epic``: signal completion (host validates)

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

        # Canonical run status as seen by this orchestrator.  Supervisor
        # calls pause()/resume() to keep this in sync so that worker
        # save_state calls do not overwrite the disk status written by the
        # supervisor (the "pause flicker" bug).
        # Values: "running" | "paused" | "awaiting_input"
        # (terminal statuses are never set here).
        self._run_status: Literal["running", "paused", "awaiting_input"] = "running"

        # HITL approval gate: set True when ask_user tool is called.
        # The loop blocks on _pending_messages.get() while this is True.
        self._awaiting_user: bool = False
        # The question/plan text the Manager wants to present to the user.
        self._pending_question: str = ""

        # Plan-approval gate (prevents the Manager from dispatching Workers
        # before the user has approved the current task plan).
        #   - dispatch is REJECTED by the host while the plan is not approved.
        #   - task_update (a plan change) invalidates approval → must re-ask.
        #   - a genuine user turn (ask_user reply / HITL / seed_prompt) grants it.
        # A continuation run resumes an already-approved plan; a fresh run must
        # earn approval via ask_user before its first dispatch.
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
        self._plan_approved: bool = is_continuation or _is_reviewer

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

        # Set by the complete_epic tool to signal the manager loop to exit.
        self._epic_complete: bool = False

        # L3 MCP: McpClientManager owned by this orchestrator (created in _run_loop,
        # stopped in the run's finally block — same single-ownership rule as FSM).
        self._mcp_manager: McpClientManager | None = None
        # MCP tools cached after start — shared with Worker/Evaluator dispatch.
        self._mcp_tools: list[Any] = []
        # Server-name → tools map built alongside _mcp_tools (BE-B profile subsets).
        self._mcp_tools_by_server: dict[str, list[Any]] = {}

        # Project Memory store — created in _run_loop, referenced by _do_complete_epic.
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

        # Update state.yaml → running.
        state = RunState(
            run_id=run_id,
            status="running",
            manager_thread=self._manager_thread_id,
            started_at=datetime.now(UTC),
        )
        await state_repo.save_state(root, project_id, epic_id, state)

        try:
            pub(RunStartedEvent(project_id=project_id, epic_id=epic_id, run_id=run_id))

            epic = await get_epic(root, project_id, epic_id)
            if epic is None:
                raise RuntimeError(f"Epic not found: {epic_id}")

            await self._run_loop(root, project_id, epic_id, run_id, epic, state, pub)

            # Mark state.yaml completed.
            state.status = "completed"
            state.active_workers = []
            state.pending_question = None
            state.last_event_at = datetime.now(UTC)
            await state_repo.save_state(root, project_id, epic_id, state)

            # Mark manager thread resolved now that the full run succeeded
            # (spec §4.2 vocabulary: active | resolved | failed).
            # stop/error paths leave threads active intentionally — the run was
            # interrupted mid-flight and could be restarted.
            await threads_repo.update_thread_status(
                root, project_id, epic_id, self._manager_thread_id, "resolved"
            )

            pub(RunCompletedEvent(project_id=project_id, epic_id=epic_id, run_id=run_id))

        except asyncio.CancelledError:
            # CancelledError has TWO distinct sources, distinguished by
            # ``self._stopped`` (set True only by an explicit supervisor.stop()):
            #
            # 1. self._stopped == True  → user-initiated stop.  Mark the run
            #    ``idle`` (restartable) and clear pending_question.  epic.yaml
            #    stays ``in_progress`` (supervisor owns that field — spec §3.2).
            #
            # 2. self._stopped == False → EXTERNAL cancellation, i.e. a graceful
            #    server shutdown (uvicorn cancels the loop's tasks; the lifespan
            #    never calls stop() on runs).  We must NOT rewrite state.yaml
            #    here: an awaiting_input run would otherwise be clobbered to
            #    idle + pending_question=None at the moment the server stops,
            #    losing the parked HITL question.  Leaving state.yaml as last
            #    persisted (awaiting_input + pending_question, or running) lets
            #    startup recovery resume/preserve it correctly on restart.
            if self._stopped:
                state.status = "idle"
                state.active_workers = []
                state.pending_question = None
                state.last_event_at = datetime.now(UTC)
                # Publish the terminal lifecycle event BEFORE the await so that a
                # second CancelledError arriving inside save_state cannot suppress
                # the event.  pub() is synchronous — it cannot be cancelled.
                # RunStoppedEvent is replayable (events/bus._LIFECYCLE_TYPES) so a
                # reconnecting client also converges to the stopped state.
                pub(RunStoppedEvent(project_id=project_id, epic_id=epic_id, run_id=run_id))
                await state_repo.save_state(root, project_id, epic_id, state)
            else:
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
            state.pending_question = None
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
                    # Record to re-raise after the sentinel publish below.
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
            # Sentinel closes SSE streams.
            event_bus.publish(project_id, epic_id, None)
            # Re-raise any cancel that arrived during MCP cleanup so the task
            # is correctly marked cancelled by asyncio.
            if _mcp_cancel is not None:
                raise _mcp_cancel

    async def pause(self) -> None:
        # During awaiting_input the run is already suspended waiting for human
        # input; pause() has no meaningful effect and must not clear _paused
        # (which would leave the run blocked at the next _checkpoint() after the
        # user answers, making disk status "running" while the event loop hangs).
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
        # Unblock the awaiting_input wait by injecting a sentinel so the
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
        self._epic_complete = False
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

        # Refine the plan-approval gate for a continuation now that tasks are
        # known.  A continuation optimistically starts approved so an in-flight
        # epic can resume dispatching without re-asking.  But if NO work has
        # started yet (every task still todo/blocked — e.g. the Manager planned
        # and asked the user, then the run was stopped before approval), the
        # plan was never actually approved: require a fresh approval so a later
        # unrelated resume message cannot auto-dispatch an unapproved plan.
        if self._require_plan_approval and self._is_continuation:
            work_started = any(
                t.status in ("in_progress", "done") for t in tasks_file.tasks
            )
            self._plan_approved = work_started

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
            on_change=self._invalidate_plan_approval,
        )
        dispatch_tool = self._make_dispatch_tool()
        complete_epic_tool = self._make_complete_epic_tool()
        read_branch_diff_tool = self._make_read_branch_diff_tool()
        ask_user_tool = self._make_ask_user_tool()

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
        # Used by write_agent_config, write_skill, write_agent_profile, remember,
        # and complete_epic learnings to surface writes as SSE events.
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

        # Store _pub_sensitive so _do_complete_epic can publish memory-write events too.
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
            # Read-only reviewer: inspect the branch diff, search the code, and
            # report to the user.  No task/dispatch/complete/authoring tools, no
            # memory-write (remember).  read_branch_diff already resolves the
            # active trial's branch (via epic.branch), so it needs no worktree.
            agent_tools: list[Any] = [
                read_branch_diff_tool,
                ask_user_tool,
                *manager_repo_tools,
            ]
        else:
            agent_tools = [
                task_update_tool,
                dispatch_tool,
                complete_epic_tool,
                read_branch_diff_tool,
                ask_user_tool,
                remember_tool,
                *manager_repo_tools,
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
        #   (HITL inject, ask_user reply, seed_prompt from user) so that
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

        # --- Manager turn loop (cap _MAX_MANAGER_TURNS) ---
        _turn_limit_reached = True  # flipped to False on any clean exit
        for turn in range(_MAX_MANAGER_TURNS):
            await self._checkpoint()
            if self._stopped:
                _turn_limit_reached = False
                break

            # If the previous turn called ask_user, block until the user replies.
            # user_answer is the raw human text (empty string on stop).
            user_answer: str = ""
            if self._awaiting_user:
                user_answer = await self._wait_for_user_input(
                    root, project_id, epic_id, run_id, state, pub
                )
                if self._stopped:
                    _turn_limit_reached = False
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
            #
            # When there is a user message (ask_user answer or unsolicited HITL):
            #   prompt = human text only → FSM writes one clean user message.
            #   The model has full session context from prior turns; it does not
            #   need the task-state boilerplate repeated in the user bubble.
            #
            # When there is no user message:
            #   prompt = the standard planning boilerplate as before.
            #   FSM records it as a user message (the "host instruction" pattern).
            tf = self._tasks_holder[0]

            _human_authored = False  # whether this turn's prompt is human text
            if user_answer:
                # Solicited reply (ask_user response): pass the user's text alone.
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
                # If seed_prompt is None (pure POST /run restart), send a generic
                # resume instruction so the model continues from where it left off.
                if self._seed_prompt:
                    prompt = self._seed_prompt
                    _human_authored = True
                elif self._agent_role == "reviewer":
                    prompt = (
                        "Continue your review from the prior session. Use "
                        "`read_branch_diff` and `repo_search` to gather evidence, then "
                        "call `ask_user` to report your findings to the user."
                    )
                elif self._require_plan_approval and not self._plan_approved:
                    # Resuming a plan that was never approved (planned, then the
                    # run ended before the user approved — no work has started).
                    # Do NOT tell the Manager to dispatch: the gate would reject it.
                    prompt = (
                        "The previous run ended before the plan was approved. "
                        "Review the existing task state and session history, then call "
                        "`ask_user` to present the plan and wait for the user's approval "
                        "before dispatching."
                    )
                else:
                    prompt = (
                        "The previous run ended. Review the existing task state and "
                        "session history, then continue: dispatch any remaining runnable "
                        "tasks, replan if needed, or call `complete_epic` if all work "
                        "is done. Call `ask_user` if you need human input."
                    )
            elif self._require_plan_approval and not self._plan_approved:
                # Plan not yet approved by the user — do NOT nudge toward
                # dispatch (it would be rejected by the host gate).  Instead
                # steer the Manager to present the plan and wait for approval.
                # This replaces the old auto-nudge that read like a synthetic
                # "proceed with the work" user message.
                task_summary = _summarise_tasks(tf)
                prompt = (
                    f"Current task state:\n{task_summary}\n\n"
                    "The user has NOT approved this plan yet, so `dispatch` will be "
                    "rejected. Call `ask_user` to present the current plan (and any "
                    "changes you just made) and wait for the user's approval. "
                    "Do NOT call `dispatch` until the user approves."
                )
            elif self._agent_role == "reviewer":
                prompt = (
                    "Continue your review. Gather any remaining evidence with "
                    "`read_branch_diff` / `repo_search`, then call `ask_user` to "
                    "report your verdict and findings to the user."
                )
            else:
                task_summary = _summarise_tasks(tf)
                prompt = (
                    f"Current task state:\n{task_summary}\n\n"
                    "Select runnable tasks and call `dispatch` "
                    "(pass multiple items for parallelism). "
                    "Review each item's verdict: on rejection, retry with feedback or replan. "
                    "When all work is done or tasks are blocked, call `complete_epic`."
                )

            # Set the hook flag so the FSM hook publishes only human-authored turns.
            _human_turn_flag[0] = _human_authored

            # A genuine user turn (ask_user reply, unsolicited HITL, or a
            # continuation seed_prompt) grants approval of the current plan.
            # If the Manager then changes the plan via task_update this turn,
            # the on_change hook re-invalidates it before dispatch is reached.
            if _human_authored:
                self._plan_approved = True

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

            # Check exit conditions.
            if self._epic_complete or self._stopped:
                _turn_limit_reached = False
                break

            # Deadlock guard: after the first turn, if there is no runnable
            # task and nothing in-flight, the Manager cannot make progress.
            # Exit cleanly (not a turn-limit error) so state stays completed.
            # We check turn>0 to give the Manager at least one chance to create
            # tasks via task_update before bailing out.
            # Skip this guard while awaiting user input — the Manager intentionally
            # suspended itself and will dispatch once the user replies.
            if turn > 0 and not self._awaiting_user:
                tf = self._tasks_holder[0]
                completed_ids = {t.id for t in tf.tasks if t.status == "done"}
                runnable_exists = any(
                    t.status not in ("done", "blocked", "in_progress")
                    and all(dep in completed_ids for dep in t.depends_on)
                    for t in tf.tasks
                )
                in_flight_exists = any(t.status == "in_progress" for t in tf.tasks)
                if not runnable_exists and not in_flight_exists:
                    # Nothing left to do — force exit (not a turn-limit error).
                    _turn_limit_reached = False
                    break

        # Post-loop task bookkeeping is Manager-only.  A reviewer run has no
        # task/dispatch machinery — it must never mark tasks blocked or (below)
        # fabricate a default task, which would overwrite the Manager trial's
        # tasks.yaml with a phantom task for the epic under review.
        if self._agent_role == "manager":
            # Mark any remaining non-done/blocked tasks as blocked.
            tf = self._tasks_holder[0]
            async with self._state_lock:
                pending_tasks = [t for t in tf.tasks if t.status not in ("done", "blocked")]
                if pending_tasks:
                    for stuck in pending_tasks:
                        logger.warning(
                            "Task %s not completed after manager loop; marking blocked", stuck.id
                        )
                        stuck.status = "blocked"
                        _publish_task_update(
                            pub, project_id, epic_id, run_id, stuck.id, "blocked", stuck.title
                        )
                    done_count = sum(1 for t in tf.tasks if t.status == "done")
                    tf.progress = TaskProgress(done=done_count, total=len(tf.tasks))
                    await tasks_repo.save_tasks(root, project_id, epic_id, tf)

            # Fallback: if manager produced no tasks at all, create default.
            if not self._tasks_holder[0].tasks:
                logger.warning("Manager produced no tasks; creating single default task")
                default_task = Task(
                    id="T1",
                    title=epic.title,
                    status="blocked",
                    repo=None,
                )
                default_tf = TasksFile(
                    tasks=[default_task],
                    progress=TaskProgress(done=0, total=1),
                )
                await tasks_repo.save_tasks(root, project_id, epic_id, default_tf)

        # Spec §6.2: if the loop was exhausted (not stopped, not complete_epic)
        # signal the caller to set run state to "error".
        if _turn_limit_reached:
            logger.error(
                "Manager turn limit (%d) reached for epic %s without complete_epic; "
                "marking run as error",
                _MAX_MANAGER_TURNS,
                epic_id,
            )
            raise _ManagerTurnLimitError(
                f"Manager turn limit ({_MAX_MANAGER_TURNS}) reached without complete_epic"
            )

    # ------------------------------------------------------------------
    # Effector tool factories
    # ------------------------------------------------------------------

    def _make_ask_user_tool(self) -> Any:
        """Return the ``ask_user`` Strands tool bound to this orchestrator.

        Manager-only tool.  Worker and Evaluator do NOT receive this tool.
        When called, it sets ``_awaiting_user=True`` and stores the question
        so the loop knows to block on ``_pending_messages.get()`` next turn.
        """
        from strands import tool

        @tool
        async def ask_user(question: str) -> str:
            """Present a question or plan summary to the user and wait for their reply.

            Use this at the end of Turn 0 to share your task breakdown and any
            clarifying questions before dispatching Workers.  Also use whenever
            you need human input or approval during the run.

            IMPORTANT: After calling this tool, do NOT call ``dispatch``.
            Stop your current turn.  The run will pause until the user replies,
            then you will receive their answer in the next turn's prompt.

            Args:
                question: The question or plan summary to present to the user.
                          Include your task breakdown and any unclear points
                          that require human decision or confirmation.

            Returns:
                Confirmation that the question has been sent and the run is
                waiting for the user's reply.
            """
            self._awaiting_user = True
            self._pending_question = question

            # Persist awaiting_input status immediately so callers that check
            # state.yaml right after receiving UserInputRequestedEvent see the
            # correct status (rather than waiting until the next loop turn).
            # Also persist the question text so GET /run/state can restore it
            # after a page reload without relying on the SSE replay buffer.
            if self._state is not None:
                self._state.status = "awaiting_input"
                self._state.pending_question = question
                self._state.last_event_at = datetime.now(UTC)
                await state_repo.save_state(
                    self._root, self._project_id, self._epic_id, self._state
                )

            # Publish the event so the UI can show "awaiting input" immediately.
            if self._pub is not None:
                self._pub(
                    UserInputRequestedEvent(
                        project_id=self._project_id,
                        epic_id=self._epic_id,
                        run_id=self._run_id,
                        thread_id=self._manager_thread_id,
                        question=question,
                    )
                )
            return (
                "Your question/plan has been sent to the user. "
                "The run is now waiting for their approval or answer. "
                "Do NOT call `dispatch` until you receive the user's reply in your next turn."
            )

        return ask_user

    def _invalidate_plan_approval(self) -> None:
        """Mark the current task plan as no longer approved.

        Called after any ``task_update`` so that a plan change forces the
        Manager to re-present the plan via ``ask_user`` before it may dispatch.
        A no-op when the approval gate is disabled.
        """
        if self._require_plan_approval:
            self._plan_approved = False

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
            current task plan.  Present the plan via ``ask_user`` and wait for the
            user's reply first; any change to the plan (``task_update``) requires
            a fresh approval before dispatching.

            Args:
                items: List of dispatch items.

            Returns:
                Per-item result list, each with:
                ``{task_id, accepted, status, feedback, worker_id, eval_id, reason?}``.
            """
            if self._require_plan_approval and not self._plan_approved:
                # Host-enforced approval gate: refuse dispatch and tell the
                # Manager to get the user's approval first.  Returned as a
                # per-item rejection so the Manager sees exactly what was blocked.
                reason = (
                    "Dispatch blocked: the current task plan has not been approved by "
                    "the user. Call `ask_user` to present the plan (and any changes you "
                    "just made) and wait for the user's reply before dispatching."
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

    def _make_complete_epic_tool(self) -> Any:
        """Return the ``complete_epic`` Strands tool bound to this orchestrator."""
        from strands import tool

        @tool
        async def complete_epic(
            learnings: list[str] | None = None,
        ) -> dict[str, Any]:
            """Signal that all Epic work is done.

            The host validates: if runnable tasks remain, returns
            ``{ok: false, reason: "runnable tasks remain: [...]"}``.
            Otherwise sets the internal complete flag and returns ``{ok: true}``.

            Args:
                learnings: Optional list of lesson strings to persist in project memory
                    for future Epics. Each string is stored as a "lesson" entry.

            Returns:
                ``{ok: bool, reason?: str}``
            """
            return await self._do_complete_epic(learnings=learnings)

        return complete_epic

    def _make_read_branch_diff_tool(self) -> Any:
        """Return the read-only ``read_branch_diff`` tool bound to this orchestrator."""
        from strands import tool

        @tool
        async def read_branch_diff(repo: str | None = None) -> dict[str, Any]:
            """Read the branch diff (epic branch vs default branch) for final review.

            Read-only.  Use this BEFORE calling ``complete_epic`` to independently
            verify what was actually implemented across the Epic — do not rely
            solely on the Evaluator verdicts.  The returned diff is the full
            change set of this trial's branch versus each repo's default branch
            (the same "branch diff" the user reviews).  If the diff does not
            satisfy the task contracts or acceptance criteria, re-``dispatch`` a
            fix or escalate via ``ask_user`` instead of completing.

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
    # complete_epic implementation
    # ------------------------------------------------------------------

    async def _do_complete_epic(self, learnings: list[str] | None = None) -> dict[str, Any]:
        """Host-side implementation of complete_epic tool."""
        tf = self._tasks_holder[0]
        completed_ids = {t.id for t in tf.tasks if t.status == "done"}

        runnable = [
            t
            for t in tf.tasks
            if t.status not in ("done", "blocked")
            and all(dep in completed_ids for dep in t.depends_on)
        ]
        if runnable:
            ids = [t.id for t in runnable]
            return {"ok": False, "reason": f"runnable tasks remain: {ids}"}

        self._epic_complete = True

        # Persist learnings to the store (done after completion is confirmed).
        # Tally per-learning success/failure using the same outcome categories as remember()
        # so partial/total failures are visible to Manager / humans.
        # Note: persistence failure does not block completion correctness (best-effort).
        learnings_stored = 0
        learnings_duplicate = 0
        learnings_failed: list[dict[str, str]] = []
        if learnings:
            mem_store = self._memory_store
            if mem_store is not None:
                for learning in learnings:
                    try:
                        record_id = await mem_store.add(
                            learning,
                            metadata={
                                "category": "lesson",
                                "epic_id": self._epic_id,
                                "source": "complete_epic",
                            },
                        )
                    except EmbedFailedError as exc:
                        logger.warning("complete_epic: failed to embed learning", exc_info=True)
                        learnings_failed.append({"reason": "embed_failed", "error": str(exc)})
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("complete_epic: failed to persist learning", exc_info=True)
                        learnings_failed.append({"reason": "error", "error": str(exc)})
                    else:
                        # add() returns None on duplicate, a record id on success.
                        if record_id is None:
                            learnings_duplicate += 1
                        else:
                            learnings_stored += 1
                            _pub_s = getattr(self, "_pub_sensitive", None)
                            if _pub_s is not None:
                                _pub_s("memory", "lesson")
            else:
                # No store available — every learning is lost; surface it.
                for _ in learnings:
                    learnings_failed.append({"reason": "no_memory_store", "error": ""})

        return {
            "ok": True,
            "learnings_stored": learnings_stored,
            "learnings_duplicate": learnings_duplicate,
            "learnings_failed": learnings_failed,
        }

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
          built by ``dispatch_attempt.run_one_attempt`` after resolving the evaluator
          profile.  Its allow/deny list is derived via ``_merge_commands(
          resolved_eval_profile, repo_allow, repo_deny)`` — independent of the Worker ctx.
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

        Transitions state.yaml to ``awaiting_input``, waits for the first
        message from ``_pending_messages``, then transitions back to ``running``.

        Returns the raw user reply text.  Returns an empty string if stop() was
        called before a reply arrived.

        The caller passes the returned text as the sole prompt to
        ``stream_async`` so FSM records exactly one clean user message
        (single-writer invariant — no boilerplate mixed into the user bubble).

        Stop safety:
          ``stop()`` injects a ``("__stop__", "")`` sentinel into the queue
          so this ``await`` unblocks immediately.  The caller checks
          ``self._stopped`` after return.
        """
        # Persist awaiting_input status (idempotent — the ask_user tool already
        # wrote status + pending_question, but write again to be safe and to
        # refresh last_event_at). Re-persisting pending_question keeps state.yaml
        # authoritative so the question survives a page reload via GET /run/state
        # (eviction-proof, independent of the SSE replay buffer).
        state.status = "awaiting_input"
        state.pending_question = self._pending_question or None
        state.last_event_at = datetime.now(UTC)
        await state_repo.save_state(root, project_id, epic_id, state)

        logger.info("Run %s entering awaiting_input; question=%r", run_id, self._pending_question)

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
                logger.info("Run %s received stop signal while awaiting_input", run_id)
                return ""

            # Manager-addressed message — this is the approval/answer we need.
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

        # Restore running status and clear the pending question so stale text
        # does not survive a page reload after the user has already replied.
        state.status = "running"
        state.pending_question = None
        state.last_event_at = datetime.now(UTC)
        await state_repo.save_state(root, project_id, epic_id, state)
        self._awaiting_user = False
        self._pending_question = ""

        # Publish the resolved event so the replay buffer contains both
        # request→resolved.  Late subscribers that reconnect mid-run will replay
        # both events in order and end up with a "running" final state — not
        # the stale "awaiting_input" that UserInputRequestedEvent alone would leave.
        if self._pub is not None:
            self._pub(
                UserInputResolvedEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                    thread_id=self._manager_thread_id,
                )
            )

        logger.info("Run %s resuming from awaiting_input; user_reply=%r", run_id, text)
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
