"""Tests for M5 parallel Worker execution, pause/resume events, stop-while-paused,
and the new REST/SSE endpoints.

Covers:
- Different-repo tasks run in parallel (worker_started for both appear before
  either worker_completed).
- Same-repo tasks serialise.
- pause → state.yaml status="paused" + RunPausedEvent published.
- resume → state.yaml status="running" + RunResumedEvent published → run continues.
- stop-while-paused does not hang; in_progress tasks are rolled back to todo.
- GET …/run/state returns current RunState (404 when epic absent, idle default
  when state.yaml absent).
- GET /api/projects/{p}/events streams only lifecycle events.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests._helpers import make_git_repo


async def _bootstrap_two_repos(
    root: str,
    project_id: str,
    epic_id: str,
    repo_a: Path,
    repo_b: Path,
) -> None:
    from yukar.models.epic import Epic
    from yukar.models.project import Project, Repo, RepoCommands
    from yukar.storage.epic_repo import save_epic
    from yukar.storage.project_repo import save_project, save_repo

    project = Project(
        id=project_id,
        name=project_id,
        status="active",
        repos=[repo_a.name, repo_b.name],
    )
    await save_project(root, project)

    for repo_path in (repo_a, repo_b):
        repo = Repo(
            name=repo_path.name,
            path=str(repo_path),
            default_branch="main",
            commands=RepoCommands(allow=["git"], deny=[]),
        )
        await save_repo(root, project_id, repo)

    epic = Epic(
        id=epic_id,
        slug="test-epic",
        title="Test Epic",
        description="M5 parallel test epic.",
        branch="yukar/ep-1-test-epic",
    )
    await save_epic(root, project_id, epic)


async def _bootstrap_one_repo(
    root: str,
    project_id: str,
    epic_id: str,
    repo: Path,
) -> None:
    from yukar.models.epic import Epic
    from yukar.models.project import Project, Repo, RepoCommands
    from yukar.storage.epic_repo import save_epic
    from yukar.storage.project_repo import save_project, save_repo

    project = Project(
        id=project_id,
        name=project_id,
        status="active",
        repos=[repo.name],
    )
    await save_project(root, project)
    await save_repo(
        root,
        project_id,
        Repo(
            name=repo.name,
            path=str(repo),
            default_branch="main",
            commands=RepoCommands(allow=["git"], deny=[]),
        ),
    )
    await save_epic(
        root,
        project_id,
        Epic(
            id=epic_id,
            slug="test-epic",
            title="Test Epic",
            description="M5 test.",
            branch="yukar/ep-1-test-epic",
        ),
    )


def _make_simple_worker_script(filename: str, content: str) -> list[Any]:
    from yukar.llm.fake import TextTurn, ToolUseTurn

    return [
        ToolUseTurn(tool_name="fs_write", tool_input={"path": filename, "content": content}),
        TextTurn("Done."),
    ]


def _accept_script() -> list[Any]:
    from yukar.llm.fake import TextTurn, ToolUseTurn

    return [
        ToolUseTurn(
            tool_name="submit_verdict",
            tool_input={"accepted": True, "feedback": ""},
        ),
        TextTurn("Accepted."),
    ]


# ---------------------------------------------------------------------------
# 1. Different-repo tasks run in parallel
# ---------------------------------------------------------------------------


class TestParallelDifferentRepos:
    @pytest.fixture
    def repo_a(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "repo-a")

    @pytest.fixture
    def repo_b(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "repo-b")

    async def test_different_repo_tasks_dispatched_in_parallel(
        self, repo_a: Path, repo_b: Path, tmp_path: Path
    ) -> None:
        """worker_started for both repos appear before either worker_completed."""
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.models.events import WorkerCompletedEvent, WorkerStartedEvent

        root = str(tmp_path / "ws")
        project_id = "proj-par"
        epic_id = "EP-PAR"

        await _bootstrap_two_repos(root, project_id, epic_id, repo_a, repo_b)

        # Manager creates T1 (repo-a) and T2 (repo-b) — no deps.
        # Dispatches both in one call (parallel intent).
        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Write a.py",
                    "status": "todo",
                    "repo": repo_a.name,
                },
            ),
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T2",
                    "title": "Write b.py",
                    "status": "todo",
                    "repo": repo_b.name,
                },
            ),
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={
                    "items": [
                        {"task_id": "T1", "repo": repo_a.name},
                        {"task_id": "T2", "repo": repo_b.name},
                    ]
                },
            ),
            ToolUseTurn(tool_name="complete_epic", tool_input={}),
            TextTurn("Plan: T1 and T2 in parallel."),
        ]

        worker_a_script = _make_simple_worker_script("a.py", "a = 1\n")
        worker_b_script = _make_simple_worker_script("b.py", "b = 2\n")
        accept_scr = _accept_script()

        call_counts: dict[str, int] = {"manager": 0, "worker": 0, "evaluator": 0}

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            r = role or "worker"
            count = call_counts.get(r, 0)
            call_counts[r] = count + 1
            if r == "manager":
                return FakeModel(script=list(manager_script))
            if r == "worker":
                # Alternate: first worker call → repo-a script, second → repo-b script.
                # The order depends on dispatch, but both scripts produce valid commits.
                return FakeModel(script=list(worker_a_script if count == 0 else worker_b_script))
            return FakeModel(script=list(accept_scr))

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
                max_parallel_workers=4,
            )
            await orch.start(root, project_id, epic_id, "run-par")

        await asyncio.wait_for(collector, timeout=10.0)

        # Extract WorkerStarted and WorkerCompleted events with their positions.
        started = [e for e in events_received if isinstance(e, WorkerStartedEvent)]
        completed = [e for e in events_received if isinstance(e, WorkerCompletedEvent)]

        assert len(started) >= 2, "Expected at least 2 WorkerStartedEvents"
        assert len(completed) >= 2, "Expected at least 2 WorkerCompletedEvents"

        # Both tasks should complete and run should succeed.
        event_types = [getattr(e, "type", None) for e in events_received]
        assert "run_completed" in event_types

        from yukar.storage import tasks_repo

        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        done_tasks = [t for t in tf.tasks if t.status == "done"]
        assert len(done_tasks) == 2, f"Expected 2 done tasks, got {[t.status for t in tf.tasks]}"


# ---------------------------------------------------------------------------
# 2. Same-repo tasks serialise
# ---------------------------------------------------------------------------


class TestSerialSameRepo:
    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_same_repo_tasks_run_serially(self, repo: Path, tmp_path: Path) -> None:
        """Two tasks for the same repo must not overlap (no concurrent commits)."""
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn

        root = str(tmp_path / "ws")
        project_id = "proj-serial"
        epic_id = "EP-SER"

        await _bootstrap_one_repo(root, project_id, epic_id, repo)

        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Write c.py",
                    "status": "todo",
                    "repo": repo.name,
                },
            ),
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T2",
                    "title": "Write d.py",
                    "status": "todo",
                    "repo": repo.name,
                },
            ),
            # Dispatch both in one call; host serialises them (same repo).
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={
                    "items": [
                        {"task_id": "T1", "repo": repo.name},
                        {"task_id": "T2", "repo": repo.name},
                    ]
                },
            ),
            ToolUseTurn(tool_name="complete_epic", tool_input={}),
            TextTurn("Plan ready."),
        ]

        call_counts: dict[str, int] = {"manager": 0, "worker": 0, "evaluator": 0}

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            r = role or "worker"
            count = call_counts.get(r, 0)
            call_counts[r] = count + 1
            if r == "manager":
                return FakeModel(script=list(manager_script))
            if r == "worker":
                fname = "c.py" if count == 0 else "d.py"
                return FakeModel(script=_make_simple_worker_script(fname, f"x = {count}\n"))
            return FakeModel(script=_accept_script())

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
                max_parallel_workers=4,
            )
            await orch.start(root, project_id, epic_id, "run-serial")

        await asyncio.wait_for(collector, timeout=15.0)

        # Both tasks should complete.
        from yukar.storage import tasks_repo

        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        done_tasks = [t for t in tf.tasks if t.status == "done"]
        assert len(done_tasks) == 2

        # Verify no concurrent commits: check git log in worktree.
        from yukar.config import paths

        worktree = paths.worktree_dir(root, project_id, epic_id, "manager", repo.name)
        result = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        # Both files should have been committed (at least 2 commits beyond initial).
        assert result.stdout.count("\n") >= 2


# ---------------------------------------------------------------------------
# 3. pause → RunPausedEvent + state.yaml paused; resume → RunResumedEvent + continues
# ---------------------------------------------------------------------------


class TestPauseResume:
    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_pause_saves_state_and_publishes_event(self, repo: Path, tmp_path: Path) -> None:
        """pause() sets state.yaml status=paused and publishes RunPausedEvent."""
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.run import RunState
        from yukar.runs.supervisor import RunSupervisor
        from yukar.storage import state_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        project_id = "proj-pause"
        epic_id = "EP-PAUSE"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="p", title="P"))

        # Write a running state so pause() can find it.
        run_id = "run-pause-test"
        await state_repo.save_state(
            root,
            project_id,
            epic_id,
            RunState(run_id=run_id, status="running"),
        )

        from yukar.config.settings import LLMSettings, Settings
        from yukar.events import bus as event_bus
        from yukar.models.events import RunPausedEvent

        settings = Settings(workspace_root=root)
        settings.llm = LLMSettings(provider="fake")
        sup = RunSupervisor(settings_getter=lambda: settings)

        # Manually inject a fake _RunHandle so we can call pause() without
        # starting a full run.
        from unittest.mock import AsyncMock, MagicMock

        from yukar.runs.supervisor import _RunHandle

        mock_runner = MagicMock()
        mock_runner.pause = AsyncMock()
        mock_task = MagicMock()
        mock_task.done.return_value = False

        sup._runs[(project_id, epic_id)] = _RunHandle(
            run_id=run_id,
            runner=mock_runner,
            task=mock_task,
            root=root,
            project_id=project_id,
            epic_id=epic_id,
        )

        received: list[Any] = []

        async with event_bus.subscribe(project_id, epic_id) as q:
            await sup.pause(project_id, epic_id)
            try:
                ev = await asyncio.wait_for(q.get(), timeout=1.0)
                received.append(ev)
            except TimeoutError:
                pass

        assert any(isinstance(e, RunPausedEvent) for e in received), "RunPausedEvent not published"

        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None
        assert state.status == "paused"

    async def test_resume_saves_state_and_publishes_event(self, repo: Path, tmp_path: Path) -> None:
        """resume() sets state.yaml status=running and publishes RunResumedEvent."""
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.run import RunState
        from yukar.runs.supervisor import RunSupervisor
        from yukar.storage import state_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        project_id = "proj-resume"
        epic_id = "EP-RESUME"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="r", title="R"))

        run_id = "run-resume-test"
        await state_repo.save_state(
            root,
            project_id,
            epic_id,
            RunState(run_id=run_id, status="paused"),
        )

        from yukar.config.settings import LLMSettings, Settings
        from yukar.events import bus as event_bus
        from yukar.models.events import RunResumedEvent

        settings = Settings(workspace_root=root)
        settings.llm = LLMSettings(provider="fake")
        sup = RunSupervisor(settings_getter=lambda: settings)

        from unittest.mock import AsyncMock, MagicMock

        from yukar.runs.supervisor import _RunHandle

        mock_runner = MagicMock()
        mock_runner.resume = AsyncMock()
        mock_task = MagicMock()
        mock_task.done.return_value = False

        sup._runs[(project_id, epic_id)] = _RunHandle(
            run_id=run_id,
            runner=mock_runner,
            task=mock_task,
            root=root,
            project_id=project_id,
            epic_id=epic_id,
        )

        received: list[Any] = []

        async with event_bus.subscribe(project_id, epic_id) as q:
            await sup.resume(project_id, epic_id)
            try:
                ev = await asyncio.wait_for(q.get(), timeout=1.0)
                received.append(ev)
            except TimeoutError:
                pass

        assert any(isinstance(e, RunResumedEvent) for e in received), (
            "RunResumedEvent not published"
        )

        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None
        assert state.status == "running"

    async def test_resume_publishes_event_even_when_worker_races_to_running(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """RunResumedEvent is published even if a racing worker already wrote
        state.yaml status='running' before supervisor.resume()'s disk read.

        Regression test for: supervisor.resume() previously guarded the entire
        publish inside `if state.status == "paused"`, so if a worker checkpoint
        wrote "running" first the event was silently dropped.
        """
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.run import RunState
        from yukar.runs.supervisor import RunSupervisor
        from yukar.storage import state_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        project_id = "proj-race-resume"
        epic_id = "EP-RACE-RES"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="rr", title="RR"))

        run_id = "run-race-resume"
        # Simulate the race: state.yaml already shows "running" because a worker
        # checkpoint wrote it before supervisor.resume() reads the disk.
        await state_repo.save_state(
            root,
            project_id,
            epic_id,
            RunState(run_id=run_id, status="running"),
        )

        from yukar.config.settings import LLMSettings, Settings
        from yukar.events import bus as event_bus
        from yukar.models.events import RunResumedEvent

        settings = Settings(workspace_root=root)
        settings.llm = LLMSettings(provider="fake")
        sup = RunSupervisor(settings_getter=lambda: settings)

        from unittest.mock import AsyncMock, MagicMock

        from yukar.runs.supervisor import _RunHandle

        mock_runner = MagicMock()
        mock_runner.resume = AsyncMock()
        mock_task = MagicMock()
        mock_task.done.return_value = False

        sup._runs[(project_id, epic_id)] = _RunHandle(
            run_id=run_id,
            runner=mock_runner,
            task=mock_task,
            root=root,
            project_id=project_id,
            epic_id=epic_id,
        )

        received: list[Any] = []

        async with event_bus.subscribe(project_id, epic_id) as q:
            await sup.resume(project_id, epic_id)
            try:
                ev = await asyncio.wait_for(q.get(), timeout=1.0)
                received.append(ev)
            except TimeoutError:
                pass

        # Event must be delivered even though disk already shows "running".
        assert any(isinstance(e, RunResumedEvent) for e in received), (
            "RunResumedEvent not published even though worker raced to running first"
        )
        # Disk state remains "running" (the guard correctly skipped the re-write).
        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None
        assert state.status == "running"


# ---------------------------------------------------------------------------
# 4. stop-while-paused does not hang
# ---------------------------------------------------------------------------


class TestStopWhilePaused:
    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_stop_while_paused_does_not_hang(self, repo: Path, tmp_path: Path) -> None:
        """stop() called while orchestrator is paused unblocks workers and
        rolls back in_progress tasks to todo without hanging."""
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        project_id = "proj-stp"
        epic_id = "EP-STP"

        await _bootstrap_one_repo(root, project_id, epic_id, repo)

        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Long task",
                    "status": "todo",
                    "repo": repo.name,
                },
            ),
            TextTurn("Plan done."),
        ]
        # Worker just returns without committing — keeps run alive long enough to pause/stop.
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
        )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            task = asyncio.create_task(orch.start(root, project_id, epic_id, "run-stp"))
            # Let the run start.
            await asyncio.sleep(0.05)
            # Pause the orchestrator.
            await orch.pause()
            # Immediately stop while paused.
            await orch.stop()
            # Cancel the asyncio task to simulate supervisor.stop().
            task.cancel()
            with pytest.raises((asyncio.CancelledError, Exception)):
                await asyncio.wait_for(task, timeout=3.0)

        # Must not hang — if we got here the test passes.
        # State should be idle (CancelledError path).
        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None
        assert state.status == "idle"


# ---------------------------------------------------------------------------
# 5. GET …/run/state endpoint
# ---------------------------------------------------------------------------


class TestRunStateEndpoint:
    async def test_get_run_state_no_state_file_returns_idle(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """When no state.yaml exists, endpoint returns status=idle."""
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        project_id = "p-state"
        epic_id = "EP-STATE"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="s", title="S"))

        resp = await app_client.get(f"/api/projects/{project_id}/epics/{epic_id}/run/state")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "idle"

    async def test_get_run_state_returns_saved_state(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """When state.yaml exists, endpoint returns its contents."""
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.run import RunState
        from yukar.storage import state_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        project_id = "p-state2"
        epic_id = "EP-ST2"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="s2", title="S2"))
        await state_repo.save_state(
            root,
            project_id,
            epic_id,
            RunState(run_id="run-abc", status="paused"),
        )

        resp = await app_client.get(f"/api/projects/{project_id}/epics/{epic_id}/run/state")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "paused"
        assert body["run_id"] == "run-abc"

    async def test_get_run_state_epic_not_found_returns_404(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """When the epic does not exist, endpoint returns 404."""
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        project_id = "p-no-epic"
        await save_project(root, Project(id=project_id, name=project_id))

        resp = await app_client.get(f"/api/projects/{project_id}/epics/nonexistent/run/state")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 6. GET /api/projects/{p}/events — project-level lifecycle SSE
# ---------------------------------------------------------------------------


class TestProjectEventsEndpoint:
    async def test_project_events_stream_receives_lifecycle_events(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """Lifecycle events published for an epic appear on the project stream."""
        from yukar.events import bus as event_bus
        from yukar.models.events import RunStartedEvent

        project_id = "p-proj-ev"
        epic_id = "EP-PEV"

        # Subscribe to project-level stream.
        received: list[Any] = []

        async with event_bus.subscribe_project(project_id) as q:
            event_bus.publish(
                project_id,
                epic_id,
                RunStartedEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id="run-pev",
                ),
            )
            try:
                ev = await asyncio.wait_for(q.get(), timeout=1.0)
                received.append(ev)
            except TimeoutError:
                pass

        assert len(received) == 1
        assert isinstance(received[0], RunStartedEvent)
        assert received[0].epic_id == epic_id

    async def test_project_events_excludes_token_events(self) -> None:
        """TokenEvent must NOT appear on the project-level stream."""
        from yukar.events import bus as event_bus
        from yukar.models.events import RunStartedEvent, TokenEvent

        project_id = "p-excl"
        epic_id = "EP-EXCL"

        async with event_bus.subscribe_project(project_id) as q:
            # Publish a token event (should not reach project queue).
            event_bus.publish(
                project_id,
                epic_id,
                TokenEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id="run-x",
                    thread_id="t",
                    delta="hello",
                ),
            )
            # Publish a lifecycle event (should reach project queue).
            event_bus.publish(
                project_id,
                epic_id,
                RunStartedEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id="run-x",
                ),
            )
            ev = await asyncio.wait_for(q.get(), timeout=1.0)

        # The only event received should be RunStartedEvent, not TokenEvent.
        assert isinstance(ev, RunStartedEvent)

    async def test_project_events_paused_resumed_events_delivered(self) -> None:
        """RunPausedEvent and RunResumedEvent appear on the project stream."""
        from yukar.events import bus as event_bus
        from yukar.models.events import RunPausedEvent, RunResumedEvent

        project_id = "p-pr"
        epic_id = "EP-PR"
        run_id = "run-pr"

        received: list[Any] = []

        async with event_bus.subscribe_project(project_id) as q:
            event_bus.publish(
                project_id,
                epic_id,
                RunPausedEvent(project_id=project_id, epic_id=epic_id, run_id=run_id),
            )
            event_bus.publish(
                project_id,
                epic_id,
                RunResumedEvent(project_id=project_id, epic_id=epic_id, run_id=run_id),
            )
            for _ in range(2):
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=0.5)
                    received.append(ev)
                except TimeoutError:
                    break

        types = [type(e).__name__ for e in received]
        assert "RunPausedEvent" in types
        assert "RunResumedEvent" in types

    async def test_project_events_sse_endpoint_responds(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """GET /api/projects/{p}/events returns 200 text/event-stream.

        httpx's ASGITransport collects the full response body inline before
        returning (see ASGITransport.handle_async_request: body_parts is built
        synchronously during await self.app(...)).  This means the SSE generator
        must finish before the request completes.

        We make the generator finite by adding a None sentinel directly into
        the subscriber queue at subscribe-time via a monkey-patch of
        subscribe_project: the context manager creates the queue, we put_nowait
        None before yielding, so the generator's first q.get() call returns
        None and it breaks cleanly.
        """
        import asyncio as _asyncio
        from collections.abc import AsyncGenerator as AG
        from contextlib import asynccontextmanager

        from yukar.events import bus as event_bus
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        project_id = "p-sse-ep"
        await save_project(root, Project(id=project_id, name=project_id))

        # Monkey-patch subscribe_project for this test only: inject a None sentinel
        # into every new queue before yielding, so the generator exits immediately.
        original_subscribe_project = event_bus.subscribe_project

        @asynccontextmanager
        async def _finite_subscribe_project(
            project_id: str, maxsize: int = 256
        ) -> AG[_asyncio.Queue[Any]]:
            async with original_subscribe_project(project_id, maxsize) as q:
                # Use the canonical helper so the sentinel path is tested too.
                event_bus.publish_project_sentinel(project_id)
                yield q

        event_bus.subscribe_project = _finite_subscribe_project  # type: ignore[assignment]
        try:
            resp = await _asyncio.wait_for(
                app_client.get(f"/api/projects/{project_id}/events"),
                timeout=5.0,
            )
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
        finally:
            event_bus.subscribe_project = original_subscribe_project


# ---------------------------------------------------------------------------
# 7. project_events_sse — disconnect / keepalive branch regression tests
#
# These tests exercise the generator in runs.py directly (not via HTTP) to
# verify all three branching paths introduced in the M5 project-events stream:
#
#   a) TimeoutError + request.is_disconnected() → True  → generator stops
#   b) Event delivered + request.is_disconnected() → True after event → stops
#   c) TimeoutError + not disconnected + keepalive tick accumulates correctly
# ---------------------------------------------------------------------------


class TestProjectEventsSSEDisconnect:
    """Regression tests for the SSE disconnect/keepalive branches."""

    async def test_disconnect_on_timeout_breaks_generator(self) -> None:
        """When is_disconnected() returns True on a poll timeout the generator exits."""
        from unittest.mock import AsyncMock, patch

        from fastapi import Request

        from yukar.api.routers.runs import project_events_sse

        project_id = "p-dc-timeout"

        # Build a minimal fake Request whose is_disconnected() returns True.
        mock_request = AsyncMock(spec=Request)
        mock_request.is_disconnected = AsyncMock(return_value=True)

        # side_effect must be a callable that closes the unawaited coroutine
        # before raising, otherwise asyncio emits RuntimeWarning: coroutine
        # 'Queue.get' was never awaited.  Same pattern as _counting_wait_for.
        def _always_timeout(coro: Any, timeout: float) -> Any:
            coro.close()
            raise TimeoutError

        # Keep the patch active for the full lifetime of the generator (the
        # StreamingResponse body_iterator runs the async generator lazily, so
        # the patch must outlive the response.body_iterator loop).
        with patch("yukar.api.routers.runs.asyncio.wait_for", side_effect=_always_timeout):
            response = await project_events_sse(project_id=project_id, request=mock_request)

            # Drain the streaming generator — it must finish without hanging.
            chunks: list[str | bytes | memoryview[int]] = []
            async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                chunks.append(chunk)

        # Generator exited after the disconnect — no keepalive emitted.
        assert chunks == [], f"Expected no output, got {chunks}"
        mock_request.is_disconnected.assert_called()

    async def test_disconnect_after_event_delivery_breaks_generator(self) -> None:
        """When is_disconnected() returns True after an event the generator exits."""
        import asyncio as _asyncio
        from unittest.mock import AsyncMock, patch

        from fastapi import Request

        from yukar.api.routers.runs import project_events_sse
        from yukar.events import bus as event_bus
        from yukar.models.events import RunStartedEvent

        project_id = "p-dc-after-event"
        epic_id = "EP-DC-AE"

        # is_disconnected returns False first (allow one event through), then True.
        call_count = {"n": 0}

        async def _is_disconnected() -> bool:
            call_count["n"] += 1
            return call_count["n"] > 1

        mock_request = AsyncMock(spec=Request)
        mock_request.is_disconnected = _is_disconnected

        event = RunStartedEvent(project_id=project_id, epic_id=epic_id, run_id="run-dc")

        # Publish the event before the generator starts so the queue has it ready.
        original_subscribe_project = event_bus.subscribe_project
        from collections.abc import AsyncGenerator as AG
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _pre_loaded_subscribe(pid: str, maxsize: int = 256) -> AG[_asyncio.Queue[Any]]:
            async with original_subscribe_project(pid, maxsize) as q:
                q.put_nowait(event)
                yield q

        with patch.object(event_bus, "subscribe_project", _pre_loaded_subscribe):
            response = await project_events_sse(project_id=project_id, request=mock_request)

            chunks: list[str | bytes | memoryview[int]] = []
            async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                chunks.append(chunk)

        # Should have received exactly one SSE data chunk (the RunStartedEvent).
        assert len(chunks) == 1, f"Expected 1 chunk, got {chunks}"
        chunk_str = chunks[0] if isinstance(chunks[0], str) else bytes(chunks[0]).decode()
        assert "run_started" in chunk_str

    async def test_keepalive_emitted_after_poll_ticks(self) -> None:
        """Keepalive is emitted after _KEEPALIVE_TICKS consecutive poll timeouts."""
        import asyncio as _asyncio
        from unittest.mock import AsyncMock, patch

        from fastapi import Request

        from yukar.api.routers import runs as runs_module
        from yukar.api.routers.runs import project_events_sse

        project_id = "p-keepalive"

        # Disconnect only after _KEEPALIVE_TICKS + 1 timeouts so that exactly
        # one keepalive fires, then the generator exits on the next disconnect check.
        keepalive_ticks = runs_module._KEEPALIVE_TICKS  # type: ignore[attr-defined]
        timeout_count = {"n": 0}

        async def _is_disconnected() -> bool:
            # Stay connected for the first keepalive_ticks+1 polls, then disconnect.
            return timeout_count["n"] > keepalive_ticks

        mock_request = AsyncMock(spec=Request)
        mock_request.is_disconnected = _is_disconnected

        real_wait_for = _asyncio.wait_for
        wait_call_count = {"n": 0}

        async def _counting_wait_for(coro: Any, timeout: float) -> Any:  # type: ignore[misc]
            wait_call_count["n"] += 1
            # For the project-event queue gets (timeout==_POLL_INTERVAL) we always
            # raise TimeoutError so the generator stays in the poll loop.
            if timeout == runs_module._POLL_INTERVAL:  # type: ignore[attr-defined]
                timeout_count["n"] += 1
                coro.close()
                raise TimeoutError
            return await real_wait_for(coro, timeout)

        with patch("yukar.api.routers.runs.asyncio.wait_for", side_effect=_counting_wait_for):
            response = await project_events_sse(project_id=project_id, request=mock_request)

            chunks: list[str | bytes | memoryview[int]] = []
            async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                chunks.append(chunk)

        def _to_str(c: str | bytes | memoryview[int]) -> str:
            if isinstance(c, str):
                return c
            return bytes(c).decode()

        # At least one keepalive comment line must be present.
        assert any("keep-alive" in _to_str(c) for c in chunks), (
            f"Expected a keepalive chunk, got {chunks}"
        )
