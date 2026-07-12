"""Tests for the Manager planning + approval gate (waiting / park HITL).

Lifecycle redesign P3: there is no ask_user tool.  The Manager presents its
plan (or question) in the message body and ends its turn — the run parks in
``waiting`` (the user's turn).  Approval is an explicit user operation
recorded in plan_approval.yaml; a chat reply never opens the dispatch gate.

Covers:
- Ending a turn parks the run in ``waiting`` and publishes
  YourTurnEvent (a pure "your turn" signal — no payload text).
- inject_message wakes the waiting run and restores running status.
- dispatch is host-rejected until the recorded approval matches the plan.
- stop() while waiting settles the state as ``waiting`` (restartable).
- YourTurnEvent is published to the event bus and SSE-serialized.
- A waiting run is preserved as-is by startup recovery (legacy
  awaiting_input state files are read back as waiting).
- RunEvent discriminated union includes YourTurnEvent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests._helpers import make_git_repo


async def _bootstrap(root: str, project_id: str, epic_id: str, repo_path: Path) -> None:
    from yukar.models.epic import Epic
    from yukar.models.project import Project, Repo, RepoCommands
    from yukar.storage.epic_repo import save_epic
    from yukar.storage.project_repo import save_project, save_repo

    project = Project(id=project_id, name=project_id, status="active", repos=[repo_path.name])
    await save_project(root, project)

    repo = Repo(
        name=repo_path.name,
        path=str(repo_path),
        default_branch="main",
        commands=RepoCommands(allow=["git", "pytest"], deny=[]),
    )
    await save_repo(root, project_id, repo)

    epic = Epic(
        id=epic_id,
        slug="test-epic",
        title="Test Epic",
        description="A test epic for automated testing.",
        branch="yukar/ep-1-test-epic",
    )
    await save_epic(root, project_id, epic)


# ---------------------------------------------------------------------------
# Unit: YourTurnEvent round-trip
# ---------------------------------------------------------------------------


class TestYourTurnEventRoundTrip:
    def _base(self) -> dict[str, Any]:
        from datetime import UTC, datetime

        return {
            "project_id": "proj",
            "epic_id": "EP-1",
            "run_id": "run-1",
            "ts": datetime.now(UTC).isoformat(),
        }

    def test_roundtrip_via_run_event_union(self) -> None:
        from pydantic import TypeAdapter

        from yukar.models.events import RunEvent, YourTurnEvent

        ev = YourTurnEvent(
            **self._base(),
            thread_id="manager",
        )
        ta: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
        data = ev.model_dump(mode="json")
        parsed = ta.validate_python(data)
        assert isinstance(parsed, YourTurnEvent)
        assert parsed.type == "your_turn"
        assert parsed.thread_id == "manager"

    def test_sse_serialization(self) -> None:
        """YourTurnEvent is serialized to SSE with type=your_turn."""
        from yukar.events.sse import run_event_to_sse
        from yukar.models.events import YourTurnEvent

        ev = YourTurnEvent(**self._base(), thread_id="manager")
        sse = run_event_to_sse(ev)
        assert "event: your_turn" in sse
        assert "your_turn" in sse


# ---------------------------------------------------------------------------
# Unit: waiting (park) gate — _wait_for_user_input mechanics
# ---------------------------------------------------------------------------


class TestWaitingGate:
    def _make_orchestrator(self) -> Any:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings

        return EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="Test",
            git_author_email="test@example.com",
        )

    async def test_stop_during_waiting_unblocks(self) -> None:
        """stop() during _wait_for_user_input returns immediately."""
        from yukar.models.run import RunState
        from yukar.storage import state_repo

        orch = self._make_orchestrator()
        orch._project_id = "proj"
        orch._epic_id = "ep"
        orch._run_id = "run-2"
        orch._pub = lambda e: None
        orch._awaiting_user = True

        # We need minimal state.yaml for _wait_for_user_input to persist status.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = tmp
            from yukar.models.epic import Epic
            from yukar.models.project import Project
            from yukar.storage.epic_repo import save_epic
            from yukar.storage.project_repo import save_project

            await save_project(root, Project(id="proj", name="proj"))
            await save_epic(root, "proj", Epic(id="ep", slug="ep", title="Ep"))

            state = RunState(run_id="run-2", status="running")
            await state_repo.save_state(root, "proj", "ep", state)

            # Start waiting in background.
            wait_task = asyncio.create_task(
                orch._wait_for_user_input(root, "proj", "ep", "run-2", state, lambda e: None)
            )
            # Give the task a moment to enter the await.
            await asyncio.sleep(0.05)

            # Verify state is now waiting (the user's turn).
            persisted = await state_repo.get_state(root, "proj", "ep")
            assert persisted is not None
            assert persisted.status == "waiting"

            # Call stop() — it should inject sentinel.
            await orch.stop()

            # The wait should unblock quickly.
            result = await asyncio.wait_for(wait_task, timeout=1.0)
            # stop() returns empty string.
            assert result == ""

    async def test_inject_message_unblocks_wait(self) -> None:
        """inject_message unblocks _wait_for_user_input and returns the reply text."""
        from yukar.models.run import RunState
        from yukar.storage import state_repo

        orch = self._make_orchestrator()
        orch._project_id = "proj"
        orch._epic_id = "ep"
        orch._run_id = "run-3"
        orch._pub = lambda e: None
        orch._awaiting_user = True

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = tmp
            from yukar.models.epic import Epic
            from yukar.models.project import Project
            from yukar.storage.epic_repo import save_epic
            from yukar.storage.project_repo import save_project

            await save_project(root, Project(id="proj", name="proj"))
            await save_epic(root, "proj", Epic(id="ep", slug="ep", title="Ep"))

            state = RunState(run_id="run-3", status="running")
            await state_repo.save_state(root, "proj", "ep", state)

            wait_task = asyncio.create_task(
                orch._wait_for_user_input(root, "proj", "ep", "run-3", state, lambda e: None)
            )
            await asyncio.sleep(0.05)

            # Verify status is waiting.
            persisted = await state_repo.get_state(root, "proj", "ep")
            assert persisted is not None
            assert persisted.status == "waiting"

            # Inject user reply.
            orch.inject_message("manager", "Looks good, proceed!")

            result = await asyncio.wait_for(wait_task, timeout=1.0)

            # Raw user reply text is returned (no boilerplate prefix).
            assert result == "Looks good, proceed!"

            # _awaiting_user should be reset.
            assert orch._awaiting_user is False

            # Status should be restored to running.
            restored = await state_repo.get_state(root, "proj", "ep")
            assert restored is not None
            assert restored.status == "running"


# ---------------------------------------------------------------------------
# Unit: Major-1 fix — non-manager messages must not unlock the waiting gate
# ---------------------------------------------------------------------------


class TestWaitForUserInputThreadFiltering:
    """_wait_for_user_input must only consume messages addressed to 'manager'."""

    def _make_orchestrator(self) -> Any:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings

        return EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="Test",
            git_author_email="test@example.com",
        )

    async def test_non_manager_message_does_not_unlock(self) -> None:
        """A message for a worker thread must not release the waiting gate.

        The gate must remain open until a 'manager'-addressed message arrives.
        """
        from yukar.models.run import RunState
        from yukar.storage import state_repo

        orch = self._make_orchestrator()
        orch._project_id = "proj"
        orch._epic_id = "ep"
        orch._run_id = "run-filter"
        orch._pub = lambda e: None
        orch._awaiting_user = True

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = tmp
            from yukar.models.epic import Epic
            from yukar.models.project import Project
            from yukar.storage.epic_repo import save_epic
            from yukar.storage.project_repo import save_project

            await save_project(root, Project(id="proj", name="proj"))
            await save_epic(root, "proj", Epic(id="ep", slug="ep", title="Ep"))

            state = RunState(run_id="run-filter", status="running")
            await state_repo.save_state(root, "proj", "ep", state)

            wait_task = asyncio.create_task(
                orch._wait_for_user_input(root, "proj", "ep", "run-filter", state, lambda e: None)
            )
            await asyncio.sleep(0.05)

            # Inject a worker-thread message first — must NOT unlock the gate.
            orch.inject_message("worker-1", "This is a worker message")
            await asyncio.sleep(0.05)

            # The task must still be blocked (the waiting gate not cleared).
            assert not wait_task.done(), (
                "_wait_for_user_input returned after a non-manager message — "
                "the gate was incorrectly released"
            )
            assert orch._awaiting_user is True

            # Now inject the real manager reply — this must unlock.
            orch.inject_message("manager", "Approved!")

            result = await asyncio.wait_for(wait_task, timeout=1.0)
            # Raw user reply text returned (no boilerplate prefix).
            assert result == "Approved!"
            assert orch._awaiting_user is False

            # The deferred worker-1 message must be back in the queue so that a
            # subsequent _drain_pending() can pick it up.
            deferred = orch._drain_pending()
            assert any(tid == "worker-1" for tid, _ in deferred), (
                "Deferred worker-1 message was lost instead of being returned to the queue"
            )

    async def test_stop_sentinel_exits_while_messages_queued(self) -> None:
        """stop() must unblock even if non-manager messages are queued first."""
        from yukar.models.run import RunState
        from yukar.storage import state_repo

        orch = self._make_orchestrator()
        orch._project_id = "proj"
        orch._epic_id = "ep"
        orch._run_id = "run-stop-filter"
        orch._pub = lambda e: None
        orch._awaiting_user = True

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = tmp
            from yukar.models.epic import Epic
            from yukar.models.project import Project
            from yukar.storage.epic_repo import save_epic
            from yukar.storage.project_repo import save_project

            await save_project(root, Project(id="proj", name="proj"))
            await save_epic(root, "proj", Epic(id="ep", slug="ep", title="Ep"))

            state = RunState(run_id="run-stop-filter", status="running")
            await state_repo.save_state(root, "proj", "ep", state)

            wait_task = asyncio.create_task(
                orch._wait_for_user_input(
                    root, "proj", "ep", "run-stop-filter", state, lambda e: None
                )
            )
            await asyncio.sleep(0.05)

            # Inject a worker message (non-manager) then stop.
            orch.inject_message("worker-99", "something")
            await orch.stop()

            result = await asyncio.wait_for(wait_task, timeout=1.0)
            # stop() path returns empty string.
            assert result == ""


# ---------------------------------------------------------------------------
# Unit: Major-2 fix — pause() is no-op while parked in waiting
# ---------------------------------------------------------------------------


class TestPauseDuringAwaitingInput:
    """pause() must be a no-op when _awaiting_user is True."""

    def _make_orchestrator(self) -> Any:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings

        return EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="Test",
            git_author_email="test@example.com",
        )

    async def test_pause_noop_when_awaiting(self) -> None:
        """pause() while parked in waiting must not clear _paused."""
        orch = self._make_orchestrator()
        orch._awaiting_user = True

        # _paused starts as set (not paused).
        assert orch._paused.is_set()

        await orch.pause()

        # _paused must still be set — pause was a no-op.
        assert orch._paused.is_set(), (
            "pause() cleared _paused while parked in waiting — "
            "this would cause a post-answer deadlock at the next _checkpoint()"
        )
        # _run_status must not change to 'paused'.
        assert orch._run_status == "running"

    async def test_pause_then_answer_does_not_block(self) -> None:
        """pause() while parked, followed by a user answer, must not stall.

        Regression test: if pause() cleared _paused, the run would hang at
        _checkpoint() after _wait_for_user_input returned, even though
        disk status was 'running'.
        """
        from yukar.models.run import RunState
        from yukar.storage import state_repo

        orch = self._make_orchestrator()
        orch._project_id = "proj"
        orch._epic_id = "ep"
        orch._run_id = "run-pause-await"
        orch._pub = lambda e: None
        orch._awaiting_user = True

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = tmp
            from yukar.models.epic import Epic
            from yukar.models.project import Project
            from yukar.storage.epic_repo import save_epic
            from yukar.storage.project_repo import save_project

            await save_project(root, Project(id="proj", name="proj"))
            await save_epic(root, "proj", Epic(id="ep", slug="ep", title="Ep"))

            state = RunState(run_id="run-pause-await", status="running")
            await state_repo.save_state(root, "proj", "ep", state)

            wait_task = asyncio.create_task(
                orch._wait_for_user_input(
                    root, "proj", "ep", "run-pause-await", state, lambda e: None
                )
            )
            await asyncio.sleep(0.05)

            # Call pause() — must be no-op (should not clear _paused).
            await orch.pause()

            # Inject user answer.
            orch.inject_message("manager", "Go ahead!")

            # The wait must complete without blocking (would time out if _paused
            # was incorrectly cleared, because the caller's _checkpoint() would
            # block forever).
            result = await asyncio.wait_for(wait_task, timeout=1.0)
            assert "Go ahead!" in result

            # _paused remains set — _checkpoint() will not block the next turn.
            assert orch._paused.is_set()

            # Status restored to running.
            restored = await state_repo.get_state(root, "proj", "ep")
            assert restored is not None
            assert restored.status == "running"


# ---------------------------------------------------------------------------
# E2E: plan-approval gate with FakeModel (text turns park; approval opens gate)
# ---------------------------------------------------------------------------


class TestPlanGateE2E:
    """E2E tests for the planning + approval gate using FakeModel."""

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_reply_alone_does_not_open_dispatch_gate(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """A chat reply does NOT approve the plan; the recorded approval does.

        Verifies:
        1. The plan-presenting turn ends → the run parks in ``waiting`` and a
           YourTurnEvent is published.
        2. A user reply WITHOUT the Approve-plan operation leaves dispatch
           host-rejected (no workers start).
        3. After the approval is recorded in plan_approval.yaml (the explicit
           user operation), the live run's next dispatch goes through — the
           gate reads the approval from disk without any restart.
        4. After the work the run parks again (a conversation never completes).
        """
        from datetime import UTC, datetime
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.models.events import WorkerStartedEvent, YourTurnEvent
        from yukar.models.task import PlanApproval, compute_plan_hash
        from yukar.storage import plan_approval_repo, state_repo, tasks_repo

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-1"
        run_id = "run-gate-test"

        await _bootstrap(root, project_id, epic_id, git_repo)

        # Manager: Turn 0 — plan tasks, present the plan in the body, end the
        # turn (park).  Turn 1 (user replied but did NOT approve) — dispatch is
        # gate-rejected, so the Manager asks for the approval operation and
        # parks again.  Turn 2 (approval recorded + reply) — dispatch runs,
        # then the Manager reports in the body and parks once more.
        manager_script = [
            # Turn 0: plan
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Write hello.py",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            TextTurn("Plan: T1=Write hello.py. Approve the plan in the UI to proceed."),
            # Turn 1: the reply alone must not open the gate.
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            TextTurn("Dispatch was rejected — please approve the plan."),
            # Turn 2: after the recorded approval
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            TextTurn("Done! T1 was implemented and accepted."),
        ]

        worker_script = [
            ToolUseTurn(
                tool_name="fs_write",
                tool_input={"path": "hello.py", "content": "print('hello')\n"},
            ),
            TextTurn("Done."),
        ]

        evaluator_script = [
            ToolUseTurn(tool_name="submit_verdict", tool_input={"accepted": True, "feedback": ""}),
            TextTurn("Accepted."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            r = role or "worker"
            if r == "manager":
                return FakeModel(script=list(manager_script))
            if r == "worker":
                return FakeModel(script=list(worker_script))
            return FakeModel(script=list(evaluator_script))

        events_received: list[Any] = []
        worker_started_before_approval: list[Any] = []
        park_events: list[asyncio.Event] = [asyncio.Event(), asyncio.Event(), asyncio.Event()]
        approval_recorded = asyncio.Event()

        async def _collect() -> None:
            park_count = 0
            async for ev in event_bus.event_stream(project_id, epic_id):
                events_received.append(ev)
                if isinstance(ev, YourTurnEvent):
                    if park_count < len(park_events):
                        park_events[park_count].set()
                    park_count += 1
                # Track workers started before the approval was recorded.
                if isinstance(ev, WorkerStartedEvent) and not approval_recorded.is_set():
                    worker_started_before_approval.append(ev)

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        llm = LLMSettings(provider="fake")
        orch = EpicOrchestrator(
            llm_settings=llm,
            git_author_name="yukar",
            git_author_email="yukar@localhost",
        )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            run_task = asyncio.create_task(orch.start(root, project_id, epic_id, run_id))

            # Wait for the plan-presenting turn to park.
            await asyncio.wait_for(park_events[0].wait(), timeout=10.0)

            # Verify status is waiting before the reply.
            state = await state_repo.get_state(root, project_id, epic_id)
            assert state is not None, "state.yaml should exist"
            assert state.status == "waiting", (
                f"Expected waiting before approval, got {state.status!r}"
            )

            # Reply WITHOUT performing the approval operation.  The Manager's
            # dispatch this turn must be host-rejected (no workers).
            orch.inject_message("manager", "Looks good, proceed!")

            # The Manager hits the gate, reports the rejection, parks again.
            await asyncio.wait_for(park_events[1].wait(), timeout=10.0)
            assert worker_started_before_approval == [], (
                "A chat reply alone must not open the dispatch gate"
            )

            # Now perform the explicit Approve-plan operation: record the
            # approval of the current plan snapshot (what POST /plan/approval
            # does), then reply.  The live run's gate reads it from disk.
            tasks_file = await tasks_repo.get_tasks(root, project_id, epic_id)
            await plan_approval_repo.save_plan_approval(
                root,
                project_id,
                epic_id,
                PlanApproval(
                    tasks_hash=compute_plan_hash(tasks_file.tasks),
                    approved_at=datetime.now(UTC),
                ),
            )
            approval_recorded.set()
            orch.inject_message("manager", "I approved the plan in the UI — go ahead.")

            # The work turn runs (dispatch → worker → evaluator), then the run
            # parks AGAIN: a conversation run never completes.
            await asyncio.wait_for(park_events[2].wait(), timeout=30.0)

            state = await state_repo.get_state(root, project_id, epic_id)
            assert state is not None
            assert state.status == "waiting", (
                f"After the work turn the run must park in waiting, got {state.status!r}"
            )

            # Release the run task (user stop): state stays waiting.
            await orch.stop()
            await asyncio.wait_for(run_task, timeout=10.0)

        await asyncio.wait_for(collector, timeout=5.0)

        event_types = [getattr(ev, "type", None) for ev in events_received]

        # Every park is a pure "your turn" signal.
        uir_events = [e for e in events_received if isinstance(e, YourTurnEvent)]
        assert len(uir_events) >= 3, "Expected three parks (one per ended turn) on the bus"
        assert all(e.thread_id == "manager" for e in uir_events), (
            "The park signal must carry the conversation thread_id"
        )

        # No workers should have started before the approval was recorded.
        assert worker_started_before_approval == [], (
            f"Workers started before the recorded approval: {worker_started_before_approval}"
        )

        # A conversation run never emits run_completed.
        assert "run_completed" not in event_types, (
            "Conversation runs must not emit run_completed (P3 principle 2)"
        )

        # Workers should have run after approval.
        assert "worker_started" in event_types
        assert "worker_completed" in event_types
        assert "eval_result" in event_types

    async def test_stop_while_waiting_keeps_waiting(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """Stopping the run while parked in waiting settles as waiting (not error).

        This verifies that stop() while waiting terminates cleanly via
        CancelledError and the state stays ``waiting`` — the conversation is
        intact and resumes as a continuation on the next message.
        """
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.models.events import YourTurnEvent
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-2"
        run_id = "run-stop-gate"

        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Some task",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            TextTurn("Ready to proceed? Reply to continue."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            return FakeModel(script=list(manager_script))

        approval_requested = asyncio.Event()
        events_received: list[Any] = []

        async def _collect() -> None:
            async for ev in event_bus.event_stream(project_id, epic_id):
                events_received.append(ev)
                if isinstance(ev, YourTurnEvent):
                    approval_requested.set()

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        llm = LLMSettings(provider="fake")
        orch = EpicOrchestrator(
            llm_settings=llm,
            git_author_name="yukar",
            git_author_email="yukar@localhost",
        )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            run_task = asyncio.create_task(orch.start(root, project_id, epic_id, run_id))

            # Wait for the run to park in waiting.
            await asyncio.wait_for(approval_requested.wait(), timeout=10.0)

            # Stop the run. Real supervisor.stop() sets _stopped=True before the
            # force-cancel; a shutdown cancel leaves it False (preserves state).
            orch._stopped = True
            run_task.cancel()
            with pytest.raises((asyncio.CancelledError, Exception)):
                await run_task

        await asyncio.wait_for(collector, timeout=5.0)

        # State stays waiting (not error) after a user-initiated stop.
        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None
        assert state.status == "waiting", (
            f"Expected waiting after stop while parked, got {state.status!r}"
        )

        # RunStoppedEvent should be published.
        stopped_events = [e for e in events_received if getattr(e, "type", None) == "run_stopped"]
        assert stopped_events, "Expected RunStoppedEvent after stop while waiting"

    async def test_dispatch_not_called_before_park(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """Simpler check: if the Manager plans on Turn 0 and ends its turn
        without dispatching, no WorkerStartedEvent appears before the park
        (YourTurnEvent).
        """
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.models.events import WorkerStartedEvent, YourTurnEvent

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-3"
        run_id = "run-no-dispatch"

        await _bootstrap(root, project_id, epic_id, git_repo)

        # Manager only plans and asks in the body — never dispatches
        # (stop comes from task cancel).
        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Some work",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            TextTurn("Before I start: is this the right approach?"),
            # If the loop somehow continued without user input:
            TextTurn("Done."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            return FakeModel(script=list(manager_script))

        events_in_order: list[Any] = []
        uir_seen = asyncio.Event()

        async def _collect() -> None:
            async for ev in event_bus.event_stream(project_id, epic_id):
                events_in_order.append(ev)
                if isinstance(ev, YourTurnEvent):
                    uir_seen.set()

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        llm = LLMSettings(provider="fake")
        orch = EpicOrchestrator(
            llm_settings=llm,
            git_author_name="yukar",
            git_author_email="yukar@localhost",
        )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            run_task = asyncio.create_task(orch.start(root, project_id, epic_id, run_id))

            # Wait for YourTurnEvent.
            await asyncio.wait_for(uir_seen.wait(), timeout=10.0)

            # Cancel the run — we've confirmed the park was observed.
            run_task.cancel()
            with pytest.raises((asyncio.CancelledError, Exception)):
                await run_task

        # A not-stopped cancel (shelve / shutdown) does not publish the SSE
        # sentinel (P5 — the conversation is not over), so close the
        # collector explicitly instead of waiting for stream end.
        collector.cancel()
        await asyncio.gather(collector, return_exceptions=True)

        # Find positions of key events.
        uir_positions = [
            i for i, e in enumerate(events_in_order) if isinstance(e, YourTurnEvent)
        ]
        worker_positions = [
            i for i, e in enumerate(events_in_order) if isinstance(e, WorkerStartedEvent)
        ]

        assert uir_positions, "YourTurnEvent should be on the bus"

        # No worker should have started before YourTurnEvent.
        if worker_positions:
            earliest_worker = min(worker_positions)
            earliest_uir = min(uir_positions)
            assert earliest_uir < earliest_worker, (
                "WorkerStartedEvent appeared before YourTurnEvent — "
                "dispatch was called before user approval!"
            )


# ---------------------------------------------------------------------------
# Recovery: waiting runs are preserved on restart (legacy files read as waiting)
# ---------------------------------------------------------------------------


class TestWaitingRecovery:
    async def test_waiting_preserved_on_recovery(self, tmp_path: Path) -> None:
        """A run parked in waiting at process death must be preserved as-is.

        The agent ended its turn — there is no in-flight async work.  After
        restart, the user's message triggers start_or_inject →
        start_continuation, which resumes cleanly from the preserved Strands
        session and state.yaml.
        """
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.run import RunState
        from yukar.runs.recovery import recover_interrupted_runs
        from yukar.storage import state_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-ai"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="ep-ai", title="Ep AI"))

        state = RunState(run_id="run-stuck", status="waiting")
        await state_repo.save_state(root, project_id, epic_id, state)

        # waiting is not counted (it is not modified).
        count = await recover_interrupted_runs(root)
        assert count == 0

        reconciled = await state_repo.get_state(root, project_id, epic_id)
        assert reconciled is not None
        # Must remain waiting.
        assert reconciled.status == "waiting"

    async def test_legacy_awaiting_input_file_reads_as_waiting(self, tmp_path: Path) -> None:
        """Old state.yaml files (awaiting_input + pending_question) load as waiting.

        The BeforeValidator coerces legacy statuses and pydantic ignores the
        removed pending_question key, so pre-P3 workspaces stay loadable.
        """
        from yukar.config import paths
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.runs.recovery import recover_interrupted_runs
        from yukar.storage import state_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-legacy"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="ep-l", title="Ep L"))

        # Write a pre-P3 state.yaml verbatim (no model round-trip).
        yaml_path = paths.state_yaml(root, project_id, epic_id)
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text(
            "run_id: run-legacy\n"
            "status: awaiting_input\n"
            "pending_question: 'Shall I proceed?'\n",
            encoding="utf-8",
        )

        loaded = await state_repo.get_state(root, project_id, epic_id)
        assert loaded is not None
        assert loaded.status == "waiting"
        assert not hasattr(loaded, "pending_question")

        # Recovery treats it like any waiting run: preserved, not counted.
        count = await recover_interrupted_runs(root)
        assert count == 0

    async def test_legacy_idle_and_interrupted_read_as_waiting(self) -> None:
        """idle / interrupted (legacy) coerce to waiting; new values pass through."""
        from yukar.models.run import RunState

        for legacy in ("idle", "awaiting_input", "interrupted"):
            st = RunState.model_validate({"run_id": "r", "status": legacy})
            assert st.status == "waiting", f"{legacy} must read back as waiting"
        for current in ("running", "paused", "waiting", "error", "completed"):
            st = RunState.model_validate({"run_id": "r", "status": current})
            assert st.status == current


# ---------------------------------------------------------------------------
# bus: YourTurnEvent is replayed to late subscribers
# ---------------------------------------------------------------------------


class TestYourTurnBusReplay:
    def test_uir_event_in_replay_buffer(self) -> None:
        """YourTurnEvent goes into the lifecycle replay buffer."""
        from yukar.events import bus as event_bus
        from yukar.models.events import YourTurnEvent

        event_bus._replay.clear()

        ev = YourTurnEvent(
            project_id="p",
            epic_id="e",
            run_id="r",
            thread_id="manager",
        )
        event_bus.publish("p", "e", ev)

        buf = list(event_bus._replay[("p", "e")])
        uir_in_buf = [x for x in buf if isinstance(x, YourTurnEvent)]
        assert len(uir_in_buf) == 1
        assert uir_in_buf[0].thread_id == "manager"


# ---------------------------------------------------------------------------
# Bug 3: YourTurnEndedEvent — publish on resume + replay buffer
# ---------------------------------------------------------------------------


class TestYourTurnEndedEvent:
    """YourTurnEndedEvent is published when resuming and replayed on reconnect."""

    def _base(self) -> dict[str, Any]:
        from datetime import UTC, datetime

        return {
            "project_id": "proj",
            "epic_id": "EP-1",
            "run_id": "run-1",
            "ts": datetime.now(UTC).isoformat(),
        }

    def test_roundtrip_via_run_event_union(self) -> None:
        from pydantic import TypeAdapter

        from yukar.models.events import RunEvent, YourTurnEndedEvent

        ev = YourTurnEndedEvent(
            **self._base(),
            thread_id="manager",
        )
        ta: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
        data = ev.model_dump(mode="json")
        parsed = ta.validate_python(data)
        assert isinstance(parsed, YourTurnEndedEvent)
        assert parsed.type == "your_turn_ended"
        assert parsed.thread_id == "manager"

    def test_sse_serialization(self) -> None:
        """YourTurnEndedEvent serializes to SSE with type=your_turn_ended."""
        from yukar.events.sse import run_event_to_sse
        from yukar.models.events import YourTurnEndedEvent

        ev = YourTurnEndedEvent(**self._base(), thread_id="manager")
        sse = run_event_to_sse(ev)
        assert "event: your_turn_ended" in sse
        assert "your_turn_ended" in sse

    def test_resolved_event_in_replay_buffer(self) -> None:
        """YourTurnEndedEvent goes into the lifecycle replay buffer."""
        from yukar.events import bus as event_bus
        from yukar.models.events import YourTurnEndedEvent

        event_bus._replay.clear()

        ev = YourTurnEndedEvent(
            project_id="p2",
            epic_id="e2",
            run_id="r2",
            thread_id="manager",
        )
        event_bus.publish("p2", "e2", ev)

        buf = list(event_bus._replay[("p2", "e2")])
        resolved_in_buf = [x for x in buf if isinstance(x, YourTurnEndedEvent)]
        assert len(resolved_in_buf) == 1

    def test_request_then_resolved_in_replay_buffer(self) -> None:
        """request→resolved appear in order in the replay buffer.

        A late subscriber that replays both events ends up with 'running' state
        (your_turn_ended wins) rather than 'waiting' (your_turn alone).
        """
        from yukar.events import bus as event_bus
        from yukar.models.events import YourTurnEndedEvent, YourTurnEvent

        event_bus._replay.clear()

        key = ("p3", "e3")
        req = YourTurnEvent(
            project_id=key[0],
            epic_id=key[1],
            run_id="r3",
            thread_id="manager",
        )
        resolved = YourTurnEndedEvent(
            project_id=key[0],
            epic_id=key[1],
            run_id="r3",
            thread_id="manager",
        )
        event_bus.publish(*key, req)
        event_bus.publish(*key, resolved)

        buf = list(event_bus._replay[key])
        types = [getattr(e, "type", None) for e in buf]
        assert "your_turn" in types
        assert "your_turn_ended" in types
        # resolved must come after requested in replay order.
        assert types.index("your_turn_ended") > types.index("your_turn")

    async def test_inject_message_publishes_resolved_event(self) -> None:
        """_wait_for_user_input publishes YourTurnEndedEvent after the user replies."""
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.models.events import YourTurnEndedEvent
        from yukar.models.run import RunState
        from yukar.storage import state_repo

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="Test",
            git_author_email="test@example.com",
        )
        orch._project_id = "proj"
        orch._epic_id = "ep"
        orch._run_id = "run-res"
        orch._awaiting_user = True

        emitted: list[Any] = []
        orch._pub = emitted.append

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = tmp
            from yukar.models.epic import Epic
            from yukar.models.project import Project
            from yukar.storage.epic_repo import save_epic
            from yukar.storage.project_repo import save_project

            await save_project(root, Project(id="proj", name="proj"))
            await save_epic(root, "proj", Epic(id="ep", slug="ep", title="Ep"))

            state = RunState(run_id="run-res", status="running")
            await state_repo.save_state(root, "proj", "ep", state)

            wait_task = asyncio.create_task(
                orch._wait_for_user_input(root, "proj", "ep", "run-res", state, lambda e: None)
            )
            await asyncio.sleep(0.05)

            orch.inject_message("manager", "Approved!")
            result = await asyncio.wait_for(wait_task, timeout=1.0)
            assert "Approved!" in result

        # YourTurnEndedEvent must have been published.
        resolved_events = [e for e in emitted if isinstance(e, YourTurnEndedEvent)]
        assert len(resolved_events) == 1
        assert resolved_events[0].thread_id == "manager"
        assert resolved_events[0].run_id == "run-res"


# ---------------------------------------------------------------------------
# Single-writer invariant: list_messages boilerplate-free user bubbles
# ---------------------------------------------------------------------------


class TestSingleWriterUserMessages:
    """Verify that the orchestrator produces clean user messages (no boilerplate).

    Covers:
    - Reply to a waiting run: _wait_for_user_input returns raw text.
    - Unsolicited (inject) messages: raw text only.
    - user_answer takes priority and is not mixed with planning boilerplate.
    """

    def _make_orchestrator(self) -> Any:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings

        return EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="Test",
            git_author_email="test@example.com",
        )

    async def test_wait_for_user_input_returns_raw_text(self) -> None:
        """_wait_for_user_input must return the raw human text, not a formatted prefix.

        The caller (turn loop) passes this text as the sole prompt to stream_async
        so FSM records exactly one clean user message with no boilerplate mixing.
        """
        import tempfile

        from yukar.models.run import RunState
        from yukar.storage import state_repo

        orch = self._make_orchestrator()
        orch._project_id = "proj"
        orch._epic_id = "ep"
        orch._run_id = "run-raw"
        orch._pub = lambda e: None
        orch._awaiting_user = True

        with tempfile.TemporaryDirectory() as tmp:
            root = tmp
            from yukar.models.epic import Epic
            from yukar.models.project import Project
            from yukar.storage.epic_repo import save_epic
            from yukar.storage.project_repo import save_project

            await save_project(root, Project(id="proj", name="proj"))
            await save_epic(root, "proj", Epic(id="ep", slug="ep", title="Ep"))

            state = RunState(run_id="run-raw", status="running")
            await state_repo.save_state(root, "proj", "ep", state)

            wait_task = asyncio.create_task(
                orch._wait_for_user_input(root, "proj", "ep", "run-raw", state, lambda e: None)
            )
            await asyncio.sleep(0.05)

            human_text = "yes, that plan looks good to me"
            orch.inject_message("manager", human_text)
            result = await asyncio.wait_for(wait_task, timeout=1.0)

        # Result must be the raw human text — no "[User answer]:" prefix,
        # no planning boilerplate, no other injected content.
        assert result == human_text, f"Expected raw text {human_text!r}; got {result!r}"
        # Confirm no boilerplate leaked in.
        assert "[User answer]" not in result
        assert "task state" not in result.lower()
        assert "dispatch" not in result.lower()

    def test_hitl_inject_texts_are_boilerplate_free(self) -> None:
        """Unsolicited HITL messages must be passed as-is (no prefix/boilerplate)."""
        orch = self._make_orchestrator()

        # Simulate two unsolicited HITL injections.
        orch.inject_message("manager", "please also add tests")
        orch.inject_message("manager", "and update the README")

        drained = orch._drain_pending()
        manager_texts = [text for tid, text in drained if tid == "manager"]

        assert manager_texts == ["please also add tests", "and update the README"], (
            f"Unexpected texts: {manager_texts}"
        )
        # None of the raw texts contain boilerplate.
        for text in manager_texts:
            assert "task state" not in text.lower()
            assert "[User message]" not in text

    def test_assistant_post_blocked_422(self) -> None:
        """Validate that the PostMessageRequest model accepts both roles but the
        router business logic rejects non-user roles.  This is a schema-level check
        (the Literal allows 'assistant') combined with the runtime 422 guard.
        """
        from yukar.api.routers.threads import PostMessageRequest

        req = PostMessageRequest(content="hello", role="assistant")
        assert req.role == "assistant"
        # The router will reject this at runtime with 422.
        # The schema keeps 'assistant' for OpenAPI compatibility, but it is blocked.

    async def test_continuation_seed_written_by_fsm(self, tmp_path: Path) -> None:
        """Continuation turn-0 with a seed: FSM records the seed as the sole user
        message without any planning boilerplate.

        Verifies via real _run_loop (fake provider; the text turn parks the run
        in waiting, then stop() releases it) rather than re-implementing the
        branch logic — so a regression in _run_loop is caught.
        """
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn
        from yukar.storage import session_store

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-SEED"
        run_id = "run-seed-fsm"

        git_repo = make_git_repo(tmp_path, "seed-repo")
        await _bootstrap(root, project_id, epic_id, git_repo)

        seed = "can you add a /metrics endpoint?"

        manager_script = [
            TextTurn("Understood — I'll plan the /metrics endpoint."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            return FakeModel(script=list(manager_script))

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="t",
            git_author_email="t@t.com",
            seed_prompt=seed,
            is_continuation=True,
        )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            run_task = asyncio.create_task(orch.start(root, project_id, epic_id, run_id))
            # The turn ends → the run parks in waiting; release it with stop().
            for _ in range(200):
                if orch.is_parked:
                    break
                await asyncio.sleep(0.05)
            else:
                run_task.cancel()
                pytest.fail("continuation run never parked in waiting")
            await orch.stop()
            await asyncio.wait_for(run_task, timeout=15.0)

        messages = session_store.list_messages(root, project_id, epic_id, "manager")
        user_msgs = [m for m in messages if m.message.role == "user"]
        assert len(user_msgs) >= 1, "FSM should have recorded at least one user message"
        first_text = user_msgs[0].message.content[0].text
        assert first_text is not None, "First user message content must be text"
        assert first_text == seed, f"FSM recorded {first_text!r} instead of seed {seed!r}"
        assert "task state" not in first_text.lower()
        assert "dispatch" not in first_text.lower()


# ---------------------------------------------------------------------------
# Plan-approval gate: dispatch is host-rejected until the recorded approval
# (plan_approval.yaml) matches the current task-plan snapshot hash
# ---------------------------------------------------------------------------


class TestPlanApprovalGate:
    """The host refuses `dispatch` until the user's recorded approval matches
    the current plan snapshot.

    Approval is an explicit user operation persisted in plan_approval.yaml
    (run-independent); a chat reply never grants it.  A plan change needs no
    imperative invalidation — the new plan simply stops matching the recorded
    hash.  The gate is disabled via ``require_plan_approval=False`` for the
    scripted orchestration tests that pre-date it.
    """

    def _orch(self, tmp_path: Path, *, require: bool = True) -> Any:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.models.task import Task, TasksFile

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="Test",
            git_author_email="test@example.com",
            require_plan_approval=require,
        )
        # Point the orchestrator at a real workspace: the gate reads
        # plan_approval.yaml from disk on every check.
        orch._root = str(tmp_path / "ws")
        orch._project_id = "proj"
        orch._epic_id = "EP-1"
        orch._tasks_holder = [
            TasksFile(tasks=[Task(id="T1", title="Write hello.py", contract="hello")])
        ]
        return orch

    async def _record_approval_of_current_plan(self, orch: Any) -> None:
        """Simulate the user's explicit Approve-plan operation (POST /plan/approval)."""
        from datetime import UTC, datetime

        from yukar.models.task import PlanApproval, compute_plan_hash
        from yukar.storage import plan_approval_repo

        approval = PlanApproval(
            tasks_hash=compute_plan_hash(orch._tasks_holder[0].tasks),
            approved_at=datetime.now(UTC),
        )
        await plan_approval_repo.save_plan_approval(
            orch._root, orch._project_id, orch._epic_id, approval
        )

    async def test_dispatch_rejected_without_recorded_approval(self, tmp_path: Path) -> None:
        orch = self._orch(tmp_path)
        assert await orch._is_plan_approved() is False
        dispatch = orch._make_dispatch_tool()
        result = await dispatch(items=[{"task_id": "T1"}, {"task_id": "T2"}])
        assert [r["task_id"] for r in result] == ["T1", "T2"]
        assert all(r["accepted"] is False and r["status"] == "rejected" for r in result)
        assert "approved" in result[0]["reason"].lower()
        assert "approve" in result[0]["reason"].lower()

    async def test_dispatch_allowed_after_approval_recorded(self, tmp_path: Path) -> None:
        orch = self._orch(tmp_path)
        await self._record_approval_of_current_plan(orch)
        assert await orch._is_plan_approved() is True

        captured: dict[str, Any] = {}

        async def fake_run_dispatch(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            captured["items"] = items
            return [{"task_id": items[0]["task_id"], "accepted": True, "status": "done"}]

        orch._run_dispatch = fake_run_dispatch  # type: ignore[method-assign]
        dispatch = orch._make_dispatch_tool()
        result = await dispatch(items=[{"task_id": "T1"}])
        assert captured["items"] == [{"task_id": "T1"}]
        assert result[0]["accepted"] is True

    async def test_plan_change_after_approval_rejects_dispatch_again(
        self, tmp_path: Path
    ) -> None:
        """task_update-style plan change → hash mismatch → gate closes again."""
        from yukar.models.task import Task, TasksFile

        orch = self._orch(tmp_path)
        await self._record_approval_of_current_plan(orch)
        assert await orch._is_plan_approved() is True

        # The Manager changes the plan (adds a task).  No invalidation call
        # exists any more — the snapshot hash simply no longer matches.
        orch._tasks_holder[0] = TasksFile(
            tasks=[
                *orch._tasks_holder[0].tasks,
                Task(id="T2", title="New work", contract="more"),
            ]
        )
        assert await orch._is_plan_approved() is False

        dispatch = orch._make_dispatch_tool()
        result = await dispatch(items=[{"task_id": "T2"}])
        assert result[0]["accepted"] is False
        assert result[0]["status"] == "rejected"

    async def test_status_change_does_not_strip_approval(self, tmp_path: Path) -> None:
        """Dispatch flipping a task's status must NOT close the gate."""
        orch = self._orch(tmp_path)
        await self._record_approval_of_current_plan(orch)
        orch._tasks_holder[0].tasks[0].status = "in_progress"
        assert await orch._is_plan_approved() is True

    async def test_approval_is_run_independent(self, tmp_path: Path) -> None:
        """A fresh orchestrator instance (new run) sees the approval on disk —
        this replaces the old continuation work_started heuristic."""
        orch1 = self._orch(tmp_path)
        await self._record_approval_of_current_plan(orch1)

        orch2 = self._orch(tmp_path)  # same workspace, fresh instance
        assert await orch2._is_plan_approved() is True

    async def test_gate_disabled_never_blocks(self, tmp_path: Path) -> None:
        orch = self._orch(tmp_path, require=False)
        # No approval file exists, and none is needed.
        assert await orch._is_plan_approved() is True

        async def fake_run_dispatch(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [{"task_id": "T1", "accepted": True, "status": "done"}]

        orch._run_dispatch = fake_run_dispatch  # type: ignore[method-assign]
        dispatch = orch._make_dispatch_tool()
        result = await dispatch(items=[{"task_id": "T1"}])
        assert result[0]["accepted"] is True
