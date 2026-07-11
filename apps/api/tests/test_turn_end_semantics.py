"""Tests for turn-end semantics: a silent turn end is the agent's yield.

The host never injects a dispatch command.  A manager turn that ends without
ask_user / complete_epic and without an effector tool (dispatch / task_update)
is honoured as the agent yielding to the user:

- A tool-less reply to a HUMAN message parks the run immediately in
  question-less awaiting_input (conversation must not be interrupted).
- A silent end after a HOST prompt gets ONE neutral stall notice; a second
  consecutive silent end parks.
- Real work (dispatch / task_update) resets the notice one-shot, so
  multi-batch autonomous runs keep flowing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests._helpers import make_git_repo

_STALL_NOTICE_MARKER = "You ended your turn without calling"
_OLD_NUDGE_MARKER = "Select runnable tasks"


async def _bootstrap(root: str, project_id: str, epic_id: str, repo_path: Path) -> None:
    from yukar.models.epic import Epic
    from yukar.models.project import Project, Repo, RepoCommands
    from yukar.storage.epic_repo import save_epic
    from yukar.storage.project_repo import save_project, save_repo

    await save_project(
        root, Project(id=project_id, name=project_id, status="active", repos=[repo_path.name])
    )
    await save_repo(
        root,
        project_id,
        Repo(
            name=repo_path.name,
            path=str(repo_path),
            default_branch="main",
            commands=RepoCommands(allow=["git", "pytest"], deny=[]),
        ),
    )
    await save_epic(
        root,
        project_id,
        Epic(
            id=epic_id,
            slug="turn-end",
            title="Turn End Epic",
            description="Epic for turn-end semantics tests.",
            branch="yukar/turn-end",
        ),
    )


def _fsm_user_texts(root: str, project_id: str, epic_id: str) -> list[str]:
    """Return the text of every user-role FSM message (empty string for non-text)."""
    from yukar.storage import session_store

    messages = session_store.list_messages(root, project_id, epic_id, "manager")
    texts: list[str] = []
    for m in messages:
        if m.message.role != "user":
            continue
        for part in m.message.content:
            if part.text:
                texts.append(part.text)
    return texts


class TestParkHelper:
    """Unit: _park_awaiting_user persists question-less awaiting_input."""

    async def test_park_publishes_empty_question_and_persists(self, tmp_path: Path) -> None:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.models.epic import Epic
        from yukar.models.events import UserInputRequestedEvent
        from yukar.models.project import Project
        from yukar.models.run import RunState
        from yukar.storage import state_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        await save_project(root, Project(id="proj", name="proj"))
        await save_epic(root, "proj", Epic(id="ep", slug="ep", title="Ep"))

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="Test",
            git_author_email="test@example.com",
        )
        orch._root = root
        orch._project_id = "proj"
        orch._epic_id = "ep"
        orch._run_id = "run-park"
        orch._state = RunState(run_id="run-park", status="running")

        emitted: list[Any] = []
        orch._pub = emitted.append

        await orch._park_awaiting_user()

        assert orch._awaiting_user is True
        assert orch._pending_question == ""

        persisted = await state_repo.get_state(root, "proj", "ep")
        assert persisted is not None
        assert persisted.status == "awaiting_input"
        assert persisted.pending_question is None

        uir = [e for e in emitted if isinstance(e, UserInputRequestedEvent)]
        assert len(uir) == 1
        assert uir[0].question == ""
        assert uir[0].thread_id == "manager"


class TestConversationalPark:
    """E2E: a tool-less reply to a human message parks; a later reply resumes work."""

    async def test_toolless_reply_parks_then_user_resumes(self, tmp_path: Path) -> None:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.models.events import UserInputRequestedEvent
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-CONV"
        run_id = "run-conv-park"

        git_repo = make_git_repo(tmp_path, "myrepo")
        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script = [
            # Turn 0: plan + ask_user.
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Write hello.py",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            ToolUseTurn(tool_name="ask_user", tool_input={"question": "Plan: T1. OK?"}),
            TextTurn("Waiting for your reply."),
            # Turn 1: the user asked a QUESTION — the manager answers in text
            # without any tool.  This must park (not trigger a dispatch nudge).
            TextTurn("T1 writes hello.py — nothing else."),
            # Turn 2: the user said proceed — real work then complete.
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
        ask_user_seen = asyncio.Event()
        park_seen = asyncio.Event()

        async def _collect() -> None:
            async for ev in event_bus.event_stream(project_id, epic_id):
                events_received.append(ev)
                if isinstance(ev, UserInputRequestedEvent):
                    if ev.question:
                        ask_user_seen.set()
                    else:
                        park_seen.set()

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="yukar",
            git_author_email="yukar@localhost",
        )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            run_task = asyncio.create_task(orch.start(root, project_id, epic_id, run_id))

            # Turn 0 ends in ask_user.
            await asyncio.wait_for(ask_user_seen.wait(), timeout=10.0)

            # The user asks a question (does NOT approve-and-command work).
            orch.inject_message("manager", "what exactly does T1 do?")

            # The manager answers in plain text → the run must park
            # (question-less awaiting_input), NOT dispatch.
            await asyncio.wait_for(park_seen.wait(), timeout=10.0)

            state = await state_repo.get_state(root, project_id, epic_id)
            assert state is not None
            assert state.status == "awaiting_input"
            assert state.pending_question is None, (
                "A conversational park must not fabricate a pending question"
            )

            # The user approves the plan (explicit operation recorded on disk —
            # what POST /plan/approval does; a chat reply alone would leave
            # dispatch gate-rejected), then resumes; the manager dispatches
            # and completes.
            from datetime import UTC, datetime

            from yukar.models.task import PlanApproval, compute_plan_hash
            from yukar.storage import plan_approval_repo, tasks_repo

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
            orch.inject_message("manager", "great — go ahead")
            await asyncio.wait_for(run_task, timeout=30.0)

        await asyncio.wait_for(collector, timeout=5.0)

        event_types = [getattr(ev, "type", None) for ev in events_received]
        assert "run_completed" in event_types
        assert "worker_completed" in event_types

        # The host must never have injected the old dispatch command.
        for text in _fsm_user_texts(root, project_id, epic_id):
            assert _OLD_NUDGE_MARKER not in text


class TestStallNoticeThenPark:
    """E2E: silent end after a host prompt → one notice; silent again → park."""

    async def test_one_notice_then_park(self, tmp_path: Path) -> None:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.models.events import UserInputRequestedEvent
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-STALL"
        run_id = "run-stall-park"

        git_repo = make_git_repo(tmp_path, "myrepo")
        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script = [
            # Turn 0: creates a task (effector) then stops with text.
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Some work",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            TextTurn("Planned. Stopping here."),
            # Turn 1 (stall notice): stalls again — must park, not loop.
            TextTurn("Still thinking."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            return FakeModel(script=list(manager_script))

        park_seen = asyncio.Event()
        events_received: list[Any] = []

        async def _collect() -> None:
            async for ev in event_bus.event_stream(project_id, epic_id):
                events_received.append(ev)
                if isinstance(ev, UserInputRequestedEvent) and not ev.question:
                    park_seen.set()

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="yukar",
            git_author_email="yukar@localhost",
            require_plan_approval=False,
        )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            run_task = asyncio.create_task(orch.start(root, project_id, epic_id, run_id))

            await asyncio.wait_for(park_seen.wait(), timeout=10.0)

            state = await state_repo.get_state(root, project_id, epic_id)
            assert state is not None
            assert state.status == "awaiting_input"
            assert state.pending_question is None

            # Exactly ONE stall notice was sent, and never the old dispatch command.
            user_texts = _fsm_user_texts(root, project_id, epic_id)
            notices = [t for t in user_texts if _STALL_NOTICE_MARKER in t]
            assert len(notices) == 1, f"Expected exactly one stall notice, got: {user_texts}"
            assert all(_OLD_NUDGE_MARKER not in t for t in user_texts)

            # Stop the parked run (user-initiated stop path).
            orch._stopped = True
            run_task.cancel()
            with pytest.raises((asyncio.CancelledError, Exception)):
                await run_task

        await asyncio.wait_for(collector, timeout=5.0)


class TestEffectorResetsNotice:
    """E2E: each productive turn re-arms the one-shot notice — autonomy flows."""

    async def test_multi_batch_autonomy_completes(self, tmp_path: Path) -> None:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.models.events import UserInputRequestedEvent

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-BATCH"
        run_id = "run-batch"

        git_repo = make_git_repo(tmp_path, "myrepo")
        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script = [
            # Turn 0: plan (effector) then stop.  T1 stays todo so the epic
            # keeps runnable work across turn boundaries (the deadlock guard
            # would otherwise end the run before the second notice).
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Some work",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            TextTurn("Planned."),
            # Turn 1 (notice): work again (effector) then stop — the notice
            # one-shot must re-arm instead of parking.
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T2",
                    "title": "More work",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            TextTurn("Added T2."),
            # Turn 2 (notice): close everything out and finish in one turn.
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Some work",
                    "status": "done",
                    "repo": git_repo.name,
                },
            ),
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T2",
                    "title": "More work",
                    "status": "done",
                    "repo": git_repo.name,
                },
            ),
            ToolUseTurn(tool_name="complete_epic", tool_input={}),
            TextTurn("All done."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            return FakeModel(script=list(manager_script))

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
            await asyncio.wait_for(
                orch.start(root, project_id, epic_id, run_id),
                timeout=30.0,
            )

        await asyncio.wait_for(collector, timeout=5.0)

        event_types = [getattr(ev, "type", None) for ev in events_received]
        assert "run_completed" in event_types

        # The run never parked (no question-less UserInputRequestedEvent).
        parks = [
            e
            for e in events_received
            if isinstance(e, UserInputRequestedEvent) and not e.question
        ]
        assert parks == [], "Autonomous multi-batch run must not park"

        # One notice per silent inter-batch boundary (turns 1 and 2).
        user_texts = _fsm_user_texts(root, project_id, epic_id)
        notices = [t for t in user_texts if _STALL_NOTICE_MARKER in t]
        assert len(notices) == 2, f"Expected two stall notices, got: {user_texts}"
        assert all(_OLD_NUDGE_MARKER not in t for t in user_texts)


class TestReadOnlyToolsKeepFlowing:
    """E2E: a notice-prompted turn that only READS still keeps the run alive.

    Read-only investigation (docs, branch diff, repo greps) is real engagement:
    it must not be classified as a silent yield, or a manager/reviewer doing
    multi-turn verification would be parked mid-work.
    """

    async def test_read_only_notice_turn_does_not_park(self, tmp_path: Path) -> None:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.models.events import UserInputRequestedEvent

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-READONLY"
        run_id = "run-readonly"

        git_repo = make_git_repo(tmp_path, "myrepo")
        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script = [
            # Turn 0: plan (effector) then stop.
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Some work",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            TextTurn("Planned."),
            # Turn 1 (notice): READ-ONLY tool use only, then text.  Must keep
            # flowing (next notice), NOT park.
            ToolUseTurn(tool_name="read_epic_docs", tool_input={}),
            TextTurn("Checked the docs; verifying next."),
            # Turn 2 (notice): close out and finish.
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Some work",
                    "status": "done",
                    "repo": git_repo.name,
                },
            ),
            ToolUseTurn(tool_name="complete_epic", tool_input={}),
            TextTurn("All done."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            return FakeModel(script=list(manager_script))

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
            await asyncio.wait_for(orch.start(root, project_id, epic_id, run_id), timeout=30.0)

        await asyncio.wait_for(collector, timeout=5.0)

        event_types = [getattr(ev, "type", None) for ev in events_received]
        assert "run_completed" in event_types
        parks = [
            e
            for e in events_received
            if isinstance(e, UserInputRequestedEvent) and not e.question
        ]
        assert parks == [], "A read-only investigating turn must not be parked"


class TestStopOnLiveAwaitingRun:
    """A user stop on a LIVE awaiting_input run must end idle, not completed.

    stop() unblocks _wait_for_user_input via the __stop__ sentinel and the loop
    returns NORMALLY (no CancelledError), so start() must branch on _stopped —
    otherwise the run was mislabelled completed, its thread resolved, and its
    remaining tasks flipped to blocked.
    """

    async def test_sentinel_stop_sets_idle_and_preserves_tasks(self, tmp_path: Path) -> None:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import state_repo, tasks_repo

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-STOPPARK"
        run_id = "run-stop-park"

        git_repo = make_git_repo(tmp_path, "myrepo")
        await _bootstrap(root, project_id, epic_id, git_repo)

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
            ToolUseTurn(tool_name="ask_user", tool_input={"question": "Proceed?"}),
            TextTurn("Waiting."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            return FakeModel(script=list(manager_script))

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
        )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            run_task = asyncio.create_task(orch.start(root, project_id, epic_id, run_id))

            # Wait for the run to persist awaiting_input.
            for _ in range(100):
                st = await state_repo.get_state(root, project_id, epic_id)
                if st is not None and st.status == "awaiting_input":
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("run never reached awaiting_input")

            # Production stop path: runner.stop() injects the sentinel; the
            # task then finishes NORMALLY (supervisor only cancels after 5s).
            await orch.stop()
            await asyncio.wait_for(run_task, timeout=10.0)

        await asyncio.wait_for(collector, timeout=5.0)

        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None
        assert state.status == "idle", f"Expected idle after stop, got {state.status!r}"
        assert state.pending_question is None

        event_types = [getattr(ev, "type", None) for ev in events_received]
        assert "run_stopped" in event_types
        assert "run_completed" not in event_types, (
            "A user stop must not be mislabelled as run completion"
        )

        # The stopped run is restartable: its task must stay todo, not blocked.
        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        t1 = next(t for t in tf.tasks if t.id == "T1")
        assert t1.status == "todo", f"Task must stay todo after stop, got {t1.status!r}"
