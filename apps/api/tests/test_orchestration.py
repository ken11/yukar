"""Tests for M2 agent orchestration layer.

Covers:
- agents/streaming: StreamTranslator callback → bus events
- agents/orchestrator: EpicOrchestrator with FakeModel E2E
- runs/recovery: orphaned running/paused state reconciliation
- supervisor HITL injection
- stop/cancel mid-run state consistency
- retry-limit → blocked task
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests._helpers import make_git_repo, run_until_parked, wait_for_run_status

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> tuple[str, str, str]:
    """Create minimal workspace + project + epic structures.

    Returns (root, project_id, epic_id).
    """
    root = str(tmp_path / "ws")
    project_id = "proj"
    epic_id = "EP-1"
    return root, project_id, epic_id


async def _bootstrap(root: str, project_id: str, epic_id: str, repo_path: Path) -> None:
    """Write the minimal YAML files needed for an orchestrator run."""
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
# StreamTranslator tests
# ---------------------------------------------------------------------------


class TestStreamTranslator:
    """Unit tests for agents/streaming.py.

    All tests use the real message-kwargs interface ({"message": <Message>})
    that Strands actually delivers, as verified by probe_callback.py.
    Synthetic "type==tool_use_stream" kwargs are intentionally NOT used here
    because that event carries only a partial-JSON string input and is not the
    authoritative source of tool call data.
    """

    async def test_text_delta_published(self) -> None:
        from yukar.agents.streaming import StreamTranslator
        from yukar.events import bus as event_bus
        from yukar.models.events import TokenEvent

        translator = StreamTranslator(project_id="p", epic_id="e", run_id="r", thread_id="t")

        received: list[Any] = []

        async with event_bus.subscribe("p", "e") as q:
            translator.callback(data="hello world")
            try:
                event = await asyncio.wait_for(q.get(), timeout=0.5)
                received.append(event)
            except TimeoutError:
                pass

        assert len(received) == 1
        assert isinstance(received[0], TokenEvent)
        assert received[0].delta == "hello world"
        assert received[0].thread_id == "t"

    async def test_empty_text_not_published(self) -> None:
        from yukar.agents.streaming import StreamTranslator
        from yukar.events import bus as event_bus

        translator = StreamTranslator(project_id="p", epic_id="e", run_id="r", thread_id="t")

        async with event_bus.subscribe("p", "e") as q:
            translator.callback(data="")
            # Nothing should be published
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.1)

    async def test_tool_call_published_via_message(self) -> None:
        """ToolCallEvent is published from assistant message kwargs (real interface)."""
        from yukar.agents.streaming import StreamTranslator
        from yukar.events import bus as event_bus
        from yukar.models.events import ToolCallEvent

        translator = StreamTranslator(project_id="p", epic_id="e", run_id="r", thread_id="t")

        received: list[Any] = []

        # Exact shape delivered by Strands (verified via probe):
        # ModelMessageEvent({"message": {"role": "assistant", "content": [{"toolUse": {...}}]}})
        assistant_message = {
            "role": "assistant",
            "content": [
                {
                    "toolUse": {
                        "toolUseId": "uid-001",
                        "name": "fs_read",
                        "input": {"path": "x.py"},
                    }
                }
            ],
        }

        async with event_bus.subscribe("p", "e") as q:
            translator.callback(message=assistant_message)
            try:
                event = await asyncio.wait_for(q.get(), timeout=0.5)
                received.append(event)
            except TimeoutError:
                pass

        assert len(received) == 1
        assert isinstance(received[0], ToolCallEvent)
        assert received[0].tool_name == "fs_read"
        assert received[0].tool_use_id == "uid-001"
        assert received[0].tool_input == {"path": "x.py"}
        assert received[0].thread_id == "t"

    async def test_tool_result_published_via_message(self) -> None:
        """ToolResultEvent is published from user message kwargs (real interface)."""
        from yukar.agents.streaming import StreamTranslator
        from yukar.events import bus as event_bus
        from yukar.models.events import ToolResultEvent

        translator = StreamTranslator(project_id="p", epic_id="e2", run_id="r", thread_id="t")

        received: list[Any] = []

        # First fire assistant message to register id→name.
        translator.callback(
            message={
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "uid-002",
                            "name": "git_commit",
                            "input": {"message": "Add file"},
                        }
                    }
                ],
            }
        )

        # Exact shape for user toolResult (verified via probe):
        # ToolResultMessageEvent({"message": {"role": "user", "content": [{"toolResult": {...}}]}})
        user_message = {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "uid-002",
                        "status": "success",
                        "content": [{"text": "Committed."}],
                    }
                }
            ],
        }

        async with event_bus.subscribe("p", "e2") as q:
            # Drain ToolCallEvent that was already published before subscribe.
            import contextlib

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.1)

            translator.callback(message=user_message)
            try:
                event = await asyncio.wait_for(q.get(), timeout=0.5)
                received.append(event)
            except TimeoutError:
                pass

        assert len(received) == 1
        assert isinstance(received[0], ToolResultEvent)
        assert received[0].tool_use_id == "uid-002"
        assert received[0].tool_name == "git_commit"
        assert received[0].result == "Committed."

    async def test_tool_use_stream_ignored(self) -> None:
        """tool_use_stream kwargs (partial JSON input) must NOT publish any event.

        Strands fires this once with a str input; the real complete tool data
        arrives later via the message kwargs. We must not react to this event.
        """
        from yukar.agents.streaming import StreamTranslator
        from yukar.events import bus as event_bus

        translator = StreamTranslator(project_id="p", epic_id="e3", run_id="r", thread_id="t")

        async with event_bus.subscribe("p", "e3") as q:
            # This is what Strands actually sends — input is a str (partial JSON).
            translator.callback(
                **{
                    "type": "tool_use_stream",
                    "delta": {"toolUse": {"input": '{"path":'}},
                    "current_tool_use": {
                        "toolUseId": "uid-partial",
                        "name": "fs_read",
                        "input": '{"path": "x.py"}',  # str, not dict
                    },
                }
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.1)

    async def test_unknown_events_ignored(self) -> None:
        from yukar.agents.streaming import StreamTranslator
        from yukar.events import bus as event_bus

        translator = StreamTranslator(project_id="p", epic_id="e", run_id="r", thread_id="t")

        async with event_bus.subscribe("p", "e") as q:
            # init_event_loop event — should be ignored.
            translator.callback(init_event_loop=True)
            translator.callback(result={"stop_reason": "end_turn"})
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.1)

    async def test_deduplication_prevents_double_publish(self) -> None:
        """Sending the same toolUseId twice must not publish duplicate events."""
        from yukar.agents.streaming import StreamTranslator
        from yukar.events import bus as event_bus
        from yukar.models.events import ToolCallEvent

        translator = StreamTranslator(project_id="p", epic_id="e4", run_id="r", thread_id="t")

        assistant_message = {
            "role": "assistant",
            "content": [
                {
                    "toolUse": {
                        "toolUseId": "uid-dup",
                        "name": "fs_read",
                        "input": {"path": "a.py"},
                    }
                }
            ],
        }

        received: list[Any] = []

        async with event_bus.subscribe("p", "e4") as q:
            translator.callback(message=assistant_message)
            translator.callback(message=assistant_message)  # duplicate
            # Drain up to 2 events with short timeout.
            for _ in range(2):
                import contextlib

                with contextlib.suppress(TimeoutError):
                    ev = await asyncio.wait_for(q.get(), timeout=0.1)
                    received.append(ev)

        assert len(received) == 1, "Duplicate toolUseId should not produce two events"
        assert isinstance(received[0], ToolCallEvent)

    async def test_real_stream_async_publishes_tool_events(self) -> None:
        """Integration: real Agent.stream_async + FakeModel + real tool → bus events.

        This is the canonical regression test that caught the original bug:
        ToolCallEvent and ToolResultEvent must appear on the bus with correct
        tool_use_id, tool_name, and tool_input (complete dict, not partial JSON).
        """
        from strands import Agent
        from strands import tool as strands_tool

        from yukar.agents.streaming import StreamTranslator
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.models.events import ToolCallEvent, ToolResultEvent

        @strands_tool
        def probe_tool(value: str) -> str:
            """A probe tool for testing."""
            return f"got_{value}"

        model = FakeModel(
            script=[
                ToolUseTurn(
                    tool_name="probe_tool",
                    tool_input={"value": "test123"},
                    tool_use_id="real-uid-001",
                ),
                TextTurn("Done."),
            ]
        )

        translator = StreamTranslator(project_id="rp", epic_id="re", run_id="rr", thread_id="rt")
        agent = Agent(
            model=model,
            tools=[probe_tool],
            callback_handler=translator.callback,
        )

        tool_calls: list[ToolCallEvent] = []
        tool_results: list[ToolResultEvent] = []

        async with event_bus.subscribe("rp", "re") as q:
            # Run agent in background; collect events concurrently.
            async def _run() -> None:
                async for _ in agent.stream_async("run probe_tool"):
                    pass

            run_task = asyncio.create_task(_run())

            # Collect events until run_task completes + short drain.
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                if run_task.done() and q.empty():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=0.1)
                    if isinstance(ev, ToolCallEvent):
                        tool_calls.append(ev)
                    elif isinstance(ev, ToolResultEvent):
                        tool_results.append(ev)
                except TimeoutError:
                    if run_task.done():
                        break

            await run_task  # re-raise any exception

        assert len(tool_calls) >= 1, "Expected at least one ToolCallEvent on the bus"
        call = tool_calls[0]
        assert call.tool_name == "probe_tool"
        assert call.tool_input == {"value": "test123"}
        assert call.tool_use_id == "real-uid-001"
        assert call.thread_id == "rt"

        assert len(tool_results) >= 1, "Expected at least one ToolResultEvent on the bus"
        result = tool_results[0]
        assert result.tool_name == "probe_tool"
        assert result.tool_use_id == "real-uid-001"
        assert "got_test123" in result.result


class TestAgentUsageRecorder:
    """Unit tests for early callback-based agent usage recording."""

    @staticmethod
    def _make_agent(callback: Any) -> Any:
        from types import SimpleNamespace

        class Model:
            def get_config(self) -> dict[str, str]:
                return {"model_id": "test-model"}

        return SimpleNamespace(
            model=Model(),
            callback_handler=callback,
            event_loop_metrics=SimpleNamespace(accumulated_usage={}),
        )

    async def test_records_each_assistant_message_early_without_double_counting(self) -> None:
        from unittest.mock import MagicMock, patch

        from yukar.agents.streaming import AgentUsageRecorder

        recorded: list[Any] = []
        first_started = asyncio.Event()
        release = asyncio.Event()

        class Tracker:
            async def record(self, **kwargs: Any) -> None:
                recorded.append(kwargs)
                first_started.set()
                await release.wait()

            def is_over_budget(self) -> bool:
                return False

        wrapped = MagicMock()
        agent = self._make_agent(wrapped)
        recorder = AgentUsageRecorder(
            project_id="p",
            epic_id="e",
            run_id="r",
            role="manager",
        ).bind(agent)

        with patch("yukar.usage.tracker.get_tracker", return_value=Tracker()):
            agent.event_loop_metrics.accumulated_usage = {
                "inputTokens": 10,
                "outputTokens": 2,
                "cacheReadInputTokens": 3,
            }
            agent.callback_handler(message={"role": "assistant", "content": []})
            await asyncio.wait_for(first_started.wait(), timeout=0.5)

            assert recorder.pending_count == 1
            assert len(recorded) == 1

            # Repeated assistant callback with the same snapshot must not record.
            agent.callback_handler(message={"role": "assistant", "content": []})
            agent.callback_handler(message={"role": "user", "content": []})

            agent.event_loop_metrics.accumulated_usage = {
                "inputTokens": 17,
                "outputTokens": 7,
                "cacheReadInputTokens": 4,
                "cacheWriteInputTokens": 6,
            }
            agent.callback_handler(message={"role": "assistant", "content": []})

            release.set()
            await recorder.flush()

        assert recorder.pending_count == 0
        assert wrapped.call_count == 4
        assert len(recorded) == 2
        first, second = (call["delta"] for call in recorded)
        assert (
            first.input_tokens,
            first.output_tokens,
            first.cache_read_tokens,
            first.cache_write_tokens,
        ) == (10, 2, 3, 0)
        assert (
            second.input_tokens,
            second.output_tokens,
            second.cache_read_tokens,
            second.cache_write_tokens,
        ) == (7, 5, 1, 6)

    async def test_record_failure_is_collected_and_does_not_escape_flush(self) -> None:
        from unittest.mock import patch

        from yukar.agents.streaming import AgentUsageRecorder

        class Tracker:
            async def record(self, **kwargs: Any) -> None:
                raise RuntimeError("ledger unavailable")

            def is_over_budget(self) -> bool:
                return False

        agent = self._make_agent(lambda **kwargs: None)
        recorder = AgentUsageRecorder(
            project_id="p",
            epic_id="e",
            run_id="r",
            role="worker",
        ).bind(agent)

        with patch("yukar.usage.tracker.get_tracker", return_value=Tracker()):
            agent.event_loop_metrics.accumulated_usage = {"inputTokens": 1}
            agent.callback_handler(message={"role": "assistant", "content": []})
            await recorder.flush()

        assert recorder.pending_count == 0

    async def test_cancelled_record_task_is_collected_and_removed(self) -> None:
        from unittest.mock import patch

        from yukar.agents.streaming import AgentUsageRecorder

        started = asyncio.Event()

        class Tracker:
            async def record(self, **kwargs: Any) -> None:
                started.set()
                await asyncio.Event().wait()

            def is_over_budget(self) -> bool:
                return False

        agent = self._make_agent(lambda **kwargs: None)
        recorder = AgentUsageRecorder(
            project_id="p",
            epic_id="e",
            run_id="r",
            role="worker",
        ).bind(agent)

        with patch("yukar.usage.tracker.get_tracker", return_value=Tracker()):
            agent.event_loop_metrics.accumulated_usage = {"inputTokens": 1}
            agent.callback_handler(message={"role": "assistant", "content": []})
            await asyncio.wait_for(started.wait(), timeout=0.5)
            task = next(iter(recorder._pending))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            for _ in range(10):
                if recorder.pending_count == 0:
                    break
                await asyncio.sleep(0)

        assert recorder.pending_count == 0

    async def test_flush_does_not_deadlock_when_budget_stop_waits_for_current_run(self) -> None:
        from unittest.mock import patch

        from yukar.agents.streaming import AgentUsageRecorder

        stop_started = asyncio.Event()
        run_finished = asyncio.Event()

        class Tracker:
            over_budget = False

            async def record(self, **kwargs: Any) -> None:
                self.over_budget = True
                stop_started.set()
                # Simulates supervisor.stop(current run) waiting for this run.
                await run_finished.wait()

            def is_over_budget(self) -> bool:
                return self.over_budget

        tracker = Tracker()
        agent = self._make_agent(lambda **kwargs: None)
        recorder = AgentUsageRecorder(
            project_id="p",
            epic_id="e",
            run_id="r",
            role="worker",
        ).bind(agent)

        with patch("yukar.usage.tracker.get_tracker", return_value=tracker):
            agent.event_loop_metrics.accumulated_usage = {"inputTokens": 1}
            agent.callback_handler(message={"role": "assistant", "content": []})
            await asyncio.wait_for(stop_started.wait(), timeout=0.5)

            await asyncio.wait_for(recorder.flush(), timeout=0.5)
            assert recorder.pending_count == 1

            run_finished.set()
            for _ in range(10):
                if recorder.pending_count == 0:
                    break
                await asyncio.sleep(0)

        assert recorder.pending_count == 0


async def test_resolve_agent_records_usage_via_callback(tmp_path: Path) -> None:
    """ResolveRunner binds the same early usage recorder as regular workers."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from yukar.config.settings import LLMSettings
    from yukar.runs.resolve_runner import ResolveRunner

    recorded: list[Any] = []

    class Tracker:
        async def record(self, **kwargs: Any) -> None:
            recorded.append(kwargs)

        def is_over_budget(self) -> bool:
            return False

    class Model:
        def get_config(self) -> dict[str, str]:
            return {"model_id": "resolve-model"}

    class FakeAgent:
        def __init__(self, *, model: Any, callback_handler: Any, **kwargs: Any) -> None:
            self.model = model
            self.callback_handler = callback_handler
            self.event_loop_metrics = SimpleNamespace(accumulated_usage={})
            self.messages = [{"role": "assistant", "content": [{"text": "resolved"}]}]

        async def stream_async(self, prompt: str) -> Any:
            self.event_loop_metrics.accumulated_usage = {
                "inputTokens": 11,
                "outputTokens": 4,
            }
            self.callback_handler(message={"role": "assistant", "content": []})
            yield {}

    ctx: Any = SimpleNamespace(
        worktree_path=tmp_path,
        workspace_root=str(tmp_path),
    )
    runner = ResolveRunner(LLMSettings(provider="fake"), repo_name="repo")

    with (
        patch("yukar.runs.common.Agent", FakeAgent),
        patch("yukar.runs.resolve_runner.create_model", return_value=Model()),
        patch("yukar.runs.common.make_fs_tools", return_value=[]),
        patch("yukar.runs.common.make_command_tools", return_value=[]),
        patch("yukar.runs.common.make_git_tools", return_value=[]),
        patch("yukar.runs.common.session_store.append_message", new=AsyncMock()),
        patch("yukar.usage.tracker.get_tracker", return_value=Tracker()),
    ):
        await runner._run_resolve_agent(
            project_id="p",
            epic_id="e",
            run_id="r",
            resolver_id="resolver",
            conflict_files=["conflicted.py"],
            ctx=ctx,
        )

    assert len(recorded) == 1
    assert recorded[0]["role"] == "worker"
    assert recorded[0]["model_id"] == "resolve-model"
    assert recorded[0]["delta"].input_tokens == 11
    assert recorded[0]["delta"].output_tokens == 4


# ---------------------------------------------------------------------------
# EpicOrchestrator E2E tests (FakeModel)
# ---------------------------------------------------------------------------


class TestEpicOrchestrator:
    """E2E tests for the orchestrator using FakeModel scripts.

    Architecture note
    -----------------
    In the Agent-as-a-Tool design the Manager Agent itself calls ``dispatch``
    as a tool call.  Worker/Evaluator scripts are consumed *inside* the
    dispatch host implementation.  Therefore:

    - ``script_manager`` must include ``ToolUseTurn(tool_name="dispatch", ...)``
      in addition to ``task_update`` calls; its final TextTurn ends the turn
      and the run parks in ``waiting`` (P3: no completion tool — a
      conversation has no end).
    - ``script_worker`` / ``script_evaluator`` are injected via ``fake_create_model``
      and consumed when the host runs ``_run_one_attempt`` during dispatch.
    """

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def _run_orchestrator(
        self,
        root: str,
        project_id: str,
        epic_id: str,
        script_manager: list[Any],
        script_worker: list[Any],
        script_evaluator: list[Any],
        repo_name: str = "myrepo",
        fake_create_model_override: Any = None,
    ) -> list[Any]:
        """Run the orchestrator with FakeModel scripts and collect bus events.

        Args:
            script_manager: Script for the Manager Agent (must include dispatch
                tool calls to drive the orchestration; the final TextTurn parks
                the run).
            script_worker: Script given to each Worker Agent (consumed inside dispatch).
            script_evaluator: Script given to each Evaluator Agent (consumed inside dispatch).
            repo_name: Repo name used by default worker script.
            fake_create_model_override: If provided, overrides the default
                ``fake_create_model`` factory entirely (used by tests with custom
                call-count logic).
        """
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel

        llm = LLMSettings(provider="fake")

        # We need to intercept create_model calls to inject our scripts.
        # Manager script is replayed fresh every Manager turn (same script from turn 0).
        # Worker/Evaluator scripts each get a *new* FakeModel per invocation so the
        # script cursor starts at turn 0 each time.
        call_counts: dict[str, int] = {"manager": 0, "worker": 0, "evaluator": 0}

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            r = role or "worker"
            count = call_counts.get(r, 0)
            call_counts[r] = count + 1
            if r == "manager":
                return FakeModel(script=list(script_manager))
            if r == "worker":
                return FakeModel(script=list(script_worker))
            # evaluator
            return FakeModel(script=list(script_evaluator))

        model_factory = fake_create_model_override or fake_create_model

        events_received: list[Any] = []

        async def _collect() -> None:
            async for ev in event_bus.event_stream(project_id, epic_id):
                events_received.append(ev)

        collector = asyncio.create_task(_collect())
        # Yield to the event loop so the collector task can subscribe before
        # the orchestrator publishes its first event (run_started).
        await asyncio.sleep(0)

        with patch("yukar.agents.orchestrator.create_model", side_effect=model_factory):
            orch = EpicOrchestrator(
                llm_settings=llm,
                git_author_name="yukar",
                git_author_email="yukar@localhost",
                # These scripted-Manager runs pre-date the plan-approval gate and
                # dispatch directly without a simulated user approval; keep the
                # gate off here so they exercise dispatch mechanics. The gate
                # itself is covered by dedicated tests in test_ask_user_gate.py.
                require_plan_approval=False,
            )
            run_id = "test-run"
            # P3: the run parks in ``waiting`` when the scripted turn ends —
            # it never completes on its own.  Wait for the park, then stop.
            await run_until_parked(orch, root, project_id, epic_id, run_id)

        # sentinel already published by orchestrator, wait for collector
        await asyncio.wait_for(collector, timeout=5.0)
        return events_received

    async def test_minimal_run_completes(self, git_repo: Path, tmp_path: Path) -> None:
        """Manager creates 1 task → dispatch → Worker commits → Evaluator accepts."""
        from yukar.llm.fake import TextTurn, ToolUseTurn

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        # Manager: plan T1, dispatch it, then report in the message body and
        # end the turn (the run parks in waiting — there is no completion tool).
        # The FakeModel replays these as scripted tool calls.
        manager_script = [
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
                tool_name="dispatch",
                tool_input={
                    "items": [{"task_id": "T1", "repo": git_repo.name}],
                },
            ),
            TextTurn("Epic complete."),
        ]

        # Worker: write a file (host stages and commits after Evaluator accepts).
        worker_script = [
            ToolUseTurn(
                tool_name="fs_write",
                tool_input={"path": "hello.py", "content": "print('hello')\n"},
            ),
            TextTurn("Implemented hello.py."),
        ]

        # Evaluator: read diff and accept
        evaluator_script = [
            ToolUseTurn(tool_name="read_diff", tool_input={"staged": False}),
            ToolUseTurn(
                tool_name="submit_verdict",
                tool_input={"accepted": True, "feedback": ""},
            ),
            TextTurn("Accepted."),
        ]

        events = await self._run_orchestrator(
            root,
            project_id,
            epic_id,
            script_manager=manager_script,
            script_worker=worker_script,
            script_evaluator=evaluator_script,
            repo_name=git_repo.name,
        )

        event_types = [getattr(ev, "type", None) for ev in events]

        # Verify key events were published
        assert "run_started" in event_types
        assert "task_update" in event_types
        assert "worker_started" in event_types
        assert "worker_completed" in event_types
        assert "eval_result" in event_types
        # P3: the ended turn parks the run (your-turn signal); a conversation
        # run never emits run_completed.
        assert "your_turn" in event_types
        assert "run_completed" not in event_types

        # Verify eval result accepted
        eval_ev = next(e for e in events if getattr(e, "type", None) == "eval_result")
        assert eval_ev.accepted is True

        # Verify tool_call and tool_result events were published during the worker run.
        # This is the key regression guard for the StreamTranslator bug: tool events
        # must flow through the bus when the agent calls real tools.
        from yukar.models.events import ToolCallEvent, ToolResultEvent

        tool_call_events = [e for e in events if isinstance(e, ToolCallEvent)]
        tool_result_events = [e for e in events if isinstance(e, ToolResultEvent)]

        assert tool_call_events, (
            "Expected ToolCallEvents on bus during worker run (StreamTranslator bug regression)"
        )
        assert tool_result_events, (
            "Expected ToolResultEvents on bus during worker run (StreamTranslator bug regression)"
        )

        # Each tool_call and tool_result should have matching tool_use_ids.
        call_ids = {e.tool_use_id for e in tool_call_events if e.tool_use_id}
        result_ids = {e.tool_use_id for e in tool_result_events if e.tool_use_id}
        assert call_ids & result_ids, (
            "At least one tool_use_id should be shared between ToolCallEvent and ToolResultEvent"
        )

        # tool_input on ToolCallEvent must be a complete dict (not partial JSON str).
        for ev in tool_call_events:
            assert isinstance(ev.tool_input, dict), (
                f"tool_input must be a dict, got {type(ev.tool_input).__name__!r}"
            )

    async def test_manager_effort_passed_to_create_model(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """The Epic's ``manager_effort`` is forwarded to ``create_model`` for the
        Manager only; worker/evaluator calls receive no effort.

        Regression guard for the Manager-effort wiring (orchestrator → factory).
        """
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage.epic_repo import get_epic, save_epic

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        # Set a non-default effort on the epic and persist it.
        epic = await get_epic(root, project_id, epic_id)
        assert epic is not None
        epic.manager_effort = "max"
        await save_epic(root, project_id, epic)

        manager_script = [
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
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            TextTurn("Epic complete."),
        ]
        worker_script = [
            ToolUseTurn(tool_name="fs_write", tool_input={"path": "hello.py", "content": "x\n"}),
            TextTurn("done"),
        ]
        evaluator_script = [
            ToolUseTurn(tool_name="read_diff", tool_input={"staged": False}),
            ToolUseTurn(tool_name="submit_verdict", tool_input={"accepted": True, "feedback": ""}),
            TextTurn("Accepted."),
        ]

        # Record the (role, effort) pair for every create_model call.
        recorded: list[tuple[Any, Any]] = []

        def recording_factory(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            recorded.append((role, kwargs.get("effort")))
            r = role or "worker"
            if r == "manager":
                return FakeModel(script=list(manager_script))
            if r == "worker":
                return FakeModel(script=list(worker_script))
            return FakeModel(script=list(evaluator_script))

        await self._run_orchestrator(
            root,
            project_id,
            epic_id,
            script_manager=manager_script,
            script_worker=worker_script,
            script_evaluator=evaluator_script,
            repo_name=git_repo.name,
            fake_create_model_override=recording_factory,
        )

        manager_calls = [eff for (role, eff) in recorded if role == "manager"]
        assert manager_calls, "expected at least one manager create_model call"
        assert all(eff == "max" for eff in manager_calls), recorded

        # Worker/evaluator must not receive an effort value.
        non_manager = [eff for (role, eff) in recorded if role != "manager"]
        assert all(eff is None for eff in non_manager), recorded

    async def test_worktree_created_and_commit_lands(self, git_repo: Path, tmp_path: Path) -> None:
        """Verify the worktree is created and there is an actual commit."""
        from yukar.config import paths
        from yukar.llm.fake import TextTurn, ToolUseTurn

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Add file",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            TextTurn("Done."),
        ]
        worker_script = [
            ToolUseTurn(
                tool_name="fs_write",
                tool_input={"path": "newfile.txt", "content": "hello\n"},
            ),
            TextTurn("Wrote newfile.txt."),
        ]
        evaluator_script = [
            ToolUseTurn(
                tool_name="submit_verdict",
                tool_input={"accepted": True, "feedback": ""},
            ),
            TextTurn("Accepted."),
        ]

        await self._run_orchestrator(
            root,
            project_id,
            epic_id,
            script_manager=manager_script,
            script_worker=worker_script,
            script_evaluator=evaluator_script,
            repo_name=git_repo.name,
        )

        # Worktree directory should exist.
        worktree_path = paths.worktree_dir(root, project_id, epic_id, "manager", git_repo.name)
        assert worktree_path.exists(), "Worktree should be created"

        # The file should exist in the worktree.
        assert (worktree_path / "newfile.txt").exists()

        # Host commits on behalf of the Worker after Evaluator acceptance.
        # Commit subject is "<task_id>: <task_title>" (issue④).
        result = subprocess.run(
            ["git", "log", "--oneline", "-3"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
        )
        assert "T1: Add file" in result.stdout

    async def test_epic_touched_repos_updated(self, git_repo: Path, tmp_path: Path) -> None:
        """Verify epic.touched_repos is updated when worktree is created."""
        from yukar.llm.fake import TextTurn, ToolUseTurn
        from yukar.storage.epic_repo import get_epic

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Trivial task",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            TextTurn("Done."),
        ]
        worker_script = [
            ToolUseTurn(
                tool_name="fs_write",
                tool_input={"path": "x.py", "content": "# x\n"},
            ),
            TextTurn("Done."),
        ]
        evaluator_script = [
            ToolUseTurn(
                tool_name="submit_verdict",
                tool_input={"accepted": True, "feedback": ""},
            ),
            TextTurn("Accepted."),
        ]

        await self._run_orchestrator(
            root,
            project_id,
            epic_id,
            script_manager=manager_script,
            script_worker=worker_script,
            script_evaluator=evaluator_script,
            repo_name=git_repo.name,
        )

        epic = await get_epic(root, project_id, epic_id)
        assert epic is not None
        assert git_repo.name in epic.touched_repos

    async def test_tasks_yaml_and_state_yaml_consistent(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """After the scripted turn, tasks.yaml has a done task and state is waiting."""
        from yukar.llm.fake import TextTurn, ToolUseTurn
        from yukar.storage import state_repo, tasks_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Write file",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            TextTurn("Done."),
        ]
        worker_script = [
            ToolUseTurn(
                tool_name="fs_write",
                tool_input={"path": "a.py", "content": "a = 1\n"},
            ),
            TextTurn("Done."),
        ]
        evaluator_script = [
            ToolUseTurn(
                tool_name="submit_verdict",
                tool_input={"accepted": True, "feedback": ""},
            ),
            TextTurn("Accepted."),
        ]

        await self._run_orchestrator(
            root,
            project_id,
            epic_id,
            script_manager=manager_script,
            script_worker=worker_script,
            script_evaluator=evaluator_script,
            repo_name=git_repo.name,
        )

        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        assert len(tf.tasks) == 1
        assert tf.tasks[0].status == "done"
        assert tf.progress.done == 1

        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None
        assert state.status == "waiting"

    async def test_retry_on_needs_fix(self, git_repo: Path, tmp_path: Path) -> None:
        """Manager dispatches T1, evaluator rejects, Manager retries with feedback, accepted.

        In Agent-as-a-Tool the Manager drives retry by calling dispatch again
        with the feedback from the first rejected attempt.  The host enforces
        the attempt limit; the Manager decides whether to retry or give up.
        """
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        # Manager calls task_update → dispatch(T1) [gets rejected] →
        # dispatch(T1 with feedback) [accepted] → reports and ends the turn.
        # FakeModel replays these as scripted tool calls in order.
        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Write file",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            # First dispatch — will be rejected by evaluator.
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            # Second dispatch with feedback — will be accepted.
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={
                    "items": [
                        {
                            "task_id": "T1",
                            "repo": git_repo.name,
                            "feedback": "Needs improvement.",
                        }
                    ]
                },
            ),
            TextTurn("Done."),
        ]

        llm = LLMSettings(provider="fake")

        # Track call count per role to alternate evaluator behavior.
        call_counts: dict[str, int] = {"manager": 0, "worker": 0, "evaluator": 0}

        eval_reject_script = [
            ToolUseTurn(
                tool_name="submit_verdict",
                tool_input={"accepted": False, "feedback": "Needs improvement."},
            ),
            TextTurn("Rejected."),
        ]
        eval_accept_script = [
            ToolUseTurn(
                tool_name="submit_verdict",
                tool_input={"accepted": True, "feedback": ""},
            ),
            TextTurn("Accepted."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            r = role or "worker"
            count = call_counts.get(r, 0)
            call_counts[r] = count + 1
            if r == "manager":
                return FakeModel(script=list(manager_script))
            if r == "worker":
                return FakeModel(
                    script=[
                        ToolUseTurn(
                            tool_name="fs_write",
                            tool_input={"path": "b.py", "content": f"b = {count + 1}\n"},
                        ),
                        TextTurn(f"Done attempt {count + 1}."),
                    ]
                )
            # evaluator: first call rejects, second accepts.
            if count == 0:
                return FakeModel(script=list(eval_reject_script))
            return FakeModel(script=list(eval_accept_script))

        events_received: list[Any] = []

        async def _collect() -> None:
            async for ev in event_bus.event_stream(project_id, epic_id):
                events_received.append(ev)

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            orch = EpicOrchestrator(
                llm_settings=llm,
                git_author_name="yukar",
                git_author_email="yukar@localhost",
                require_plan_approval=False,
            )
            await run_until_parked(orch, root, project_id, epic_id, "run-retry-test")

        await asyncio.wait_for(collector, timeout=5.0)

        # There should be 2 eval_result events: first rejected, second accepted.
        eval_events = [e for e in events_received if getattr(e, "type", None) == "eval_result"]
        assert len(eval_events) == 2
        assert eval_events[0].accepted is False
        assert eval_events[1].accepted is True

    async def test_retry_limit_marks_task_blocked(self, git_repo: Path, tmp_path: Path) -> None:
        """After _MAX_ATTEMPTS_PER_TASK failed attempts, host auto-blocks the task.

        Manager keeps dispatching T1 (evaluator always rejects).  After the host
        enforces the attempt limit the task is blocked; the Manager reports in
        its message body and the run parks in waiting (no failure).
        """
        from unittest.mock import patch

        from yukar.agents.orchestrator import _MAX_ATTEMPTS_PER_TASK, EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import tasks_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        # Manager dispatches T1 _MAX_ATTEMPTS_PER_TASK times (all rejected), then
        # one final dispatch returns blocked (host limit), then ends its turn.
        # Build manager script dynamically: task_update + N dispatch calls + text.
        dispatch_item = {"task_id": "T1", "repo": git_repo.name}
        manager_turns: list[Any] = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Always fails",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
        ]
        # Dispatch _MAX_ATTEMPTS_PER_TASK times; the last call will return blocked.
        for _ in range(_MAX_ATTEMPTS_PER_TASK + 1):
            manager_turns.append(
                ToolUseTurn(
                    tool_name="dispatch",
                    tool_input={"items": [dispatch_item]},
                )
            )
        manager_turns.append(TextTurn("Done."))

        # Evaluator always rejects.
        always_reject = [
            ToolUseTurn(
                tool_name="submit_verdict",
                tool_input={"accepted": False, "feedback": "Still wrong."},
            ),
            TextTurn("Rejected."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            r = role or "worker"
            if r == "manager":
                return FakeModel(script=list(manager_turns))
            if r == "worker":
                return FakeModel(
                    script=[
                        ToolUseTurn(
                            tool_name="fs_write",
                            tool_input={"path": "bad.py", "content": "bad\n"},
                        ),
                        TextTurn("Done."),
                    ]
                )
            return FakeModel(script=list(always_reject))

        events_received: list[Any] = []

        async def _collect() -> None:
            async for ev in event_bus.event_stream(project_id, epic_id):
                events_received.append(ev)

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            orch = EpicOrchestrator(
                llm_settings=LLMSettings(provider="fake"),
                git_author_name="yukar",
                git_author_email="yukar@localhost",
                require_plan_approval=False,
            )
            await run_until_parked(orch, root, project_id, epic_id, "run-blocked")

        await asyncio.wait_for(collector, timeout=10.0)

        # Should have exactly _MAX_ATTEMPTS_PER_TASK eval results, all rejected.
        eval_events = [e for e in events_received if getattr(e, "type", None) == "eval_result"]
        assert len(eval_events) == _MAX_ATTEMPTS_PER_TASK
        assert all(not e.accepted for e in eval_events)

        # Task should be blocked.
        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        assert tf.tasks[0].status == "blocked"

        # The run parks normally (attempt exhaustion is not a run failure).
        assert not any(getattr(e, "type", None) == "run_failed" for e in events_received)
        assert any(getattr(e, "type", None) == "your_turn" for e in events_received)

    async def test_stop_updates_state(self, git_repo: Path, tmp_path: Path) -> None:
        """Stopping an orchestrator mid-run sets state.yaml to waiting (not error).

        CancelledError is always caused by an explicit supervisor.stop() call
        (user-initiated interrupt) — the run can be resumed by starting a new
        run.  Internal errors continue to produce state.status=error.
        """
        import asyncio
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import state_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        # Manager calls task_update → dispatch (which starts Worker) — we cancel before it finishes.
        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Long task",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            TextTurn("Plan done."),
        ]
        # Worker just returns text without committing.
        worker_script = [TextTurn("Working…")]
        # Evaluator accepts trivially.
        eval_script = [
            ToolUseTurn(
                tool_name="submit_verdict",
                tool_input={"accepted": True, "feedback": ""},
            ),
            TextTurn("OK."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            r = role or "worker"
            if r == "manager":
                return FakeModel(script=list(manager_script))
            if r == "worker":
                return FakeModel(script=list(worker_script))
            return FakeModel(script=list(eval_script))

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="yukar",
            git_author_email="yukar@localhost",
            require_plan_approval=False,
        )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            task = asyncio.create_task(orch.start(root, project_id, epic_id, "run-stop-test"))
            # Give it a moment to start.
            await asyncio.sleep(0.05)
            # Real supervisor.stop() sets _stopped=True before force-cancel; a
            # shutdown cancel leaves it False (preserves state.yaml) — see
            # orchestrator CancelledError handler.
            orch._stopped = True
            task.cancel()
            with pytest.raises((asyncio.CancelledError, Exception)):
                await task

        # CancelledError (stop) → state.status must be waiting, NOT error.
        # epic.yaml is user-owned and untouched by the stop.
        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None
        assert state.status == "waiting", (
            f"Expected waiting after stop, got {state.status!r}. "
            "stop is a user-initiated interrupt, not an error."
        )
        assert state.active_workers == []

    async def test_stop_publishes_run_stopped_event(self, git_repo: Path, tmp_path: Path) -> None:
        """Stopping an orchestrator mid-run publishes RunStoppedEvent on the bus.

        The event must arrive before the None sentinel (which closes the stream),
        and must carry the correct project_id / epic_id / run_id.

        This is a regression test for the publish-order fix: pub(RunStoppedEvent)
        must be called BEFORE await save_state so that a second CancelledError
        inside save_state cannot suppress the event.
        """
        import asyncio
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn

        run_id = "run-stop-event-test"
        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Long task",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            TextTurn("Plan done."),
        ]
        worker_script = [TextTurn("Working…")]
        eval_script = [
            ToolUseTurn(
                tool_name="submit_verdict",
                tool_input={"accepted": True, "feedback": ""},
            ),
            TextTurn("OK."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            r = role or "worker"
            if r == "manager":
                return FakeModel(script=list(manager_script))
            if r == "worker":
                return FakeModel(script=list(worker_script))
            return FakeModel(script=list(eval_script))

        # Subscribe to the event bus before starting the run.
        events_received: list[Any] = []

        async def _collect() -> None:
            async for ev in event_bus.event_stream(project_id, epic_id):
                events_received.append(ev)

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="yukar",
            git_author_email="yukar@localhost",
            require_plan_approval=False,
        )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            task = asyncio.create_task(orch.start(root, project_id, epic_id, run_id))
            # Give the orchestrator a moment to start running.
            await asyncio.sleep(0.05)
            # Real supervisor.stop() sets _stopped=True before force-cancel.
            orch._stopped = True
            task.cancel()
            with pytest.raises((asyncio.CancelledError, Exception)):
                await task

        # Collector terminates when the None sentinel is published (finally block).
        await asyncio.wait_for(collector, timeout=5.0)

        # RunStoppedEvent must have been published.
        stopped_events = [e for e in events_received if getattr(e, "type", None) == "run_stopped"]
        received_types = [getattr(e, "type", type(e).__name__) for e in events_received]
        assert stopped_events, (
            "Expected at least one RunStoppedEvent after stop, got none. "
            f"Event types received: {received_types}"
        )

        ev = stopped_events[0]
        assert ev.project_id == project_id
        assert ev.epic_id == epic_id
        assert ev.run_id == run_id

        # RunStoppedEvent must arrive before the None sentinel (i.e. before the
        # collector task exits).  Since the collector only terminates on None
        # and we already asserted it finished, any event in events_received
        # implicitly arrived before the sentinel.  Additionally verify ordering
        # relative to run_started so the lifecycle sequence is sane.
        event_types = [getattr(e, "type", None) for e in events_received]
        assert "run_started" in event_types, "run_started should precede run_stopped"
        started_idx = event_types.index("run_started")
        stopped_idx = event_types.index("run_stopped")
        assert started_idx < stopped_idx, (
            f"run_started (idx={started_idx}) must precede run_stopped (idx={stopped_idx})"
        )

    async def test_hitl_injection_reaches_orchestrator(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """inject_message queues messages; they can be drained."""
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="yukar",
            git_author_email="yukar@localhost",
            require_plan_approval=False,
        )

        orch.inject_message("th-abc", "Hello agent!")
        orch.inject_message("manager", "Please revise.")

        drained = orch._drain_pending()
        assert ("th-abc", "Hello agent!") in drained
        assert ("manager", "Please revise.") in drained
        assert len(drained) == 2


# ---------------------------------------------------------------------------
# Recovery tests
# ---------------------------------------------------------------------------


class TestRecovery:
    async def test_running_state_reconciled_to_waiting(self, tmp_path: Path) -> None:
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.run import RunState
        from yukar.runs.recovery import recover_interrupted_runs
        from yukar.storage import state_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-1"

        # Create minimal project structure.
        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(
            root,
            project_id,
            Epic(id=epic_id, slug="test", title="Test"),
        )

        # Write a running state.
        state = RunState(run_id="run-abc", status="running")
        await state_repo.save_state(root, project_id, epic_id, state)

        count = await recover_interrupted_runs(root)
        assert count == 1

        reconciled = await state_repo.get_state(root, project_id, epic_id)
        assert reconciled is not None
        assert reconciled.status == "waiting"
        assert reconciled.active_workers == []

    async def test_paused_state_reconciled_to_waiting(self, tmp_path: Path) -> None:
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.run import RunState
        from yukar.runs.recovery import recover_interrupted_runs
        from yukar.storage import state_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-2"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="test2", title="Test2"))

        state = RunState(run_id="run-xyz", status="paused")
        await state_repo.save_state(root, project_id, epic_id, state)

        count = await recover_interrupted_runs(root)
        assert count == 1

        reconciled = await state_repo.get_state(root, project_id, epic_id)
        assert reconciled is not None
        assert reconciled.status == "waiting"

    async def test_completed_state_not_reconciled(self, tmp_path: Path) -> None:
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.run import RunState
        from yukar.runs.recovery import recover_interrupted_runs
        from yukar.storage import state_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-3"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="test3", title="Test3"))

        state = RunState(run_id="run-comp", status="completed")
        await state_repo.save_state(root, project_id, epic_id, state)

        count = await recover_interrupted_runs(root)
        assert count == 0

        # State unchanged.
        s = await state_repo.get_state(root, project_id, epic_id)
        assert s is not None
        assert s.status == "completed"

    async def test_empty_workspace_no_error(self, tmp_path: Path) -> None:
        from yukar.runs.recovery import recover_interrupted_runs

        count = await recover_interrupted_runs(str(tmp_path / "nonexistent"))
        assert count == 0

    async def test_multiple_epics_recovered(self, tmp_path: Path) -> None:
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.run import RunState
        from yukar.runs.recovery import recover_interrupted_runs
        from yukar.storage import state_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        project_id = "proj"

        await save_project(root, Project(id=project_id, name=project_id))

        for i in range(3):
            epic_id = f"EP-{i}"
            await save_epic(root, project_id, Epic(id=epic_id, slug=f"e{i}", title=f"E{i}"))
            await state_repo.save_state(
                root,
                project_id,
                epic_id,
                RunState(run_id=f"run-{i}", status="running"),
            )

        count = await recover_interrupted_runs(root)
        assert count == 3


# ---------------------------------------------------------------------------
# Supervisor HITL tests
# ---------------------------------------------------------------------------


class TestSupervisorHITL:
    def test_inject_when_no_run_returns_false(self) -> None:
        from yukar.runs.supervisor import RunSupervisor

        sup = RunSupervisor()
        result = sup.inject_hitl_message("proj", "epic", "thread", "hello")
        assert result is False

    def test_inject_hitl_to_orchestrator(self) -> None:
        """inject_hitl_message delegates to orchestrator.inject_message."""
        from unittest.mock import MagicMock

        from yukar.runs.supervisor import RunSupervisor, _RunHandle

        sup = RunSupervisor()

        # Mock orchestrator with inject_message.
        mock_runner = MagicMock()
        mock_runner.inject_message = MagicMock()
        mock_task = MagicMock()
        mock_task.done.return_value = False

        sup._runs[("proj", "epic")] = _RunHandle(
            run_id="run-x",
            runner=mock_runner,
            task=mock_task,
            root="/tmp",
            project_id="proj",
            epic_id="epic",
        )

        result = sup.inject_hitl_message("proj", "epic", "th-abc", "hello")
        assert result is True
        mock_runner.inject_message.assert_called_once_with("th-abc", "hello")

    def test_inject_hitl_no_inject_method(self) -> None:
        """Runner without inject_message returns False gracefully."""
        from unittest.mock import MagicMock

        from yukar.runs.runner import DummyRunner
        from yukar.runs.supervisor import RunSupervisor, _RunHandle

        sup = RunSupervisor()

        mock_task = MagicMock()
        mock_task.done.return_value = False

        sup._runs[("proj", "epic")] = _RunHandle(
            run_id="run-y",
            runner=DummyRunner(),
            task=mock_task,
            root="/tmp",
            project_id="proj",
            epic_id="epic",
        )

        result = sup.inject_hitl_message("proj", "epic", "th-abc", "message")
        # DummyRunner has no inject_message → returns False.
        assert result is False


# ---------------------------------------------------------------------------
# API integration: HITL via thread POST
# ---------------------------------------------------------------------------


class TestThreadsHITLAPI:
    async def test_post_message_calls_inject_when_run_active(
        self,
        app_client: Any,
        tmp_workspace: Path,
    ) -> None:
        """POST /threads/{t}/messages on active run calls inject_hitl_message."""
        from unittest.mock import MagicMock, patch

        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage import session_store
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        project_id = "proj-api"
        epic_id = "EP-1"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="e", title="E"))

        # Manually create thread agent.
        await session_store.ensure_agent(root, project_id, epic_id, "th-001")

        # Mock supervisor to track inject call.
        mock_supervisor = MagicMock()
        mock_supervisor.inject_hitl_message = MagicMock(return_value=True)

        with patch("yukar.api.routers.threads.get_run_supervisor", return_value=mock_supervisor):
            resp = await app_client.post(
                f"/api/projects/{project_id}/epics/{epic_id}/threads/th-001/messages",
                json={"content": "Please help!", "role": "user"},
            )

        assert resp.status_code == 201
        mock_supervisor.inject_hitl_message.assert_called_once_with(
            project_id, epic_id, "th-001", "Please help!"
        )

    async def test_post_assistant_message_rejected_422(
        self,
        app_client: Any,
        tmp_workspace: Path,
    ) -> None:
        """POST with role=assistant must be rejected with 422 (FSM is the sole writer).

        Non-user roles cannot be hand-written via the threads API; FSM is the
        only writer for all agent message history.  This prevents duplicate or
        fabricated messages in the Manager's session.
        """
        from unittest.mock import MagicMock, patch

        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage import session_store
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        project_id = "proj-api2"
        epic_id = "EP-2"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="e2", title="E2"))
        await session_store.ensure_agent(root, project_id, epic_id, "th-002")

        mock_supervisor = MagicMock()
        mock_supervisor.inject_hitl_message = MagicMock()

        with patch("yukar.api.routers.threads.get_run_supervisor", return_value=mock_supervisor):
            resp = await app_client.post(
                f"/api/projects/{project_id}/epics/{epic_id}/threads/th-002/messages",
                json={"content": "Agent reply", "role": "assistant"},
            )

        assert resp.status_code == 422, resp.text
        assert "only user" in resp.json()["detail"].lower()
        mock_supervisor.inject_hitl_message.assert_not_called()


# ---------------------------------------------------------------------------
# Regression: settings change reflected in next Run (architecture.md §5 #7)
# ---------------------------------------------------------------------------


class TestSupervisorSettingsResolution:
    """Verify that RunSupervisor resolves settings at run-start time.

    Regression guard for the bug where supervisor held a snapshot of
    LLMSettings at construction time, so PUT /api/settings changes were
    invisible until server restart.
    """

    def test_make_runner_reads_current_settings(self) -> None:
        """_make_runner reads the settings_getter result at call time.

        Regression guard: supervisor must NOT cache a settings snapshot at
        construction time.  Mutating the settings object between two
        ``_make_runner()`` calls must be reflected in the second runner.
        """
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import GitSettings, LLMSettings, Settings
        from yukar.runs.supervisor import RunSupervisor

        settings = Settings(workspace_root="/tmp")
        settings.llm = LLMSettings(provider="fake")
        settings.git = GitSettings(author_name="before", author_email="a@b.com")

        sup = RunSupervisor(settings_getter=lambda: settings)

        runner1 = sup._make_runner()
        assert isinstance(runner1, EpicOrchestrator)
        assert runner1._git_author_name == "before"

        # Mutate settings — next call must see the change.
        settings.git = GitSettings(author_name="after", author_email="a@b.com")
        runner2 = sup._make_runner()
        assert isinstance(runner2, EpicOrchestrator)
        assert runner2._git_author_name == "after", (
            "supervisor snapshotted git author at construction; "
            "change was not picked up by the second _make_runner() call"
        )

    def test_make_runner_no_getter_gives_dummy(self) -> None:
        """Without a settings_getter, _make_runner falls back to DummyRunner."""
        from yukar.runs.runner import DummyRunner
        from yukar.runs.supervisor import RunSupervisor

        sup = RunSupervisor()
        runner = sup._make_runner()
        assert isinstance(runner, DummyRunner)

    def test_llm_provider_change_reflected_in_next_runner(self) -> None:
        """Changing llm.provider in settings is reflected in the next runner.

        Concretely: switching from one provider to another still produces an
        EpicOrchestrator both times, but the llm_settings object inside it
        matches the current settings value — not the value at supervisor init.
        """
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings, Settings
        from yukar.runs.supervisor import RunSupervisor

        settings = Settings(workspace_root="/tmp")
        settings.llm = LLMSettings(provider="bedrock")

        sup = RunSupervisor(settings_getter=lambda: settings)

        runner1 = sup._make_runner()
        assert isinstance(runner1, EpicOrchestrator)
        assert runner1._llm.provider == "bedrock"

        settings.llm = LLMSettings(provider="fake")
        runner2 = sup._make_runner()
        assert isinstance(runner2, EpicOrchestrator)
        assert runner2._llm.provider == "fake", (
            "supervisor did not pick up the updated llm.provider"
        )


# ---------------------------------------------------------------------------
# Thread status lifecycle (spec §4.2)
# ---------------------------------------------------------------------------


class TestThreadStatusLifecycle:
    """Verify that threads.yaml status transitions are written correctly."""

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def _run_minimal(
        self,
        root: str,
        project_id: str,
        epic_id: str,
        git_repo: Path,
        eval_accepted: bool = True,
    ) -> None:
        """Run the orchestrator through a minimal script and return."""
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn

        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Write file",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            TextTurn("Done."),
        ]
        worker_script = [
            ToolUseTurn(
                tool_name="fs_write",
                tool_input={"path": "out.py", "content": "x = 1\n"},
            ),
            TextTurn("Done."),
        ]
        eval_script = [
            ToolUseTurn(
                tool_name="submit_verdict",
                tool_input={"accepted": eval_accepted, "feedback": ""},
            ),
            TextTurn("Evaluated."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            r = role or "worker"
            if r == "manager":
                return FakeModel(script=list(manager_script))
            if r == "worker":
                return FakeModel(script=list(worker_script))
            return FakeModel(script=list(eval_script))

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            orch = EpicOrchestrator(
                llm_settings=LLMSettings(provider="fake"),
                git_author_name="yukar",
                git_author_email="yukar@localhost",
                require_plan_approval=False,
            )
            await run_until_parked(orch, root, project_id, epic_id, "run-thread-test")

    async def test_manager_thread_stays_active_after_run(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """The Manager conversation never resolves — it stays active after the
        turn parks (P3: a conversation has no end; only archived is terminal)."""
        from yukar.storage.threads_repo import get_threads

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        await self._run_minimal(root, project_id, epic_id, git_repo, eval_accepted=True)

        tf = await get_threads(root, project_id, epic_id)
        manager = next((t for t in tf.threads if t.id == "manager"), None)
        assert manager is not None, "manager thread not found in threads.yaml"
        assert manager.status == "active", f"expected active, got {manager.status}"

    async def test_worker_and_eval_threads_resolved_on_accept(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """Worker and evaluator thread statuses are resolved when task accepted."""
        from yukar.storage.threads_repo import get_threads

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        await self._run_minimal(root, project_id, epic_id, git_repo, eval_accepted=True)

        tf = await get_threads(root, project_id, epic_id)
        workers = [t for t in tf.threads if t.role == "worker"]
        evals = [t for t in tf.threads if t.role == "evaluator"]

        assert workers, "no worker threads found"
        assert evals, "no evaluator threads found"

        # All worker/eval threads should be resolved (only one attempt in this case).
        for t in workers:
            assert t.status == "resolved", f"worker {t.id} status = {t.status}"
        for t in evals:
            assert t.status == "resolved", f"evaluator {t.id} status = {t.status}"

    async def test_worker_and_eval_threads_failed_on_blocked(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """Worker and evaluator thread statuses are failed when retry limit exceeded."""
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage.threads_repo import get_threads

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        from yukar.agents.orchestrator import _MAX_ATTEMPTS_PER_TASK

        # Build manager script: task_update + N dispatch calls + text.
        dispatch_item = {"task_id": "T1", "repo": git_repo.name}
        manager_turns_blocked: list[Any] = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Always fails",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
        ]
        for _ in range(_MAX_ATTEMPTS_PER_TASK + 1):
            manager_turns_blocked.append(
                ToolUseTurn(tool_name="dispatch", tool_input={"items": [dispatch_item]})
            )
        manager_turns_blocked.append(TextTurn("Done."))

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            r = role or "worker"
            if r == "manager":
                return FakeModel(script=list(manager_turns_blocked))
            if r == "worker":
                return FakeModel(
                    script=[
                        ToolUseTurn(
                            tool_name="fs_write",
                            tool_input={"path": "bad.py", "content": "bad\n"},
                        ),
                        TextTurn("Done."),
                    ]
                )
            # evaluator always rejects
            return FakeModel(
                script=[
                    ToolUseTurn(
                        tool_name="submit_verdict",
                        tool_input={"accepted": False, "feedback": "Wrong."},
                    ),
                    TextTurn("Rejected."),
                ]
            )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            orch = EpicOrchestrator(
                llm_settings=LLMSettings(provider="fake"),
                git_author_name="yukar",
                git_author_email="yukar@localhost",
                require_plan_approval=False,
            )
            await run_until_parked(orch, root, project_id, epic_id, "run-blocked-threads")

        tf = await get_threads(root, project_id, epic_id)

        # The last worker and evaluator threads should be failed.
        workers = [t for t in tf.threads if t.role == "worker"]
        evals = [t for t in tf.threads if t.role == "evaluator"]

        assert workers, "no worker threads found"
        assert evals, "no evaluator threads found"

        # Only the LAST worker/eval are set to failed; earlier retry threads stay active
        # (they were superseded, not definitively failed from the user perspective).
        last_worker = workers[-1]
        last_eval = evals[-1]
        assert last_worker.status == "failed", f"last worker status = {last_worker.status}"
        assert last_eval.status == "failed", f"last eval status = {last_eval.status}"


# ---------------------------------------------------------------------------
# Review fix #2 — stop sets state=waiting, NOT error
# ---------------------------------------------------------------------------


class TestStopSetsWaitingState:
    """CancelledError (stop) produces state.status=waiting, internal errors produce error."""

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_stop_sets_state_waiting(self, git_repo: Path, tmp_path: Path) -> None:
        """After supervisor.stop(), state.yaml=waiting (epic.yaml untouched)."""
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import state_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Long task",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            TextTurn("Plan done."),
        ]
        worker_script = [TextTurn("Working…")]
        eval_script = [
            ToolUseTurn(
                tool_name="submit_verdict",
                tool_input={"accepted": True, "feedback": ""},
            ),
            TextTurn("OK."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> Any:
            r = role or "worker"
            if r == "manager":
                return FakeModel(script=list(manager_script))
            if r == "worker":
                return FakeModel(script=list(worker_script))
            return FakeModel(script=list(eval_script))

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="yukar",
            git_author_email="yukar@localhost",
            require_plan_approval=False,
        )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            task = asyncio.create_task(orch.start(root, project_id, epic_id, "run-idle-test"))
            await asyncio.sleep(0.05)
            # Real supervisor.stop() sets _stopped=True before force-cancel.
            orch._stopped = True
            task.cancel()
            import pytest as _pytest

            with _pytest.raises((asyncio.CancelledError, Exception)):
                await task

        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None
        assert state.status == "waiting", (
            f"Expected waiting after stop, got {state.status!r}. "
            "stop is a user-initiated interrupt, not an internal error."
        )
        assert state.active_workers == []

    async def test_internal_error_sets_state_error(self, git_repo: Path, tmp_path: Path) -> None:
        """An unhandled exception (not CancelledError) must set state=error."""
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.storage import state_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> Any:
            raise RuntimeError("Simulated internal LLM error")

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="yukar",
            git_author_email="yukar@localhost",
            require_plan_approval=False,
        )

        with (
            patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model),
            pytest.raises(RuntimeError),
        ):
            await orch.start(root, project_id, epic_id, "run-error-test")

        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None
        assert state.status == "error", (
            f"Expected error after internal exception, got {state.status!r}"
        )


# ---------------------------------------------------------------------------
# Review fix #3 — stop rolls back in_progress task to todo; recovery rolls back too
# ---------------------------------------------------------------------------


class TestTaskRollbackOnStop:
    """In-progress tasks must be rolled back to todo on stop and on recovery."""

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_stop_rolls_back_in_progress_task(self, git_repo: Path, tmp_path: Path) -> None:
        """When stop fires mid-task, the in_progress task becomes todo."""
        import asyncio
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import tasks_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Rollback test",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            TextTurn("Done."),
        ]
        # Worker takes some time so we can cancel mid-run.
        worker_script = [TextTurn("Working…")]
        eval_script = [
            ToolUseTurn(
                tool_name="submit_verdict",
                tool_input={"accepted": True, "feedback": ""},
            ),
            TextTurn("OK."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            r = role or "worker"
            if r == "manager":
                return FakeModel(script=list(manager_script))
            if r == "worker":
                return FakeModel(script=list(worker_script))
            return FakeModel(script=list(eval_script))

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="yukar",
            git_author_email="yukar@localhost",
            require_plan_approval=False,
        )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            task = asyncio.create_task(orch.start(root, project_id, epic_id, "run-rollback"))
            await asyncio.sleep(0.1)
            # Signal stop via the flag (not cancel — tests the internal path).
            await orch.stop()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=5.0)

        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        # All tasks must be either todo, done, or blocked — never in_progress.
        in_progress = [t for t in tf.tasks if t.status == "in_progress"]
        assert not in_progress, (
            f"Expected no in_progress tasks after stop, found: {[t.id for t in in_progress]}"
        )

    async def test_recovery_rolls_back_in_progress_tasks(self, tmp_path: Path) -> None:
        """Startup recovery must roll back in_progress tasks to todo."""
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.run import RunState
        from yukar.models.task import Task, TasksFile
        from yukar.runs.recovery import recover_interrupted_runs
        from yukar.storage import state_repo, tasks_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-rec"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="rec", title="Rec"))

        # Plant a running state with an in_progress task.
        state = RunState(run_id="run-crash", status="running")
        await state_repo.save_state(root, project_id, epic_id, state)

        tf = TasksFile(
            tasks=[
                Task(id="T1", title="done task", status="done"),
                Task(id="T2", title="crashed task", status="in_progress"),
            ]
        )
        await tasks_repo.save_tasks(root, project_id, epic_id, tf)

        count = await recover_interrupted_runs(root)
        assert count == 1

        # State must have settled into waiting (crash recovery — the turn died
        # with the process; the conversation is intact and it is the user's turn).
        s = await state_repo.get_state(root, project_id, epic_id)
        assert s is not None
        assert s.status == "waiting"

        # The in_progress task must have been rolled back to todo.
        tf2 = await tasks_repo.get_tasks(root, project_id, epic_id)
        task_by_id = {t.id: t for t in tf2.tasks}
        assert task_by_id["T1"].status == "done", "done tasks must not be touched"
        assert task_by_id["T2"].status == "todo", (
            f"Expected todo after recovery, got {task_by_id['T2'].status!r}"
        )


# ---------------------------------------------------------------------------
# Review fix #6 — dependency resolution handles out-of-order tasks
# ---------------------------------------------------------------------------


class TestDependencyResolutionLoop:
    """Tasks with forward dependencies execute after their deps complete."""

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_forward_dependency_executes_after_dep_completes(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """T2 depends on T1; Manager dispatches T1 first, then T2 after T1 done."""
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import tasks_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        # Manager declares T2 first (depends_on T1), then T1.
        # Then dispatches T1 (only runnable), then T2 (deps now satisfied), then complete.
        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T2",
                    "title": "Second task",
                    "status": "todo",
                    "repo": git_repo.name,
                    "depends_on": ["T1"],
                },
            ),
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "First task",
                    "status": "todo",
                    "repo": git_repo.name,
                    "depends_on": [],
                },
            ),
            # Dispatch T1 first (T2 not yet runnable — dep T1 not done).
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            # Now T1 is done; dispatch T2.
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T2", "repo": git_repo.name}]},
            ),
            TextTurn("Done planning."),
        ]

        call_counts: dict[str, int] = {"manager": 0, "worker": 0, "evaluator": 0}

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            r = role or "worker"
            count = call_counts.get(r, 0)
            call_counts[r] = count + 1
            if r == "manager":
                return FakeModel(script=list(manager_script))
            if r == "worker":
                # Each worker call writes a distinct file.
                return FakeModel(
                    script=[
                        ToolUseTurn(
                            tool_name="fs_write",
                            tool_input={"path": f"task{count + 1}.py", "content": "x = 1\n"},
                        ),
                        TextTurn("Done."),
                    ]
                )
            # Evaluator always accepts.
            return FakeModel(
                script=[
                    ToolUseTurn(
                        tool_name="submit_verdict",
                        tool_input={"accepted": True, "feedback": ""},
                    ),
                    TextTurn("Accepted."),
                ]
            )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            orch = EpicOrchestrator(
                llm_settings=LLMSettings(provider="fake"),
                git_author_name="yukar",
                git_author_email="yukar@localhost",
                require_plan_approval=False,
            )
            await run_until_parked(orch, root, project_id, epic_id, "run-dep-order")

        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        task_by_id = {t.id: t for t in tf.tasks}

        assert task_by_id["T1"].status == "done", (
            f"T1 should be done, got {task_by_id['T1'].status!r}"
        )
        assert task_by_id["T2"].status == "done", (
            f"T2 should be done even though declared before T1, got {task_by_id['T2'].status!r}"
        )
        # Both workers ran (T1 then T2).
        assert call_counts["worker"] == 2, f"Expected 2 worker calls, got {call_counts['worker']}"

    async def test_unsatisfiable_deps_stay_todo_after_park(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """A task whose dependency never completes stays ``todo`` when the run parks.

        P3 removed the post-loop cleanup that fabricated ``blocked`` tasks: the
        run no longer "ends", so there is nothing to reconcile.  The host still
        rejects the dispatch of a dep-unsatisfied task; the plan simply stays
        as the Manager left it, and it is the user's turn.
        """
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import tasks_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        # T2 depends on T99 (nonexistent) — can never run.
        # Manager creates the task, tries to dispatch (host rejects — unsatisfied
        # dep), then reports and ends its turn — the run parks in waiting.
        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T2",
                    "title": "Unresolvable dep",
                    "status": "todo",
                    "repo": git_repo.name,
                    "depends_on": ["T99"],
                },
            ),
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T2", "repo": git_repo.name}]},
            ),
            TextTurn("T2 is blocked on T99 which does not exist — please advise."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            r = role or "worker"
            if r == "manager":
                return FakeModel(script=list(manager_script))
            # Worker/evaluator should never be reached for T2.
            return FakeModel(script=[TextTurn("Should not run.")])

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            orch = EpicOrchestrator(
                llm_settings=LLMSettings(provider="fake"),
                git_author_name="yukar",
                git_author_email="yukar@localhost",
                require_plan_approval=False,
            )
            await run_until_parked(orch, root, project_id, epic_id, "run-unresolvable")

        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        task_by_id = {t.id: t for t in tf.tasks}
        assert task_by_id["T2"].status == "todo", (
            "P3: no post-loop cleanup may fabricate a blocked status; "
            f"T2 must stay todo, got {task_by_id['T2'].status!r}"
        )


# ---------------------------------------------------------------------------
# New tests: Agent-as-a-Tool Manager autonomy
# ---------------------------------------------------------------------------


class TestManagerAutonomy:
    """Tests proving Manager autonomy in the Agent-as-a-Tool design.

    These tests verify that the Manager can dispatch multiple independent
    tasks in one call (parallel intent).
    """

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_two_independent_tasks_dispatched_in_parallel(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """Manager passes two independent tasks in one dispatch call.

        Both tasks are assigned to the same repo (so host serialises them) but
        the Manager sends them in a single ``dispatch`` call demonstrating the
        parallel-intent API.  Both must end as done.
        """
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import tasks_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        # Manager creates T1 and T2 (no deps), dispatches both in one call.
        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "First independent task",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T2",
                    "title": "Second independent task",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            # Single dispatch with both tasks — Manager asserts parallel intent.
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={
                    "items": [
                        {"task_id": "T1", "repo": git_repo.name},
                        {"task_id": "T2", "repo": git_repo.name},
                    ]
                },
            ),
            TextTurn("Both done."),
        ]

        call_counts: dict[str, int] = {"manager": 0, "worker": 0, "evaluator": 0}

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            r = role or "worker"
            count = call_counts.get(r, 0)
            call_counts[r] = count + 1
            if r == "manager":
                return FakeModel(script=list(manager_script))
            if r == "worker":
                return FakeModel(
                    script=[
                        ToolUseTurn(
                            tool_name="fs_write",
                            tool_input={"path": f"file{count + 1}.py", "content": f"x = {count}\n"},
                        ),
                        TextTurn("Done."),
                    ]
                )
            return FakeModel(
                script=[
                    ToolUseTurn(
                        tool_name="submit_verdict",
                        tool_input={"accepted": True, "feedback": ""},
                    ),
                    TextTurn("Accepted."),
                ]
            )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            orch = EpicOrchestrator(
                llm_settings=LLMSettings(provider="fake"),
                git_author_name="yukar",
                git_author_email="yukar@localhost",
                require_plan_approval=False,
            )
            await run_until_parked(orch, root, project_id, epic_id, "run-parallel")

        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        task_by_id = {t.id: t for t in tf.tasks}
        assert task_by_id["T1"].status == "done", (
            f"T1 should be done, got {task_by_id['T1'].status!r}"
        )
        assert task_by_id["T2"].status == "done", (
            f"T2 should be done, got {task_by_id['T2'].status!r}"
        )
        # Two workers ran.
        assert call_counts["worker"] == 2, f"Expected 2 worker calls, got {call_counts['worker']}"


# ---------------------------------------------------------------------------
# Tests: Manager turn>0 loop (finding 6)
# ---------------------------------------------------------------------------


class TestManagerMultiTurn:
    """Verify the P3 turn loop: one user input drives exactly one turn.

    Turn 0 ends with a TextTurn → the run parks in ``waiting``.  The user's
    reply (inject_message) wakes the run; the Strands agent's next
    ``stream_async()`` call consumes the rest of the script (turn 1).
    """

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_manager_second_turn_called(self, git_repo: Path, tmp_path: Path) -> None:
        """Manager loop reaches turn>0 via a user reply: plan in turn 0, park,
        user replies, dispatch in turn 1, park again.

        Turn 0: task_update + TextTurn → the run parks in ``waiting``.
        User reply: wakes the run (the only way a next turn ever starts).
        Turn 1: dispatch + TextTurn → T1 done, run parks again.

        If the reply did not drive a second turn, T1 would remain 'todo'
        because dispatch was never called.  Asserting T1=='done' proves the
        wake→turn wiring.
        """
        from unittest.mock import patch

        from tests._helpers import wait_until
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import state_repo, tasks_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script: list[Any] = [
            # -- turn 0: plan only, no dispatch --
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Multi-turn task",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            TextTurn("Planned T1; will dispatch next turn."),
            # -- turn 1: dispatch + complete --
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            TextTurn("All done."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            r = role or "worker"
            if r == "manager":
                return FakeModel(script=list(manager_script))
            if r == "worker":
                return FakeModel(
                    script=[
                        ToolUseTurn(
                            tool_name="fs_write",
                            tool_input={"path": "out.py", "content": "x = 1\n"},
                        ),
                        TextTurn("Done."),
                    ]
                )
            # evaluator
            return FakeModel(
                script=[
                    ToolUseTurn(
                        tool_name="submit_verdict",
                        tool_input={"accepted": True, "feedback": ""},
                    ),
                    TextTurn("Accepted."),
                ]
            )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            orch = EpicOrchestrator(
                llm_settings=LLMSettings(provider="fake"),
                git_author_name="yukar",
                git_author_email="yukar@localhost",
                require_plan_approval=False,
            )
            run_task = asyncio.create_task(orch.start(root, project_id, epic_id, "run-multi-turn"))
            # Turn 0 ends → park.
            await wait_for_run_status(root, project_id, epic_id, "waiting", timeout=30.0)

            # The user replies — this is the ONLY way a next turn starts.
            orch.inject_message("manager", "Looks good — dispatch T1.")

            # Turn 1 dispatches T1; wait for the observable side effect, then
            # for the second park (state cycles waiting→running→waiting).
            async def _t1_done() -> bool:
                tf_now = await tasks_repo.get_tasks(root, project_id, epic_id)
                return bool(tf_now.tasks) and tf_now.tasks[0].status == "done"

            await wait_until(_t1_done, timeout=30.0, message="T1 to be done after the reply")
            await wait_for_run_status(root, project_id, epic_id, "waiting", timeout=30.0)
            await orch.stop()
            await asyncio.wait_for(run_task, timeout=10.0)

        # T1 done proves turn 1 ran (dispatch is only in turn 1's script).
        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        assert tf.tasks[0].status == "done", f"T1 should be done, got {tf.tasks[0].status!r}"
        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None
        assert state.status == "waiting", f"Run should be waiting, got {state.status!r}"


# ---------------------------------------------------------------------------
# Tests: dispatch rejection branches (finding 7)
# ---------------------------------------------------------------------------


class TestDispatchRejections:
    """Tests for _run_dispatch rejection paths.

    Verified by running the full orchestrator E2E with a scripted Manager and
    checking observable state (task status in tasks.yaml, run completion) after
    the run.
    """

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_task_not_found_run_still_completes(self, git_repo: Path, tmp_path: Path) -> None:
        """dispatch of a nonexistent task_id is rejected but run still completes.

        The Manager dispatches 'NONEXISTENT' (never created via task_update).
        The host rejects the item; the Manager reports in its message body and
        the run parks in waiting (a rejection is not a run failure).
        """
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import state_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script: list[Any] = [
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "NONEXISTENT"}]},
            ),
            TextTurn("Done."),
        ]
        with patch("yukar.agents.orchestrator.create_model") as mock_cm:
            mock_cm.side_effect = lambda settings, role=None, **kw: FakeModel(
                script=list(manager_script) if (role or "worker") == "manager" else []
            )
            orch = EpicOrchestrator(
                llm_settings=LLMSettings(provider="fake"),
                git_author_name="yukar",
                git_author_email="yukar@localhost",
                require_plan_approval=False,
            )
            await run_until_parked(orch, root, project_id, epic_id, "run-dispatch-not-found")

        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None
        assert state.status == "waiting", (
            f"Run should park when dispatch item is rejected; got {state.status!r}"
        )

    async def test_unsatisfied_dep_task_stays_todo(self, git_repo: Path, tmp_path: Path) -> None:
        """A dispatch of a dep-unsatisfied task is rejected; the task stays todo.

        T2 depends on T1 (never created/done).  Manager dispatches T2; the host
        rejects it and the Manager ends its turn.  P3 removed the post-loop
        cleanup, so nothing fabricates a blocked status — the plan stays as
        the Manager left it.
        """
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import tasks_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script: list[Any] = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T2",
                    "title": "Dependent task",
                    "status": "todo",
                    "repo": git_repo.name,
                    "depends_on": ["T1"],
                },
            ),
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T2"}]},
            ),
            TextTurn("Done."),
        ]
        with patch("yukar.agents.orchestrator.create_model") as mock_cm:
            mock_cm.side_effect = lambda settings, role=None, **kw: FakeModel(
                script=list(manager_script) if (role or "worker") == "manager" else []
            )
            orch = EpicOrchestrator(
                llm_settings=LLMSettings(provider="fake"),
                git_author_name="yukar",
                git_author_email="yukar@localhost",
                require_plan_approval=False,
            )
            await run_until_parked(orch, root, project_id, epic_id, "run-dispatch-dep")

        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        t2 = next(t for t in tf.tasks if t.id == "T2")
        assert t2.status == "todo", (
            f"T2 with unsatisfied dep must stay todo (no fabricated blocked), got {t2.status!r}"
        )

    async def test_attempt_limit_blocks_task(self, git_repo: Path, tmp_path: Path) -> None:
        """After _MAX_ATTEMPTS_PER_TASK rejected attempts the task is blocked."""
        from unittest.mock import patch

        from yukar.agents.orchestrator import _MAX_ATTEMPTS_PER_TASK, EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import tasks_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        # Build a manager script that dispatches T1 _MAX_ATTEMPTS+1 times.
        # Each dispatch produces a rejection (worker does nothing, evaluator rejects).
        dispatch_items = [{"task_id": "T1", "repo": git_repo.name}]
        manager_script: list[Any] = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Retried task",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
        ]
        # Dispatch _MAX_ATTEMPTS_PER_TASK+1 times to hit the limit.
        for _ in range(_MAX_ATTEMPTS_PER_TASK + 1):
            manager_script.append(
                ToolUseTurn(
                    tool_name="dispatch",
                    tool_input={"items": dispatch_items},
                )
            )
        manager_script.append(TextTurn("Done."))

        # Worker does nothing useful; evaluator always rejects.
        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            r = role or "worker"
            if r == "manager":
                return FakeModel(script=list(manager_script))
            if r == "worker":
                return FakeModel(script=[TextTurn("No work done.")])
            # evaluator rejects
            return FakeModel(
                script=[
                    ToolUseTurn(
                        tool_name="submit_verdict",
                        tool_input={"accepted": False, "feedback": "not done"},
                    ),
                    TextTurn("Rejected."),
                ]
            )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            orch = EpicOrchestrator(
                llm_settings=LLMSettings(provider="fake"),
                git_author_name="yukar",
                git_author_email="yukar@localhost",
                require_plan_approval=False,
            )
            await run_until_parked(orch, root, project_id, epic_id, "run-attempt-limit")

        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        t1 = next(t for t in tf.tasks if t.id == "T1")
        assert t1.status == "blocked", (
            f"T1 should be blocked after attempt limit, got {t1.status!r}"
        )


# ---------------------------------------------------------------------------
# Tests: _MAX_MANAGER_TURNS exhaustion — cost backstop, not an error (P3)
# ---------------------------------------------------------------------------


class TestManagerTurnLimit:
    """The turn limit is a pure cost backstop under park-every-turn semantics.

    One user input drives exactly one turn, so reaching the limit just ends
    the run TASK: the final turn already parked the run in ``waiting``, the
    conversation is intact, and the next user message starts a continuation.
    No error state, no RunFailedEvent, no RunCompletedEvent.
    """

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_turn_limit_leaves_waiting_state(self, git_repo: Path, tmp_path: Path) -> None:
        """Exhaust a patched 2-turn limit: the run task ends normally and the
        state stays ``waiting`` (restartable as a continuation)."""
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import state_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        busy_turn = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Stuck task",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            TextTurn("Reworked T1; still not done."),
        ]
        manager_script = [*busy_turn, *busy_turn]

        events_received: list[Any] = []

        async def _collect() -> None:
            async for ev in event_bus.event_stream(project_id, epic_id):
                events_received.append(ev)

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        with (
            patch("yukar.agents.orchestrator._MAX_MANAGER_TURNS", 2),
            patch("yukar.agents.orchestrator.create_model") as mock_cm,
        ):
            mock_cm.side_effect = lambda settings, role=None, **kw: FakeModel(
                script=list(manager_script)
            )
            orch = EpicOrchestrator(
                llm_settings=LLMSettings(provider="fake"),
                git_author_name="yukar",
                git_author_email="yukar@localhost",
                require_plan_approval=False,
            )
            run_task = asyncio.create_task(
                orch.start(root, project_id, epic_id, "run-turn-limit")
            )
            # Turn 0 parks; the user's reply drives turn 1, which exhausts the
            # patched limit — the run task must then finish WITHOUT an error.
            await wait_for_run_status(root, project_id, epic_id, "waiting", timeout=30.0)
            orch.inject_message("manager", "keep going")
            await asyncio.wait_for(run_task, timeout=30.0)

        await asyncio.wait_for(collector, timeout=5.0)

        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None, "state.yaml should exist"
        assert state.status == "waiting", (
            f"Turn-limit exhaustion must leave the run waiting, got {state.status!r}"
        )

        event_types = [getattr(ev, "type", None) for ev in events_received]
        assert "run_failed" not in event_types, (
            "The turn limit is a cost backstop, not an error"
        )
        assert "run_completed" not in event_types, (
            "A conversation run never emits run_completed"
        )


class TestHITLEndToEnd:
    """End-to-end: a HITL message injected into a run reaches the Manager's prompt.

    ``TestEpicOrchestrator.test_hitl_injection_reaches_orchestrator`` only covers
    the queue (inject_message / _drain_pending) in isolation.  This test drives a
    real orchestrator run and asserts the injected text is actually drained into
    the Manager's next prompt — the drain → hitl_prefix → stream_async wiring.
    """

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_injected_manager_message_reaches_prompt(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        import json
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import state_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        recorded_prompts: list[str] = []

        class RecordingManagerModel(FakeModel):
            """FakeModel that records the conversation passed to each stream()."""

            def stream(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
                recorded_prompts.append(json.dumps(messages, default=str))
                return super().stream(messages, *args, **kwargs)

        # turn 0: create T1 (todo); the trailing text ends the turn and the
        # run parks in waiting.  No task is dispatched, so no Worker/Evaluator
        # runs — the test stays fast and focused on HITL wiring.
        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "T",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            TextTurn("Planned."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            if role == "manager":
                return RecordingManagerModel(script=list(manager_script))
            return FakeModel(script=[TextTurn("noop")])

        marker = "PING-HITL-XYZZY"
        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="yukar",
            git_author_email="yukar@localhost",
            require_plan_approval=False,
        )
        # Inject a HITL message for the manager BEFORE the run starts; it must be
        # drained into turn 0's prompt.
        orch.inject_message("manager", marker)

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            await run_until_parked(orch, root, project_id, epic_id, "run-hitl-e2e")

        assert recorded_prompts, "Manager model was never invoked"
        assert any(marker in p for p in recorded_prompts), (
            "Injected HITL message did not reach the Manager prompt"
        )

        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None
        assert state.status == "waiting"


# ---------------------------------------------------------------------------
# A1: limits propagation — run_worker / run_evaluator pass limits= to stream_async
# ---------------------------------------------------------------------------


class TestAgentLimitsPropagation:
    """Verify that AgentSettings.worker_max_turns / evaluator_max_turns are wired
    into the ``limits=`` kwarg of Agent.stream_async (spec A1)."""

    @pytest.mark.asyncio
    async def test_run_worker_passes_limits_to_stream_async(self, tmp_path: Path) -> None:
        """run_worker must call stream_async(prompt, limits={"turns": max_turns})."""
        from types import SimpleNamespace
        from typing import cast
        from unittest.mock import AsyncMock, MagicMock, patch

        from yukar.agents.context import AgentContext
        from yukar.agents.worker import run_worker
        from yukar.models.task import Task

        captured_limits: list[Any] = []

        class _FakeAgent:
            def __init__(self, **kwargs: Any) -> None:
                self.event_loop_metrics = SimpleNamespace(accumulated_usage={})
                self.messages = [{"role": "assistant", "content": [{"text": "done"}]}]
                self.callback_handler = kwargs.get("callback_handler")

            async def stream_async(self, prompt: str, *, limits: Any = None) -> Any:
                captured_limits.append(limits)
                self.event_loop_metrics.accumulated_usage = {}
                if self.callback_handler:
                    self.callback_handler(
                        message={"role": "assistant", "content": [{"text": "done"}]}
                    )
                return
                yield  # make it an async generator

        ctx = cast(
            AgentContext,
            SimpleNamespace(
                worktree_path=tmp_path,
                workspace_root=str(tmp_path),
                project_id="p",
                repo_name="repo",
            ),
        )
        task = Task(id="T1", title="do something", status="in_progress")

        with (
            patch("yukar.agents.worker.Agent", _FakeAgent),
            patch("yukar.agents.worker.make_fs_tools", return_value=[]),
            patch("yukar.agents.worker.make_fs_edit_tools", return_value=[]),
            patch("yukar.agents.worker.make_command_tools", return_value=[]),
            patch("yukar.agents.worker.make_git_tools", return_value=[]),
            patch("yukar.agents.worker.make_repo_tools", return_value=[]),
            patch("yukar.agents.worker.session_store.append_message", new=AsyncMock()),
            patch(
                "yukar.agents.streaming.AgentUsageRecorder.bind",
                return_value=MagicMock(flush=AsyncMock()),
            ),
        ):
            await run_worker(
                project_id="p",
                epic_id="e",
                run_id="r",
                worker_id="w1",
                task=task,
                ctx=ctx,
                feedback="",
                hitl_prefix="",
                worker_model=MagicMock(),
                conversation_manager=None,
                indexer_service=None,
                git_author_name="yukar",
                git_author_email="yukar@localhost",
                max_turns=42,
            )

        assert captured_limits, "stream_async was never called"
        assert captured_limits[0]["turns"] == 42, (
            f"Expected limits['turns']==42, got {captured_limits[0]}"
        )

    @pytest.mark.asyncio
    async def test_run_worker_passes_total_tokens_when_set(self, tmp_path: Path) -> None:
        """When max_total_tokens is set, limits must include 'total_tokens'."""
        from types import SimpleNamespace
        from typing import cast
        from unittest.mock import AsyncMock, MagicMock, patch

        from yukar.agents.context import AgentContext
        from yukar.agents.worker import run_worker
        from yukar.models.task import Task

        captured_limits: list[Any] = []

        class _FakeAgent:
            def __init__(self, **kwargs: Any) -> None:
                self.event_loop_metrics = SimpleNamespace(accumulated_usage={})
                self.messages = [{"role": "assistant", "content": [{"text": "done"}]}]
                self.callback_handler = kwargs.get("callback_handler")

            async def stream_async(self, prompt: str, *, limits: Any = None) -> Any:
                captured_limits.append(limits)
                self.event_loop_metrics.accumulated_usage = {}
                if self.callback_handler:
                    self.callback_handler(
                        message={"role": "assistant", "content": [{"text": "done"}]}
                    )
                return
                yield

        ctx = cast(
            AgentContext,
            SimpleNamespace(
                worktree_path=tmp_path,
                workspace_root=str(tmp_path),
                project_id="p",
                repo_name="repo",
            ),
        )
        task = Task(id="T1", title="budget task", status="in_progress")

        with (
            patch("yukar.agents.worker.Agent", _FakeAgent),
            patch("yukar.agents.worker.make_fs_tools", return_value=[]),
            patch("yukar.agents.worker.make_fs_edit_tools", return_value=[]),
            patch("yukar.agents.worker.make_command_tools", return_value=[]),
            patch("yukar.agents.worker.make_git_tools", return_value=[]),
            patch("yukar.agents.worker.make_repo_tools", return_value=[]),
            patch("yukar.agents.worker.session_store.append_message", new=AsyncMock()),
            patch(
                "yukar.agents.streaming.AgentUsageRecorder.bind",
                return_value=MagicMock(flush=AsyncMock()),
            ),
        ):
            await run_worker(
                project_id="p",
                epic_id="e",
                run_id="r",
                worker_id="w1",
                task=task,
                ctx=ctx,
                feedback="",
                hitl_prefix="",
                worker_model=MagicMock(),
                conversation_manager=None,
                indexer_service=None,
                git_author_name="yukar",
                git_author_email="yukar@localhost",
                max_turns=30,
                max_total_tokens=100_000,
            )

        assert captured_limits, "stream_async was never called"
        assert captured_limits[0]["turns"] == 30
        assert captured_limits[0]["total_tokens"] == 100_000

    @pytest.mark.asyncio
    async def test_run_evaluator_passes_limits_to_stream_async(self, tmp_path: Path) -> None:
        """run_evaluator must call stream_async(prompt, limits={"turns": max_turns})."""
        from types import SimpleNamespace
        from typing import cast
        from unittest.mock import AsyncMock, MagicMock, patch

        from yukar.agents.context import AgentContext
        from yukar.agents.evaluator import run_evaluator
        from yukar.models.task import Task

        captured_limits: list[Any] = []

        class _FakeAgent:
            def __init__(self, **kwargs: Any) -> None:
                self.event_loop_metrics = SimpleNamespace(accumulated_usage={})
                self.messages = [{"role": "assistant", "content": [{"text": "verdict"}]}]
                self.callback_handler = kwargs.get("callback_handler")

            async def stream_async(self, prompt: str, *, limits: Any = None) -> Any:
                captured_limits.append(limits)
                self.event_loop_metrics.accumulated_usage = {}
                if self.callback_handler:
                    self.callback_handler(
                        message={"role": "assistant", "content": [{"text": "verdict"}]}
                    )
                return
                yield

        ctx = cast(
            AgentContext,
            SimpleNamespace(
                worktree_path=tmp_path,
                workspace_root=str(tmp_path),
                project_id="p",
                repo_name="repo",
            ),
        )
        task = Task(id="T1", title="eval task", status="in_progress")

        with (
            patch("yukar.agents.evaluator.Agent", _FakeAgent),
            patch("yukar.agents.evaluator.make_evaluator_tools", return_value=[]),
            patch("yukar.agents.evaluator.session_store.append_message", new=AsyncMock()),
            patch(
                "yukar.agents.streaming.AgentUsageRecorder.bind",
                return_value=MagicMock(flush=AsyncMock()),
            ),
        ):
            await run_evaluator(
                project_id="p",
                epic_id="e",
                run_id="r",
                eval_id="ev1",
                task=task,
                ctx=ctx,
                worker_id="w1",
                eval_model=MagicMock(),
                conversation_manager=None,
                max_turns=15,
            )

        assert captured_limits, "stream_async was never called"
        assert captured_limits[0]["turns"] == 15, (
            f"Expected limits['turns']==15, got {captured_limits[0]}"
        )
