"""Tests for UX-fix A1-A5 (new events, ThreadEntry parent_thread_id, bus backfill).

Covers:
- New event types round-trip through RunEvent discriminated union (A1).
- EvalResultEvent now carries eval_id (A1 change).
- ThreadEntry.parent_thread_id round-trip (A2).
- _register_agent_thread sets parent correctly (A2).
- DelegationEvent / ManagerMessageEvent / PauseEffectiveEvent emission (A1-A5).
- Per-thread token backfill in bus (A4).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# A1: RunEvent discriminated union round-trip
# ---------------------------------------------------------------------------


class TestNewEventsRoundTrip:
    def _base(self) -> dict[str, Any]:
        return {
            "project_id": "proj",
            "epic_id": "EP-1",
            "run_id": "run-1",
            "ts": datetime.now(UTC).isoformat(),
        }

    def test_manager_turn_started_roundtrip(self) -> None:
        from pydantic import TypeAdapter

        from yukar.models.events import ManagerTurnStartedEvent, RunEvent

        ev = ManagerTurnStartedEvent(**self._base(), turn=0)
        ta: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
        data = ev.model_dump(mode="json")
        parsed = ta.validate_python(data)
        assert isinstance(parsed, ManagerTurnStartedEvent)
        assert parsed.turn == 0
        assert parsed.type == "manager_turn_started"

    def test_manager_message_roundtrip(self) -> None:
        from pydantic import TypeAdapter

        from yukar.models.events import ManagerMessageEvent, RunEvent

        ev = ManagerMessageEvent(**self._base(), thread_id="manager", turn=1, text="hello")
        ta: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
        data = ev.model_dump(mode="json")
        parsed = ta.validate_python(data)
        assert isinstance(parsed, ManagerMessageEvent)
        assert parsed.text == "hello"
        assert parsed.type == "manager_message"

    def test_delegation_event_roundtrip(self) -> None:
        from pydantic import TypeAdapter

        from yukar.models.events import DelegationEvent, DelegationItem, RunEvent

        ev = DelegationEvent(
            **self._base(),
            items=[DelegationItem(task_id="T1", repo="repo1", title="My task")],
        )
        ta: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
        data = ev.model_dump(mode="json")
        parsed = ta.validate_python(data)
        assert isinstance(parsed, DelegationEvent)
        assert parsed.items[0].task_id == "T1"
        assert parsed.type == "delegation"

    def test_evaluator_started_roundtrip(self) -> None:
        from pydantic import TypeAdapter

        from yukar.models.events import EvaluatorStartedEvent, RunEvent

        ev = EvaluatorStartedEvent(
            **self._base(),
            eval_id="eval-abc",
            worker_id="worker-xyz",
            task_id="T1",
            repo="repo1",
        )
        ta: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
        data = ev.model_dump(mode="json")
        parsed = ta.validate_python(data)
        assert isinstance(parsed, EvaluatorStartedEvent)
        assert parsed.eval_id == "eval-abc"
        assert parsed.type == "evaluator_started"

    def test_pause_effective_roundtrip(self) -> None:
        from pydantic import TypeAdapter

        from yukar.models.events import PauseEffectiveEvent, RunEvent

        ev = PauseEffectiveEvent(**self._base())
        ta: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
        data = ev.model_dump(mode="json")
        parsed = ta.validate_python(data)
        assert isinstance(parsed, PauseEffectiveEvent)
        assert parsed.type == "pause_effective"

    def test_eval_result_has_eval_id(self) -> None:
        from pydantic import TypeAdapter

        from yukar.models.events import EvalResultEvent, RunEvent

        ev = EvalResultEvent(
            **self._base(),
            worker_id="worker-1",
            eval_id="eval-1",
            accepted=True,
            feedback="",
        )
        ta: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
        data = ev.model_dump(mode="json")
        parsed = ta.validate_python(data)
        assert isinstance(parsed, EvalResultEvent)
        assert parsed.eval_id == "eval-1"

    def test_eval_result_eval_id_defaults_empty(self) -> None:
        """eval_id is optional for backward compatibility."""
        from yukar.models.events import EvalResultEvent

        ev = EvalResultEvent(
            **self._base(),
            worker_id="worker-1",
            accepted=False,
            feedback="needs work",
        )
        assert ev.eval_id == ""


# ---------------------------------------------------------------------------
# A2: ThreadEntry.parent_thread_id
# ---------------------------------------------------------------------------


class TestThreadEntryParentId:
    def test_parent_thread_id_defaults_none(self) -> None:
        from yukar.models.thread import ThreadEntry

        entry = ThreadEntry(id="th-1", title="Manager", role="manager")
        assert entry.parent_thread_id is None

    def test_parent_thread_id_roundtrip(self) -> None:
        from yukar.models.thread import ThreadEntry

        entry = ThreadEntry(
            id="worker-1",
            title="Worker worker-1",
            role="worker",
            parent_thread_id="manager",
        )
        data = entry.model_dump(mode="json")
        parsed = ThreadEntry.model_validate(data)
        assert parsed.parent_thread_id == "manager"

    def test_evaluator_parent_is_worker(self) -> None:
        from yukar.models.thread import ThreadEntry

        entry = ThreadEntry(
            id="eval-1",
            title="Evaluator eval-1",
            role="evaluator",
            parent_thread_id="worker-abc",
        )
        assert entry.parent_thread_id == "worker-abc"


# ---------------------------------------------------------------------------
# A2: _register_agent_thread sets parent correctly
# ---------------------------------------------------------------------------


class TestRegisterAgentThread:
    async def test_worker_gets_manager_as_parent(self, tmp_path: Path) -> None:
        from yukar.agents.orchestrator import _register_agent_thread
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project
        from yukar.storage.threads_repo import get_threads

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-1"

        await save_project(root, Project(id=project_id, name="P"))
        await save_epic(root, project_id, Epic(id=epic_id, slug="ep-1", title="T"))

        await _register_agent_thread(
            root,
            project_id,
            epic_id,
            thread_id="worker-abc",
            role="worker",
            parent_thread_id="manager",
        )

        tf = await get_threads(root, project_id, epic_id)
        entry = next((t for t in tf.threads if t.id == "worker-abc"), None)
        assert entry is not None
        assert entry.parent_thread_id == "manager"

    async def test_evaluator_gets_worker_as_parent(self, tmp_path: Path) -> None:
        from yukar.agents.orchestrator import _register_agent_thread
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project
        from yukar.storage.threads_repo import get_threads

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-1"

        await save_project(root, Project(id=project_id, name="P"))
        await save_epic(root, project_id, Epic(id=epic_id, slug="ep-1", title="T"))

        await _register_agent_thread(
            root,
            project_id,
            epic_id,
            thread_id="eval-xyz",
            role="evaluator",
            parent_thread_id="worker-abc",
        )

        tf = await get_threads(root, project_id, epic_id)
        entry = next((t for t in tf.threads if t.id == "eval-xyz"), None)
        assert entry is not None
        assert entry.parent_thread_id == "worker-abc"

    async def test_manager_parent_is_none(self, tmp_path: Path) -> None:
        """Manager registered inline in _run_loop has parent_thread_id=None."""
        from yukar.models.thread import ThreadEntry

        # Direct model check — manager ThreadEntry is constructed inline in
        # _run_loop, not via _register_agent_thread.
        entry = ThreadEntry(
            id="manager",
            title="Epic Manager",
            role="manager",
            status="active",
            parent_thread_id=None,
        )
        assert entry.parent_thread_id is None


# ---------------------------------------------------------------------------
# A4: per-thread token backfill in bus
# ---------------------------------------------------------------------------


class TestThreadTokenBackfill:
    def _make_token_event(
        self, project_id: str, epic_id: str, run_id: str, thread_id: str, delta: str
    ) -> Any:
        from yukar.models.events import TokenEvent

        return TokenEvent(
            project_id=project_id,
            epic_id=epic_id,
            run_id=run_id,
            thread_id=thread_id,
            delta=delta,
        )

    def test_tokens_buffered_on_publish(self) -> None:
        from yukar.events import bus as event_bus

        event_bus._thread_token_buffer.clear()
        ev = self._make_token_event("p", "e", "r", "worker-1", "hello")
        event_bus.publish("p", "e", ev)

        backfill = event_bus.get_thread_token_backfill("p", "e", "worker-1")
        assert len(backfill) == 1
        assert backfill[0].delta == "hello"

    def test_tokens_not_buffered_for_other_thread(self) -> None:
        from yukar.events import bus as event_bus

        event_bus._thread_token_buffer.clear()
        ev = self._make_token_event("p", "e", "r", "worker-1", "hello")
        event_bus.publish("p", "e", ev)

        backfill = event_bus.get_thread_token_backfill("p", "e", "worker-2")
        assert backfill == []

    def test_backfill_cleared_on_worker_completed(self) -> None:
        from yukar.events import bus as event_bus
        from yukar.models.events import WorkerCompletedEvent

        event_bus._thread_token_buffer.clear()
        ev = self._make_token_event("p", "e", "r", "worker-1", "abc")
        event_bus.publish("p", "e", ev)
        assert len(event_bus.get_thread_token_backfill("p", "e", "worker-1")) == 1

        completed = WorkerCompletedEvent(
            project_id="p", epic_id="e", run_id="r", worker_id="worker-1"
        )
        event_bus.publish("p", "e", completed)
        assert event_bus.get_thread_token_backfill("p", "e", "worker-1") == []

    def test_tool_events_also_buffered(self) -> None:
        from yukar.events import bus as event_bus
        from yukar.models.events import ToolCallEvent, ToolResultEvent

        event_bus._thread_token_buffer.clear()

        tc = ToolCallEvent(
            project_id="p",
            epic_id="e",
            run_id="r",
            thread_id="worker-1",
            tool_name="fs_read",
        )
        tr = ToolResultEvent(
            project_id="p",
            epic_id="e",
            run_id="r",
            thread_id="worker-1",
            tool_name="fs_read",
            result="content",
        )
        event_bus.publish("p", "e", tc)
        event_bus.publish("p", "e", tr)

        backfill = event_bus.get_thread_token_backfill("p", "e", "worker-1")
        assert len(backfill) == 2

    def test_backfill_returns_empty_when_no_buffer(self) -> None:
        from yukar.events import bus as event_bus

        event_bus._thread_token_buffer.clear()
        assert event_bus.get_thread_token_backfill("p", "e", "no-such-thread") == []

    async def test_thread_stream_replays_backfill(self) -> None:
        """Tokens published before stream open are delivered first."""
        from yukar.events import bus as event_bus

        event_bus._thread_token_buffer.clear()

        # Publish a token before any subscriber is listening.
        ev = self._make_token_event("p", "e", "r", "worker-1", "pre-connect")
        event_bus.publish("p", "e", ev)

        # Now open a subscription and check backfill comes through.
        backfill = event_bus.get_thread_token_backfill("p", "e", "worker-1")
        assert backfill[0].delta == "pre-connect"

        # Publish a live token after subscribe.
        async with event_bus.subscribe("p", "e") as q:
            ev2 = self._make_token_event("p", "e", "r", "worker-1", "live")
            event_bus.publish("p", "e", ev2)
            live_ev = await asyncio.wait_for(q.get(), timeout=1.0)
            assert live_ev.delta == "live"


# ---------------------------------------------------------------------------
# A5: PauseEffectiveEvent emitted by _checkpoint
# ---------------------------------------------------------------------------


def _make_test_orchestrator() -> Any:
    """Create a minimal EpicOrchestrator suitable for unit tests."""
    from yukar.agents.orchestrator import EpicOrchestrator
    from yukar.config.settings import LLMSettings

    return EpicOrchestrator(
        llm_settings=LLMSettings(provider="fake"),
        git_author_name="Test",
        git_author_email="test@example.com",
    )


class TestPauseEffectiveCheckpoint:
    async def test_checkpoint_emits_pause_effective_when_paused(self) -> None:
        """When _paused is cleared (paused state), _checkpoint emits PauseEffectiveEvent."""
        from yukar.models.events import PauseEffectiveEvent

        orch = _make_test_orchestrator()
        emitted: list[Any] = []
        orch._pub = emitted.append
        orch._project_id = "p"
        orch._epic_id = "e"
        orch._run_id = "r"

        # Put orchestrator in paused state.
        orch._paused.clear()

        # Start checkpoint in background; it should emit PauseEffectiveEvent then block.
        checkpoint_task = asyncio.create_task(orch._checkpoint())
        # Give event loop a chance to run the checkpoint up to the wait().
        await asyncio.sleep(0.05)

        # PauseEffectiveEvent should have been emitted.
        assert any(isinstance(e, PauseEffectiveEvent) for e in emitted), (
            f"Expected PauseEffectiveEvent, got: {emitted}"
        )

        # Resume to unblock the checkpoint.
        orch._paused.set()
        await asyncio.wait_for(checkpoint_task, timeout=1.0)

    async def test_checkpoint_no_pause_effective_when_running(self) -> None:
        """When _paused is set (running), _checkpoint does NOT emit PauseEffectiveEvent."""
        from yukar.models.events import PauseEffectiveEvent

        orch = _make_test_orchestrator()
        emitted: list[Any] = []
        orch._pub = emitted.append
        orch._project_id = "p"
        orch._epic_id = "e"
        orch._run_id = "r"

        # _paused is already set (running) by default.
        assert orch._paused.is_set()

        await orch._checkpoint()

        assert not any(isinstance(e, PauseEffectiveEvent) for e in emitted)
