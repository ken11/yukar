"""Regression tests: Strands stream chunks arrive as *incremental* TokenEvents.

Background
----------
FakeModel._emit splits TextTurn.text into chunks of ~12 chars and yields one
contentBlockDelta per chunk.  Strands fires callback_handler(data=<chunk>) for
each delta, which StreamTranslator translates to a TokenEvent.  This file proves
that the full pipeline — FakeModel → Agent.stream_async → StreamTranslator →
event_bus — actually delivers *multiple* TokenEvents per turn and that
concatenating their deltas reconstructs the original text.

The autouse fixture ``zero_fake_sleep`` (conftest.py) sets YUKAR_FAKE_SLEEP=0,
so no real delay is incurred.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests._helpers import make_git_repo

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Must be long enough to produce more than one 12-char chunk.
_LONG_TEXT = "Hello from the streaming pipeline, this is a long enough message."
assert len(_LONG_TEXT) > 12, "Test text must exceed one chunk"

# Expected minimum chunk count: ceil(len / 12).  Actual may vary due to
# word-boundary splitting, but must be > 1.
_MIN_CHUNKS = 2


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
        slug="streaming-epic",
        title="Streaming Epic",
        description="Epic for streaming regression tests.",
        branch="yukar/ep-1-streaming-epic",
    )
    await save_epic(root, project_id, epic)


# ---------------------------------------------------------------------------
# Unit: FakeModel + Agent.stream_async → StreamTranslator → bus (no orchestrator)
# ---------------------------------------------------------------------------


class TestStreamTranslatorIncremental:
    """Verify that a single long TextTurn produces multiple TokenEvents on the bus."""

    async def test_long_text_turn_produces_multiple_token_events(self) -> None:
        """A TextTurn longer than _CHUNK_SIZE generates >1 TokenEvent on the bus.

        This is the fundamental unit-level proof that FakeModel chunking flows
        through StreamTranslator to the event bus as incremental deltas.
        """
        from strands import Agent

        from yukar.agents.streaming import StreamTranslator
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn
        from yukar.models.events import TokenEvent

        model = FakeModel(script=[TextTurn(_LONG_TEXT)])
        translator = StreamTranslator(
            project_id="p-incr",
            epic_id="e-incr",
            run_id="r-incr",
            thread_id="t-incr",
        )
        agent = Agent(model=model, tools=[], callback_handler=translator.callback)

        token_events: list[TokenEvent] = []

        async with event_bus.subscribe("p-incr", "e-incr") as q:

            async def _run() -> None:
                async for _ in agent.stream_async("go"):
                    pass

            run_task = asyncio.create_task(_run())

            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                if run_task.done() and q.empty():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=0.1)
                    if isinstance(ev, TokenEvent) and ev.thread_id == "t-incr":
                        token_events.append(ev)
                except TimeoutError:
                    if run_task.done():
                        break

            await run_task

        assert len(token_events) >= _MIN_CHUNKS, (
            f"Expected >= {_MIN_CHUNKS} TokenEvents for a {len(_LONG_TEXT)}-char text, "
            f"got {len(token_events)}: {[e.delta for e in token_events]}"
        )

        reconstructed = "".join(e.delta for e in token_events)
        assert reconstructed == _LONG_TEXT, (
            f"Concatenated deltas do not match original text.\n"
            f"  expected: {_LONG_TEXT!r}\n"
            f"  got:      {reconstructed!r}"
        )

    async def test_chunk_split_utility(self) -> None:
        """_split_chunks produces >1 chunk for texts longer than _CHUNK_SIZE."""
        from yukar.llm.fake import _CHUNK_SIZE, _split_chunks

        text = "A" * (_CHUNK_SIZE + 1)
        chunks = _split_chunks(text)
        assert len(chunks) > 1, "Expected more than one chunk for long text"
        assert "".join(chunks) == text, "Chunks must reconstruct the original text"

    async def test_multiple_turns_produce_independent_token_sequences(self) -> None:
        """Two TextTurns produce independent incremental sequences, each concatenating correctly."""
        from strands import Agent

        from yukar.agents.streaming import StreamTranslator
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn
        from yukar.models.events import TokenEvent

        text_a = "First turn with enough characters to exceed chunk limit easily here."
        text_b = "Second turn is also long enough to produce multiple streaming chunks."

        assert len(text_a) > 12
        assert len(text_b) > 12

        model = FakeModel(script=[TextTurn(text_a), TextTurn(text_b)])
        translator = StreamTranslator(
            project_id="p-multi",
            epic_id="e-multi",
            run_id="r-multi",
            thread_id="t-multi",
        )
        agent = Agent(model=model, tools=[], callback_handler=translator.callback)

        token_events: list[TokenEvent] = []

        async with event_bus.subscribe("p-multi", "e-multi") as q:

            async def _run_twice() -> None:
                # First turn
                async for _ in agent.stream_async("turn one"):
                    pass
                # Second turn (agent's internal message history now has the first response)
                async for _ in agent.stream_async("turn two"):
                    pass

            run_task = asyncio.create_task(_run_twice())

            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                if run_task.done() and q.empty():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=0.1)
                    if isinstance(ev, TokenEvent):
                        token_events.append(ev)
                except TimeoutError:
                    if run_task.done():
                        break

            await run_task

        reconstructed = "".join(e.delta for e in token_events)
        assert reconstructed == text_a + text_b, (
            f"Expected concatenation of both turns.\n"
            f"  expected: {(text_a + text_b)!r}\n"
            f"  got:      {reconstructed!r}"
        )
        # Both turns must have produced multiple chunks each.
        assert len(token_events) >= _MIN_CHUNKS * 2, (
            f"Expected >= {_MIN_CHUNKS * 2} total TokenEvents across two turns, "
            f"got {len(token_events)}"
        )


# ---------------------------------------------------------------------------
# E2E: EpicOrchestrator — Manager thread_id and Worker thread_id
# ---------------------------------------------------------------------------


class TestOrchestratorIncrementalStreaming:
    """Verify incremental TokenEvent delivery through the full orchestrator pipeline.

    Covers both the Manager agent thread and the Worker agent thread to confirm
    that the StreamTranslator is wired correctly for each role.
    """

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "streamrepo")

    async def _run_and_collect(
        self,
        root: str,
        project_id: str,
        epic_id: str,
        manager_text: str,
        worker_text: str,
        git_repo: Path,
    ) -> list[Any]:
        """Run the orchestrator with text-heavy scripts and return all bus events."""
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn

        llm = LLMSettings(provider="fake")

        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Streaming task",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            ToolUseTurn(tool_name="complete_epic", tool_input={}),
            # Long text turn — must produce multiple TokenEvents on the manager thread.
            TextTurn(manager_text),
        ]

        worker_script = [
            ToolUseTurn(
                tool_name="fs_write",
                tool_input={"path": "out.py", "content": "# generated\n"},
            ),
            # Long text turn — must produce multiple TokenEvents on the worker thread.
            TextTurn(worker_text),
        ]

        evaluator_script = [
            ToolUseTurn(
                tool_name="submit_verdict",
                tool_input={"accepted": True, "feedback": ""},
            ),
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
            )
            await orch.start(root, project_id, epic_id, "run-streaming")

        await asyncio.wait_for(collector, timeout=10.0)
        return events_received

    async def test_manager_thread_delivers_incremental_tokens(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """Manager thread produces multiple TokenEvents whose deltas reconstruct the original text.

        This proves the Manager → StreamTranslator → event_bus incremental path.
        """
        from yukar.models.events import TokenEvent

        manager_text = (
            "Manager says: this is a long enough narration to span multiple streaming chunks."
        )
        assert len(manager_text) > 12

        root = str(tmp_path / "ws")
        project_id = "p-mgr"
        epic_id = "EP-mgr"
        await _bootstrap(root, project_id, epic_id, git_repo)

        events = await self._run_and_collect(
            root=root,
            project_id=project_id,
            epic_id=epic_id,
            manager_text=manager_text,
            worker_text="Worker done.",
            git_repo=git_repo,
        )

        # Identify the manager thread_id.  The orchestrator uses a fixed thread ID
        # for the manager agent (typically "manager"); collect all token events.
        manager_token_events = [
            e for e in events if isinstance(e, TokenEvent) and e.thread_id == "manager"
        ]

        assert len(manager_token_events) >= _MIN_CHUNKS, (
            f"Expected >= {_MIN_CHUNKS} TokenEvents on the manager thread for "
            f"{len(manager_text)}-char text, "
            f"got {len(manager_token_events)}: {[e.delta for e in manager_token_events]}"
        )

        reconstructed = "".join(e.delta for e in manager_token_events)
        assert reconstructed == manager_text, (
            f"Manager token deltas do not reconstruct the original text.\n"
            f"  expected: {manager_text!r}\n"
            f"  got:      {reconstructed!r}"
        )

    async def test_worker_thread_delivers_incremental_tokens(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """Worker thread produces multiple TokenEvents whose deltas reconstruct the original text.

        This proves the Worker → StreamTranslator → event_bus incremental path.
        """
        from yukar.models.events import TokenEvent

        worker_text = "Worker says: implementation is complete with all required changes committed."
        assert len(worker_text) > 12

        root = str(tmp_path / "ws")
        project_id = "p-wkr"
        epic_id = "EP-wkr"
        await _bootstrap(root, project_id, epic_id, git_repo)

        events = await self._run_and_collect(
            root=root,
            project_id=project_id,
            epic_id=epic_id,
            manager_text="Manager done.",
            worker_text=worker_text,
            git_repo=git_repo,
        )

        # Worker thread IDs are assigned by the orchestrator as the worker_id,
        # which is formatted as "worker-{hex8}".  Evaluators use "eval-{hex8}".
        # Filter strictly to avoid including evaluator tokens.
        worker_token_events = [
            e for e in events if isinstance(e, TokenEvent) and e.thread_id.startswith("worker-")
        ]

        assert len(worker_token_events) >= _MIN_CHUNKS, (
            f"Expected >= {_MIN_CHUNKS} TokenEvents on the worker thread for "
            f"{len(worker_text)}-char text, "
            f"got {len(worker_token_events)}: {[e.delta for e in worker_token_events]}"
        )

        reconstructed = "".join(e.delta for e in worker_token_events)
        assert reconstructed == worker_text, (
            f"Worker token deltas do not reconstruct the original text.\n"
            f"  expected: {worker_text!r}\n"
            f"  got:      {reconstructed!r}"
        )

    async def test_both_manager_and_worker_deliver_incremental_tokens(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """Both Manager and Worker produce >= _MIN_CHUNKS TokenEvents each.

        This is the composite regression guard: both roles must stream
        incrementally end-to-end through the orchestrator.
        """
        from yukar.models.events import TokenEvent

        manager_text = "Manager narration: planning phase is complete with all tasks allocated."
        worker_text = "Worker narration: all files have been written and committed to the branch."

        assert len(manager_text) > 12
        assert len(worker_text) > 12

        root = str(tmp_path / "ws")
        project_id = "p-both"
        epic_id = "EP-both"
        await _bootstrap(root, project_id, epic_id, git_repo)

        events = await self._run_and_collect(
            root=root,
            project_id=project_id,
            epic_id=epic_id,
            manager_text=manager_text,
            worker_text=worker_text,
            git_repo=git_repo,
        )

        manager_token_events = [
            e for e in events if isinstance(e, TokenEvent) and e.thread_id == "manager"
        ]
        worker_token_events = [
            e for e in events if isinstance(e, TokenEvent) and e.thread_id.startswith("worker-")
        ]

        # Manager incremental assertion.
        assert len(manager_token_events) >= _MIN_CHUNKS, (
            f"Manager: expected >= {_MIN_CHUNKS} TokenEvents, got {len(manager_token_events)}"
        )
        mgr_reconstructed = "".join(e.delta for e in manager_token_events)
        assert mgr_reconstructed == manager_text, (
            f"Manager deltas mismatch.\n"
            f"  expected: {manager_text!r}\n"
            f"  got:      {mgr_reconstructed!r}"
        )

        # Worker incremental assertion.
        assert len(worker_token_events) >= _MIN_CHUNKS, (
            f"Worker: expected >= {_MIN_CHUNKS} TokenEvents, got {len(worker_token_events)}"
        )
        wkr_reconstructed = "".join(e.delta for e in worker_token_events)
        assert wkr_reconstructed == worker_text, (
            f"Worker deltas mismatch.\n"
            f"  expected: {worker_text!r}\n"
            f"  got:      {wkr_reconstructed!r}"
        )
