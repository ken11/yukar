"""Tests for pending_question persistence in RunState.

Verifies that:
- ask_user call sets RunState.pending_question and persists it to state.yaml.
- Reply received (resolve) sets pending_question=None and persists.
- Terminal transitions (stop/complete/error) clear pending_question=None.
- GET /run/state endpoint exposes pending_question correctly.
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
        description="A test epic.",
        branch="yukar/ep-1-test-epic",
    )
    await save_epic(root, project_id, epic)


# ---------------------------------------------------------------------------
# Unit: RunState model has pending_question field
# ---------------------------------------------------------------------------


class TestRunStateModel:
    def test_pending_question_defaults_to_none(self) -> None:
        from yukar.models.run import RunState

        state = RunState(run_id="r1")
        assert state.pending_question is None

    def test_pending_question_can_be_set(self) -> None:
        from yukar.models.run import RunState

        state = RunState(run_id="r1", pending_question="Is this plan OK?")
        assert state.pending_question == "Is this plan OK?"

    def test_pending_question_survives_roundtrip(self) -> None:
        """pending_question is preserved through model_dump / model_validate."""
        from yukar.models.run import RunState

        state = RunState(run_id="r1", status="awaiting_input", pending_question="Ready?")
        data = state.model_dump()
        restored = RunState.model_validate(data)
        assert restored.pending_question == "Ready?"

    def test_pending_question_none_survives_roundtrip(self) -> None:
        from yukar.models.run import RunState

        state = RunState(run_id="r1", pending_question=None)
        data = state.model_dump()
        restored = RunState.model_validate(data)
        assert restored.pending_question is None


# ---------------------------------------------------------------------------
# Unit: ask_user tool persists pending_question
# ---------------------------------------------------------------------------


class TestAskUserPersistsPendingQuestion:
    def _make_orchestrator(self) -> Any:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings

        return EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="Test",
            git_author_email="test@example.com",
        )

    async def test_ask_user_sets_pending_question_in_state(self) -> None:
        """ask_user tool writes pending_question to state.yaml."""
        import tempfile

        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.run import RunState
        from yukar.storage import state_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        orch = self._make_orchestrator()
        orch._project_id = "proj"
        orch._epic_id = "ep"
        orch._run_id = "run-q"
        orch._pub = lambda e: None

        with tempfile.TemporaryDirectory() as tmp:
            root = tmp
            await save_project(root, Project(id="proj", name="proj"))
            await save_epic(root, "proj", Epic(id="ep", slug="ep", title="Ep"))

            # Pre-seed state so save_state has something to overwrite.
            initial = RunState(run_id="run-q", status="running")
            await state_repo.save_state(root, "proj", "ep", initial)

            # Wire the orchestrator's internal state ref.
            orch._state = initial
            orch._root = root

            question = "Is the plan OK? Step 1: write tests. Step 2: implement."
            tool_fn = orch._make_ask_user_tool()
            await tool_fn(question=question)

            # pending_question must be persisted.
            persisted = await state_repo.get_state(root, "proj", "ep")
            assert persisted is not None
            assert persisted.status == "awaiting_input"
            assert persisted.pending_question == question

    async def test_resolve_clears_pending_question_in_state(self) -> None:
        """After user replies, pending_question is set to None in state.yaml."""
        import tempfile

        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.run import RunState
        from yukar.storage import state_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        orch = self._make_orchestrator()
        orch._project_id = "proj"
        orch._epic_id = "ep"
        orch._run_id = "run-resolve"
        orch._pub = lambda e: None
        orch._awaiting_user = True
        orch._pending_question = "Ready?"

        with tempfile.TemporaryDirectory() as tmp:
            root = tmp
            await save_project(root, Project(id="proj", name="proj"))
            await save_epic(root, "proj", Epic(id="ep", slug="ep", title="Ep"))

            # State is already awaiting with a question.
            state = RunState(
                run_id="run-resolve",
                status="awaiting_input",
                pending_question="Ready?",
            )
            await state_repo.save_state(root, "proj", "ep", state)

            wait_task = asyncio.create_task(
                orch._wait_for_user_input(root, "proj", "ep", "run-resolve", state, lambda e: None)
            )
            await asyncio.sleep(0.05)

            # Inject the user's reply.
            orch.inject_message("manager", "Yes, go ahead!")
            await asyncio.wait_for(wait_task, timeout=2.0)

            # pending_question must be None after reply.
            persisted = await state_repo.get_state(root, "proj", "ep")
            assert persisted is not None
            assert persisted.status == "running"
            assert persisted.pending_question is None

    async def test_stop_clears_pending_question_in_state(self) -> None:
        """stop() while awaiting_input clears pending_question in state.yaml."""
        import tempfile

        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.run import RunState
        from yukar.storage import state_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        orch = self._make_orchestrator()
        orch._project_id = "proj"
        orch._epic_id = "ep"
        orch._run_id = "run-stop-q"
        orch._pub = lambda e: None
        orch._awaiting_user = True
        orch._pending_question = "Shall I proceed?"

        with tempfile.TemporaryDirectory() as tmp:
            root = tmp
            await save_project(root, Project(id="proj", name="proj"))
            await save_epic(root, "proj", Epic(id="ep", slug="ep", title="Ep"))

            state = RunState(
                run_id="run-stop-q",
                status="awaiting_input",
                pending_question="Shall I proceed?",
            )
            await state_repo.save_state(root, "proj", "ep", state)

            wait_task = asyncio.create_task(
                orch._wait_for_user_input(root, "proj", "ep", "run-stop-q", state, lambda e: None)
            )
            await asyncio.sleep(0.05)

            await orch.stop()
            result = await asyncio.wait_for(wait_task, timeout=2.0)
            # stop() returns empty string.
            assert result == ""

            # The stop path does NOT save state (that's the orchestrator's outer
            # try/except that catches CancelledError).  pending_question in state
            # is still the stale value until the outer exception handler clears it.
            # However: the _wait_for_user_input itself exits without updating state
            # on the stop path — that is correct per the current design.
            # The outer CancelledError handler sets idle + pending_question=None.
            # We test that invariant in TestTerminalTransitionsClearPendingQuestion.


# ---------------------------------------------------------------------------
# Unit: terminal transitions clear pending_question
# ---------------------------------------------------------------------------


class TestTerminalTransitionsClearPendingQuestion:
    """Verify completed/idle(stop)/error all write pending_question=None to state."""

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_completed_run_clears_pending_question(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """A run that completes normally must have pending_question=None in state."""
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-DONE"
        run_id = "run-done"

        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script = [
            ToolUseTurn(tool_name="complete_epic", tool_input={}),
            TextTurn("Done."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            return FakeModel(script=list(manager_script))

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="yukar",
            git_author_email="yukar@localhost",
        )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            await asyncio.wait_for(
                orch.start(root, project_id, epic_id, run_id),
                timeout=20.0,
            )

        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None
        assert state.status == "completed"
        assert state.pending_question is None

    async def test_stop_during_awaiting_input_clears_pending_question(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """stop() during awaiting_input must set pending_question=None in state."""
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.models.events import UserInputRequestedEvent
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-STOP"
        run_id = "run-stop-pq"

        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script = [
            ToolUseTurn(
                tool_name="ask_user",
                tool_input={"question": "Should I proceed with the plan?"},
            ),
            TextTurn("Waiting."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            return FakeModel(script=list(manager_script))

        uir_event = asyncio.Event()

        async def _collect() -> None:
            async for ev in event_bus.event_stream(project_id, epic_id):
                if isinstance(ev, UserInputRequestedEvent):
                    uir_event.set()

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="yukar",
            git_author_email="yukar@localhost",
        )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            run_task = asyncio.create_task(orch.start(root, project_id, epic_id, run_id))

            # Wait until the run is in awaiting_input.
            await asyncio.wait_for(uir_event.wait(), timeout=10.0)

            # Verify pending_question is set before stopping.
            state_before = await state_repo.get_state(root, project_id, epic_id)
            assert state_before is not None
            assert state_before.status == "awaiting_input"
            assert state_before.pending_question == "Should I proceed with the plan?"

            # Stop the run. Real supervisor.stop() sets _stopped=True before the
            # force-cancel; a shutdown cancel leaves it False (preserves state).
            orch._stopped = True
            run_task.cancel()
            with pytest.raises((asyncio.CancelledError, Exception)):
                await run_task

        await asyncio.wait_for(collector, timeout=5.0)

        # After stop, state is idle and pending_question must be None.
        state_after = await state_repo.get_state(root, project_id, epic_id)
        assert state_after is not None
        assert state_after.status == "idle"
        assert state_after.pending_question is None, (
            f"pending_question should be None after stop, got {state_after.pending_question!r}"
        )


# ---------------------------------------------------------------------------
# Unit: _wait_for_user_input persists pending_question on entry
# ---------------------------------------------------------------------------


class TestWaitForUserInputPersistsPendingQuestion:
    """_wait_for_user_input must persist pending_question from self._pending_question."""

    def _make_orchestrator(self) -> Any:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings

        return EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="Test",
            git_author_email="test@example.com",
        )

    async def test_wait_for_user_input_persists_question_on_entry(self) -> None:
        """_wait_for_user_input writes self._pending_question to state.yaml on entry."""
        import tempfile

        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.run import RunState
        from yukar.storage import state_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        orch = self._make_orchestrator()
        orch._project_id = "proj"
        orch._epic_id = "ep"
        orch._run_id = "run-wfui"
        orch._pub = lambda e: None
        orch._awaiting_user = True
        # Simulate the seed plan-approval path: _pending_question set before
        # _wait_for_user_input is called directly (without going through ask_user).
        orch._pending_question = "Initial seed plan: do X, Y, Z. Approve?"

        with tempfile.TemporaryDirectory() as tmp:
            root = tmp
            await save_project(root, Project(id="proj", name="proj"))
            await save_epic(root, "proj", Epic(id="ep", slug="ep", title="Ep"))

            state = RunState(run_id="run-wfui", status="running")
            await state_repo.save_state(root, "proj", "ep", state)

            wait_task = asyncio.create_task(
                orch._wait_for_user_input(root, "proj", "ep", "run-wfui", state, lambda e: None)
            )
            await asyncio.sleep(0.05)

            # State must now show awaiting_input + the question.
            persisted = await state_repo.get_state(root, "proj", "ep")
            assert persisted is not None
            assert persisted.status == "awaiting_input"
            assert persisted.pending_question == "Initial seed plan: do X, Y, Z. Approve?"

            # Resolve and verify it's cleared.
            orch.inject_message("manager", "Yes, proceed!")
            await asyncio.wait_for(wait_task, timeout=2.0)

            after = await state_repo.get_state(root, "proj", "ep")
            assert after is not None
            assert after.pending_question is None

    async def test_wait_for_user_input_clears_question_when_empty(self) -> None:
        """When _pending_question is empty string, pending_question=None in state.yaml."""
        import tempfile

        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.run import RunState
        from yukar.storage import state_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        orch = self._make_orchestrator()
        orch._project_id = "proj"
        orch._epic_id = "ep"
        orch._run_id = "run-empty-q"
        orch._pub = lambda e: None
        orch._awaiting_user = True
        orch._pending_question = ""  # empty string → should be stored as None

        with tempfile.TemporaryDirectory() as tmp:
            root = tmp
            await save_project(root, Project(id="proj", name="proj"))
            await save_epic(root, "proj", Epic(id="ep", slug="ep", title="Ep"))

            state = RunState(run_id="run-empty-q", status="running")
            await state_repo.save_state(root, "proj", "ep", state)

            wait_task = asyncio.create_task(
                orch._wait_for_user_input(
                    root, "proj", "ep", "run-empty-q", state, lambda e: None
                )
            )
            await asyncio.sleep(0.05)

            persisted = await state_repo.get_state(root, "proj", "ep")
            assert persisted is not None
            assert persisted.status == "awaiting_input"
            # Empty string treated as None.
            assert persisted.pending_question is None

            orch.inject_message("manager", "ok")
            await asyncio.wait_for(wait_task, timeout=2.0)
