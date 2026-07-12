"""Tests for turn-end semantics (lifecycle redesign P3).

EVERY ended turn is the agent's yield: the run parks in ``waiting`` (the
user's turn) and the next user message drives exactly one more turn.  The
host never injects a prompt to keep a run going — no stall notices, no
dispatch nudges, no completion tool:

- A turn that ends (tool-using or not) parks the run in ``waiting`` and
  publishes a question-less YourTurnEvent (pure "your turn" signal).
- The ONLY host-authored user prompt is turn-0 initialisation.
- stop() on a live waiting run settles the state as ``waiting`` (restartable),
  never ``completed`` — a conversation run has no completed state.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tests._helpers import make_git_repo, wait_for_run_status

# Legacy host-injected prompts that must NEVER appear again.
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
    """Unit: _park_awaiting_user persists ``waiting`` and signals "your turn"."""

    async def test_park_persists_waiting_and_publishes_your_turn(self, tmp_path: Path) -> None:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.models.epic import Epic
        from yukar.models.events import YourTurnEvent
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
        assert orch.is_parked is True

        persisted = await state_repo.get_state(root, "proj", "ep")
        assert persisted is not None
        assert persisted.status == "waiting"

        uir = [e for e in emitted if isinstance(e, YourTurnEvent)]
        assert len(uir) == 1
        assert uir[0].thread_id == "manager"


class TestEveryTurnParks:
    """E2E: every ended turn parks; a user reply drives exactly one more turn.

    The conversational flow with plain text turns (questions and reports
    live in the message body — no dedicated tools):
    turn 0 presents a plan in the message body and ends → park; the user's
    question is answered in text → park; the user approves (explicit
    operation) and says go → dispatch runs → park.  The host never injects
    a stall notice or a dispatch nudge.
    """

    async def test_plan_question_approve_dispatch_flow(self, tmp_path: Path) -> None:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.models.events import YourTurnEvent
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-CONV"
        run_id = "run-conv-park"

        git_repo = make_git_repo(tmp_path, "myrepo")
        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script = [
            # Turn 0: plan, then present it in the message body and end.
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Write hello.py",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            TextTurn("Plan: T1 writes hello.py. Approve to proceed."),
            # Turn 1: the user asked a question — answer in text, no tools.
            TextTurn("T1 writes hello.py — nothing else."),
            # Turn 2: the user approved and said go — dispatch, then report.
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            TextTurn("T1 done — hello.py written and accepted."),
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
        park_count = 0
        park_events: list[asyncio.Event] = [asyncio.Event(), asyncio.Event(), asyncio.Event()]

        async def _collect() -> None:
            nonlocal park_count
            async for ev in event_bus.event_stream(project_id, epic_id):
                events_received.append(ev)
                if isinstance(ev, YourTurnEvent):
                    if park_count < len(park_events):
                        park_events[park_count].set()
                    park_count += 1

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="yukar",
            git_author_email="yukar@localhost",
        )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            run_task = asyncio.create_task(orch.start(root, project_id, epic_id, run_id))

            # Turn 0 ends → park #1.
            await asyncio.wait_for(park_events[0].wait(), timeout=10.0)
            state = await state_repo.get_state(root, project_id, epic_id)
            assert state is not None
            assert state.status == "waiting"

            # The user asks a question; the tool-less text answer parks again.
            orch.inject_message("manager", "what exactly does T1 do?")
            await asyncio.wait_for(park_events[1].wait(), timeout=10.0)

            # The user approves the plan — an explicit operation recorded on
            # disk (what POST /plan/approval does; a chat reply alone would
            # leave dispatch gate-rejected) — then says go.
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

            # Turn 2 dispatches and ends → park #3.  The run task stays alive.
            await asyncio.wait_for(park_events[2].wait(), timeout=30.0)
            await orch.stop()
            await asyncio.wait_for(run_task, timeout=10.0)

        await asyncio.wait_for(collector, timeout=5.0)

        event_types = [getattr(ev, "type", None) for ev in events_received]
        assert "worker_completed" in event_types
        assert "eval_result" in event_types
        # A conversation run never completes; it parks and is finally stopped.
        assert "run_completed" not in event_types
        assert "run_stopped" in event_types

        # The host never injected any prompt besides turn-0 initialisation:
        # no stall notice, no dispatch nudge.
        user_texts = _fsm_user_texts(root, project_id, epic_id)
        for text in user_texts:
            assert _STALL_NOTICE_MARKER not in text
            assert _OLD_NUDGE_MARKER not in text
        # turn-0 boilerplate + 2 human messages.
        assert len(user_texts) == 3, (
            f"Expected exactly 3 user messages (turn-0 + 2 replies), got: {user_texts}"
        )

        # Final state stays waiting.
        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None
        assert state.status == "waiting"


class TestToolUsingTurnAlsoParks:
    """E2E: a turn that used effector tools STILL parks when it ends.

    Under the old semantics an effector turn re-armed a stall notice and the
    host kept the loop flowing; under P3 the host never continues on its own.
    """

    async def test_effector_turn_parks_without_notice(self, tmp_path: Path) -> None:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.models.events import YourTurnEvent
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-EFFECTOR"
        run_id = "run-effector-park"

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
            TextTurn("Planned. Stopping here."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            return FakeModel(script=list(manager_script))

        park_seen = asyncio.Event()
        events_received: list[Any] = []

        async def _collect() -> None:
            async for ev in event_bus.event_stream(project_id, epic_id):
                events_received.append(ev)
                if isinstance(ev, YourTurnEvent):
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
            assert state.status == "waiting"

            # No stall notice was ever injected — the only user message is
            # the turn-0 initialisation prompt.
            user_texts = _fsm_user_texts(root, project_id, epic_id)
            assert len(user_texts) == 1, f"Expected only the turn-0 prompt, got: {user_texts}"
            assert all(_STALL_NOTICE_MARKER not in t for t in user_texts)
            assert all(_OLD_NUDGE_MARKER not in t for t in user_texts)

            await orch.stop()
            await asyncio.wait_for(run_task, timeout=10.0)

        await asyncio.wait_for(collector, timeout=5.0)


class TestStopOnLiveWaitingRun:
    """A user stop on a LIVE waiting run must settle as waiting, not completed.

    stop() unblocks _wait_for_user_input via the __stop__ sentinel and the loop
    returns NORMALLY (no CancelledError), so start() must branch on _stopped —
    the regression this guards: a stop mislabelled as completion, the thread
    resolved, and remaining tasks flipped to blocked (e771261 lineage).
    """

    async def test_sentinel_stop_keeps_waiting_and_preserves_tasks(self, tmp_path: Path) -> None:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import state_repo, tasks_repo
        from yukar.storage.threads_repo import get_threads

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
            TextTurn("Plan ready — waiting for your approval."),
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

            # Wait for the run to park (persisted waiting).
            await wait_for_run_status(root, project_id, epic_id, "waiting", timeout=10.0)

            # Production stop path: runner.stop() injects the sentinel; the
            # task then finishes NORMALLY (supervisor only cancels after 5s).
            await orch.stop()
            await asyncio.wait_for(run_task, timeout=10.0)

        await asyncio.wait_for(collector, timeout=5.0)

        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None
        assert state.status == "waiting", f"Expected waiting after stop, got {state.status!r}"

        event_types = [getattr(ev, "type", None) for ev in events_received]
        assert "run_stopped" in event_types
        assert "run_completed" not in event_types, (
            "A user stop must not be mislabelled as run completion"
        )

        # The stopped run is restartable: its task must stay todo, not blocked.
        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        t1 = next(t for t in tf.tasks if t.id == "T1")
        assert t1.status == "todo", f"Task must stay todo after stop, got {t1.status!r}"

        # The conversation stays active (no resolved vocabulary for managers).
        threads = await get_threads(root, project_id, epic_id)
        manager = next((t for t in threads.threads if t.id == "manager"), None)
        assert manager is not None
        assert manager.status == "active"
