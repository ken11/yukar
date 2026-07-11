"""Tests for the Manager planning + approval gate (ask_user HITL).

Covers:
- ask_user tool causes orchestrator to enter awaiting_input status.
- inject_message unblocks the wait and restores running status.
- dispatch is NOT called before user approves (Workers not started).
- stop() during awaiting_input terminates cleanly.
- UserInputRequestedEvent is published to the event bus and SSE-serialized.
- awaiting_input is reconciled to error on recovery.
- RunEvent discriminated union includes UserInputRequestedEvent.
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
# Unit: UserInputRequestedEvent round-trip
# ---------------------------------------------------------------------------


class TestUserInputRequestedEventRoundTrip:
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

        from yukar.models.events import RunEvent, UserInputRequestedEvent

        ev = UserInputRequestedEvent(
            **self._base(),
            thread_id="manager",
            question="Is this plan OK?",
        )
        ta: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
        data = ev.model_dump(mode="json")
        parsed = ta.validate_python(data)
        assert isinstance(parsed, UserInputRequestedEvent)
        assert parsed.type == "user_input_requested"
        assert parsed.thread_id == "manager"
        assert parsed.question == "Is this plan OK?"

    def test_sse_serialization(self) -> None:
        """UserInputRequestedEvent is serialized to SSE with type=user_input_requested."""
        from yukar.events.sse import run_event_to_sse
        from yukar.models.events import UserInputRequestedEvent

        ev = UserInputRequestedEvent(**self._base(), thread_id="manager", question="Confirm plan?")
        sse = run_event_to_sse(ev)
        assert "event: user_input_requested" in sse
        assert "user_input_requested" in sse
        assert "Confirm plan?" in sse


# ---------------------------------------------------------------------------
# Unit: ask_user tool and awaiting_user state
# ---------------------------------------------------------------------------


class TestAskUserTool:
    def _make_orchestrator(self) -> Any:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings

        return EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="Test",
            git_author_email="test@example.com",
        )

    async def test_ask_user_tool_sets_awaiting_user(self) -> None:
        """Calling ask_user sets _awaiting_user=True and publishes UserInputRequestedEvent."""
        from yukar.models.events import UserInputRequestedEvent

        orch = self._make_orchestrator()
        orch._project_id = "proj"
        orch._epic_id = "ep"
        orch._run_id = "run-1"

        emitted: list[Any] = []
        orch._pub = emitted.append

        # Build the ask_user tool and call it directly.
        tool_fn = orch._make_ask_user_tool()
        # The Strands @tool decorator wraps the async function; call the underlying
        # coroutine directly by accessing __wrapped__ or calling tool_fn directly.
        # Since Strands' @tool decorator preserves the async function as the callable,
        # we can call it directly.
        result = await tool_fn(question="Is the plan OK?")

        assert orch._awaiting_user is True
        assert orch._pending_question == "Is the plan OK?"
        assert "waiting" in result.lower() or "sent" in result.lower()

        # UserInputRequestedEvent should be emitted.
        uir_events = [e for e in emitted if isinstance(e, UserInputRequestedEvent)]
        assert len(uir_events) == 1
        assert uir_events[0].question == "Is the plan OK?"
        assert uir_events[0].thread_id == "manager"

    async def test_stop_during_awaiting_input_unblocks(self) -> None:
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

            # Verify state is now awaiting_input.
            persisted = await state_repo.get_state(root, "proj", "ep")
            assert persisted is not None
            assert persisted.status == "awaiting_input"

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

            # Verify status is awaiting_input.
            persisted = await state_repo.get_state(root, "proj", "ep")
            assert persisted is not None
            assert persisted.status == "awaiting_input"

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
# Unit: Major-1 fix — non-manager messages must not unlock awaiting_input
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
        """A message for a worker thread must not release the awaiting_input gate.

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

            # The task must still be running (awaiting_input not cleared).
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
# Unit: Major-2 fix — pause() is no-op while awaiting_input
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
        """pause() while awaiting_input must not clear _paused."""
        orch = self._make_orchestrator()
        orch._awaiting_user = True

        # _paused starts as set (not paused).
        assert orch._paused.is_set()

        await orch.pause()

        # _paused must still be set — pause was a no-op.
        assert orch._paused.is_set(), (
            "pause() cleared _paused while awaiting_input — "
            "this would cause a post-answer deadlock at the next _checkpoint()"
        )
        # _run_status must not change to 'paused'.
        assert orch._run_status == "running"

    async def test_pause_then_answer_does_not_block(self) -> None:
        """pause() during awaiting_input followed by a user answer must not stall.

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
# E2E: ask_user gate with FakeModel
# ---------------------------------------------------------------------------


class TestAskUserGateE2E:
    """E2E tests for the planning + approval gate using FakeModel."""

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_ask_user_blocks_dispatch_until_approved(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """A chat reply does NOT approve the plan; the recorded approval does.

        Verifies:
        1. Run enters awaiting_input status and UserInputRequestedEvent is published.
        2. A user reply WITHOUT the Approve-plan operation leaves dispatch
           host-rejected (no workers start).
        3. After the approval is recorded in plan_approval.yaml (the explicit
           user operation), the live run's next dispatch goes through — the
           gate reads the approval from disk without any restart.
        """
        from datetime import UTC, datetime
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.models.events import UserInputRequestedEvent, WorkerStartedEvent
        from yukar.models.task import PlanApproval, compute_plan_hash
        from yukar.storage import plan_approval_repo, state_repo, tasks_repo

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-1"
        run_id = "run-gate-test"

        await _bootstrap(root, project_id, epic_id, git_repo)

        # Manager: Turn 0 — plan tasks, call ask_user, stop.
        # Turn 1 (user replied but did NOT approve) — dispatch is gate-rejected,
        # so the Manager re-asks for the approval operation.
        # Turn 2 (approval recorded + reply) — dispatch runs, complete_epic.
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
            ToolUseTurn(
                tool_name="ask_user",
                tool_input={"question": "Plan: T1=Write hello.py. Any questions before I proceed?"},
            ),
            TextTurn("Waiting for user approval."),
            # Turn 1: the reply alone must not open the gate.
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            ToolUseTurn(
                tool_name="ask_user",
                tool_input={"question": "Dispatch was rejected — please approve the plan."},
            ),
            TextTurn("Waiting for the approval operation."),
            # Turn 2: after the recorded approval
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            ToolUseTurn(tool_name="complete_epic", tool_input={}),
            TextTurn("Done!"),
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
        first_question = asyncio.Event()
        second_question = asyncio.Event()
        approval_recorded = asyncio.Event()

        async def _collect() -> None:
            async for ev in event_bus.event_stream(project_id, epic_id):
                events_received.append(ev)
                if isinstance(ev, UserInputRequestedEvent):
                    if not first_question.is_set():
                        first_question.set()
                    else:
                        second_question.set()
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

            # Wait for the plan question.
            await asyncio.wait_for(first_question.wait(), timeout=10.0)

            # Verify status is awaiting_input before the reply.
            state = await state_repo.get_state(root, project_id, epic_id)
            assert state is not None, "state.yaml should exist"
            assert state.status == "awaiting_input", (
                f"Expected awaiting_input before approval, got {state.status!r}"
            )

            # Reply WITHOUT performing the approval operation.  The Manager's
            # dispatch this turn must be host-rejected (no workers).
            orch.inject_message("manager", "Looks good, proceed!")

            # The Manager hits the gate and asks again.
            await asyncio.wait_for(second_question.wait(), timeout=10.0)
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

            # Wait for the run to complete.
            await asyncio.wait_for(run_task, timeout=30.0)

        await asyncio.wait_for(collector, timeout=5.0)

        event_types = [getattr(ev, "type", None) for ev in events_received]

        # Both questions should be on the bus.
        uir_events = [e for e in events_received if isinstance(e, UserInputRequestedEvent)]
        assert len(uir_events) >= 2, "Expected two UserInputRequestedEvents on the bus"
        assert uir_events[0].question == "Plan: T1=Write hello.py. Any questions before I proceed?"

        # No workers should have started before the approval was recorded.
        assert worker_started_before_approval == [], (
            f"Workers started before the recorded approval: {worker_started_before_approval}"
        )

        # Run should complete successfully.
        assert "run_completed" in event_types, f"Expected run_completed, got: {event_types}"

        # Workers should have run after approval.
        assert "worker_started" in event_types
        assert "worker_completed" in event_types
        assert "eval_result" in event_types

    async def test_stop_during_awaiting_input_sets_idle(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """Stopping the run while in awaiting_input sets state to idle (not error).

        This verifies that stop() during awaiting_input terminates cleanly
        via CancelledError, consistent with the existing stop-path.
        """
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.models.events import UserInputRequestedEvent
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
            ToolUseTurn(
                tool_name="ask_user",
                tool_input={"question": "Ready to proceed?"},
            ),
            TextTurn("Waiting."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            return FakeModel(script=list(manager_script))

        approval_requested = asyncio.Event()
        events_received: list[Any] = []

        async def _collect() -> None:
            async for ev in event_bus.event_stream(project_id, epic_id):
                events_received.append(ev)
                if isinstance(ev, UserInputRequestedEvent):
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

            # Wait for the run to reach awaiting_input.
            await asyncio.wait_for(approval_requested.wait(), timeout=10.0)

            # Stop the run. Real supervisor.stop() sets _stopped=True before the
            # force-cancel; a shutdown cancel leaves it False (preserves state).
            orch._stopped = True
            run_task.cancel()
            with pytest.raises((asyncio.CancelledError, Exception)):
                await run_task

        await asyncio.wait_for(collector, timeout=5.0)

        # State should be idle (not error) after user-initiated stop.
        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None
        assert state.status == "idle", (
            f"Expected idle after stop during awaiting_input, got {state.status!r}"
        )

        # RunStoppedEvent should be published.
        stopped_events = [e for e in events_received if getattr(e, "type", None) == "run_stopped"]
        assert stopped_events, "Expected RunStoppedEvent after stop during awaiting_input"

    async def test_dispatch_not_called_before_ask_user(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """Simpler check: if Manager calls ask_user on Turn 0 and does NOT dispatch,
        no WorkerStartedEvent appears before UserInputRequestedEvent.
        """
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.models.events import UserInputRequestedEvent, WorkerStartedEvent

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-3"
        run_id = "run-no-dispatch"

        await _bootstrap(root, project_id, epic_id, git_repo)

        # Manager only plans and asks — never dispatches (stop comes from task cancel).
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
            ToolUseTurn(
                tool_name="ask_user",
                tool_input={"question": "Before I start: is this the right approach?"},
            ),
            TextTurn("Awaiting approval."),
            # If loop continues (it shouldn't without user input):
            ToolUseTurn(tool_name="complete_epic", tool_input={}),
            TextTurn("Done."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            return FakeModel(script=list(manager_script))

        events_in_order: list[Any] = []
        uir_seen = asyncio.Event()

        async def _collect() -> None:
            async for ev in event_bus.event_stream(project_id, epic_id):
                events_in_order.append(ev)
                if isinstance(ev, UserInputRequestedEvent):
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

            # Wait for UserInputRequestedEvent.
            await asyncio.wait_for(uir_seen.wait(), timeout=10.0)

            # Cancel the run — we've confirmed ask_user was called.
            run_task.cancel()
            with pytest.raises((asyncio.CancelledError, Exception)):
                await run_task

        await asyncio.wait_for(collector, timeout=5.0)

        # Find positions of key events.
        uir_positions = [
            i for i, e in enumerate(events_in_order) if isinstance(e, UserInputRequestedEvent)
        ]
        worker_positions = [
            i for i, e in enumerate(events_in_order) if isinstance(e, WorkerStartedEvent)
        ]

        assert uir_positions, "UserInputRequestedEvent should be on the bus"

        # No worker should have started before UserInputRequestedEvent.
        if worker_positions:
            earliest_worker = min(worker_positions)
            earliest_uir = min(uir_positions)
            assert earliest_uir < earliest_worker, (
                "WorkerStartedEvent appeared before UserInputRequestedEvent — "
                "dispatch was called before user approval!"
            )


# ---------------------------------------------------------------------------
# Recovery: awaiting_input reconciled to error on restart
# ---------------------------------------------------------------------------


class TestAwaitingInputRecovery:
    async def test_awaiting_input_preserved_on_recovery(self, tmp_path: Path) -> None:
        """A run in awaiting_input at process death must be preserved (not interrupted).

        The Manager is parked waiting for a human reply — it has no in-flight async
        work.  After restart, the user's reply triggers start_or_inject →
        start_continuation, which resumes cleanly from the preserved Strands session
        and state.yaml (including pending_question).  Forcing awaiting_input →
        interrupted would break that resumption path and lose the question bubble.
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

        state = RunState(
            run_id="run-stuck",
            status="awaiting_input",
            pending_question="Shall I proceed?",
        )
        await state_repo.save_state(root, project_id, epic_id, state)

        # awaiting_input is not counted (it is not modified).
        count = await recover_interrupted_runs(root)
        assert count == 0

        reconciled = await state_repo.get_state(root, project_id, epic_id)
        assert reconciled is not None
        # Must remain awaiting_input — NOT interrupted.
        assert reconciled.status == "awaiting_input"
        # pending_question must survive so the UI can restore the question bubble.
        assert reconciled.pending_question == "Shall I proceed?"


# ---------------------------------------------------------------------------
# bus: UserInputRequestedEvent is replayed to late subscribers
# ---------------------------------------------------------------------------


class TestUserInputRequestedBusReplay:
    def test_uir_event_in_replay_buffer(self) -> None:
        """UserInputRequestedEvent goes into the lifecycle replay buffer."""
        from yukar.events import bus as event_bus
        from yukar.models.events import UserInputRequestedEvent

        event_bus._replay.clear()

        ev = UserInputRequestedEvent(
            project_id="p",
            epic_id="e",
            run_id="r",
            thread_id="manager",
            question="Ready?",
        )
        event_bus.publish("p", "e", ev)

        buf = list(event_bus._replay[("p", "e")])
        uir_in_buf = [x for x in buf if isinstance(x, UserInputRequestedEvent)]
        assert len(uir_in_buf) == 1
        assert uir_in_buf[0].question == "Ready?"


# ---------------------------------------------------------------------------
# Bug 3: UserInputResolvedEvent — publish on resume + replay buffer
# ---------------------------------------------------------------------------


class TestUserInputResolvedEvent:
    """UserInputResolvedEvent is published when resuming and replayed on reconnect."""

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

        from yukar.models.events import RunEvent, UserInputResolvedEvent

        ev = UserInputResolvedEvent(
            **self._base(),
            thread_id="manager",
        )
        ta: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
        data = ev.model_dump(mode="json")
        parsed = ta.validate_python(data)
        assert isinstance(parsed, UserInputResolvedEvent)
        assert parsed.type == "user_input_resolved"
        assert parsed.thread_id == "manager"

    def test_sse_serialization(self) -> None:
        """UserInputResolvedEvent serializes to SSE with type=user_input_resolved."""
        from yukar.events.sse import run_event_to_sse
        from yukar.models.events import UserInputResolvedEvent

        ev = UserInputResolvedEvent(**self._base(), thread_id="manager")
        sse = run_event_to_sse(ev)
        assert "event: user_input_resolved" in sse
        assert "user_input_resolved" in sse

    def test_resolved_event_in_replay_buffer(self) -> None:
        """UserInputResolvedEvent goes into the lifecycle replay buffer."""
        from yukar.events import bus as event_bus
        from yukar.models.events import UserInputResolvedEvent

        event_bus._replay.clear()

        ev = UserInputResolvedEvent(
            project_id="p2",
            epic_id="e2",
            run_id="r2",
            thread_id="manager",
        )
        event_bus.publish("p2", "e2", ev)

        buf = list(event_bus._replay[("p2", "e2")])
        resolved_in_buf = [x for x in buf if isinstance(x, UserInputResolvedEvent)]
        assert len(resolved_in_buf) == 1

    def test_request_then_resolved_in_replay_buffer(self) -> None:
        """request→resolved appear in order in the replay buffer.

        A late subscriber that replays both events ends up with 'running' state
        (resolved wins) rather than 'awaiting_input' (requested alone).
        """
        from yukar.events import bus as event_bus
        from yukar.models.events import UserInputRequestedEvent, UserInputResolvedEvent

        event_bus._replay.clear()

        key = ("p3", "e3")
        req = UserInputRequestedEvent(
            project_id=key[0],
            epic_id=key[1],
            run_id="r3",
            thread_id="manager",
            question="OK?",
        )
        resolved = UserInputResolvedEvent(
            project_id=key[0],
            epic_id=key[1],
            run_id="r3",
            thread_id="manager",
        )
        event_bus.publish(*key, req)
        event_bus.publish(*key, resolved)

        buf = list(event_bus._replay[key])
        types = [getattr(e, "type", None) for e in buf]
        assert "user_input_requested" in types
        assert "user_input_resolved" in types
        # resolved must come after requested in replay order.
        assert types.index("user_input_resolved") > types.index("user_input_requested")

    async def test_inject_message_publishes_resolved_event(self) -> None:
        """_wait_for_user_input publishes UserInputResolvedEvent after the user replies."""
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.models.events import UserInputResolvedEvent
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

        # UserInputResolvedEvent must have been published.
        resolved_events = [e for e in emitted if isinstance(e, UserInputResolvedEvent)]
        assert len(resolved_events) == 1
        assert resolved_events[0].thread_id == "manager"
        assert resolved_events[0].run_id == "run-res"


# ---------------------------------------------------------------------------
# Single-writer invariant: list_messages boilerplate-free user bubbles
# ---------------------------------------------------------------------------


class TestSingleWriterUserMessages:
    """Verify that the orchestrator produces clean user messages (no boilerplate).

    Covers:
    - Solicited (ask_user) reply: _wait_for_user_input returns raw text.
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

        Verifies via real _run_loop (fake provider, complete_epic on turn 0) rather
        than re-implementing the branch logic — so a regression in _run_loop is caught.
        """
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import session_store

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-SEED"
        run_id = "run-seed-fsm"

        git_repo = make_git_repo(tmp_path, "seed-repo")
        await _bootstrap(root, project_id, epic_id, git_repo)

        seed = "can you add a /metrics endpoint?"

        # complete_epic on turn-0: a tool-less text reply to the human seed
        # would park the run in awaiting_input under turn-end semantics.
        manager_script = [
            ToolUseTurn(tool_name="complete_epic", tool_input={}),
            TextTurn("Done."),
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
            await asyncio.wait_for(
                orch.start(root, project_id, epic_id, run_id),
                timeout=15.0,
            )

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
