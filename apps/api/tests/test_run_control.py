"""Wave 1 run-control / thread-layer tests.

Covers:
1. Non-manager thread read-only: POST …/messages with role=user returns 403
   for worker/evaluator threads; manager thread still accepts.
2. Continuation run: start_or_inject starts a new run when no run is active;
   inject_hitl_message is called when a run is already active.
3. Reconcile on startup: state.yaml with running/paused status is settled
   into 'waiting' by recover_interrupted_runs; waiting is preserved.
4. waiting/error → start_run (POST /run) is allowed (no 409).
5. Index race guard: _ensure_repos_indexed triggers reindex for unindexed repos.
6. EpicOrchestrator accepts seed_prompt / is_continuation kwargs.
7. Legacy RunState statuses (idle/awaiting_input/interrupted) coerce to 'waiting'.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._helpers import run_until_parked

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _write_project_epic(
    root: str,
    project_id: str = "proj",
    epic_id: str = "EP-1",
) -> None:
    from yukar.models.epic import Epic
    from yukar.models.project import Project
    from yukar.storage.epic_repo import save_epic
    from yukar.storage.project_repo import save_project

    await save_project(root, Project(id=project_id, name=project_id))
    await save_epic(
        root,
        project_id,
        Epic(id=epic_id, slug="test", title="Test", branch="yukar/ep-1-test"),
    )


async def _register_thread(
    root: str,
    project_id: str,
    epic_id: str,
    thread_id: str,
    role: str = "worker",
) -> None:
    from typing import Literal, cast

    from yukar.models.thread import ThreadEntry
    from yukar.storage import threads_repo

    ThreadRole = Literal["manager", "worker", "evaluator", "user"]
    entry = ThreadEntry(id=thread_id, title=f"{role} thread", role=cast(ThreadRole, role))
    await threads_repo.add_thread(root, project_id, epic_id, entry)


# ---------------------------------------------------------------------------
# 1. Non-manager thread read-only (403)
# ---------------------------------------------------------------------------


class TestNonManagerThreadReadOnly:
    """POST .../threads/{thread_id}/messages with role=user must return 403
    for worker and evaluator threads.  Manager thread must still succeed."""

    async def test_worker_thread_returns_403(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)
        await _register_thread(root, pid, eid, "wk-001", role="worker")

        resp = await app_client.post(
            f"/api/projects/{pid}/epics/{eid}/threads/wk-001/messages",
            json={"content": "hello", "role": "user"},
        )
        assert resp.status_code == 403, resp.text
        assert "read-only" in resp.json()["detail"].lower()

    async def test_evaluator_thread_returns_403(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)
        await _register_thread(root, pid, eid, "ev-001", role="evaluator")

        resp = await app_client.post(
            f"/api/projects/{pid}/epics/{eid}/threads/ev-001/messages",
            json={"content": "hello", "role": "user"},
        )
        assert resp.status_code == 403, resp.text

    async def test_manager_thread_accepted(self, app_client: Any, tmp_workspace: Path) -> None:
        """Manager thread must still accept user messages (and auto-start continuation)."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)
        await _register_thread(root, pid, eid, "manager", role="manager")

        # Patch start_or_inject so no real run is launched.
        with patch("yukar.api.routers.threads.get_run_supervisor") as mock_get_sup:
            fake_sup = MagicMock()
            fake_sup.start_or_inject = AsyncMock(return_value=False)
            mock_get_sup.return_value = fake_sup

            resp = await app_client.post(
                f"/api/projects/{pid}/epics/{eid}/threads/manager/messages",
                json={"content": "continue please", "role": "user"},
            )
        assert resp.status_code == 201, resp.text

    async def test_assistant_role_rejected_422(self, app_client: Any, tmp_workspace: Path) -> None:
        """assistant-role POST must be rejected with 422 (FSM is the sole writer)."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)
        await _register_thread(root, pid, eid, "wk-001", role="worker")

        resp = await app_client.post(
            f"/api/projects/{pid}/epics/{eid}/threads/wk-001/messages",
            json={"content": "I implemented the task.", "role": "assistant"},
        )
        # assistant messages are blocked — FSM is the sole writer for agent threads.
        assert resp.status_code == 422, resp.text
        assert "only user" in resp.json()["detail"].lower()

    async def test_assistant_role_on_manager_thread_rejected_422(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """assistant-role POST to manager thread must also return 422."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)
        await _register_thread(root, pid, eid, "manager", role="manager")

        resp = await app_client.post(
            f"/api/projects/{pid}/epics/{eid}/threads/manager/messages",
            json={"content": "I implemented the task.", "role": "assistant"},
        )
        assert resp.status_code == 422, resp.text
        assert "only user" in resp.json()["detail"].lower()

    async def test_manager_message_budget_exceeded_returns_409(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """When start_or_inject raises RuntimeError (budget/race), the router
        must return 409, not 500."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)
        await _register_thread(root, pid, eid, "manager", role="manager")

        with patch("yukar.api.routers.threads.get_run_supervisor") as mock_get_sup:
            fake_sup = MagicMock()
            fake_sup.start_or_inject = AsyncMock(side_effect=RuntimeError("Budget limit reached"))
            mock_get_sup.return_value = fake_sup

            resp = await app_client.post(
                f"/api/projects/{pid}/epics/{eid}/threads/manager/messages",
                json={"content": "add more features", "role": "user"},
            )
        assert resp.status_code == 409, resp.text
        assert "Budget limit reached" in resp.json()["detail"]

    async def test_manager_message_run_already_active_returns_409(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """RuntimeError('Run already active') from start_or_inject → 409."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)
        await _register_thread(root, pid, eid, "manager", role="manager")

        with patch("yukar.api.routers.threads.get_run_supervisor") as mock_get_sup:
            fake_sup = MagicMock()
            fake_sup.start_or_inject = AsyncMock(
                side_effect=RuntimeError("Run already active for epic EP-1")
            )
            mock_get_sup.return_value = fake_sup

            resp = await app_client.post(
                f"/api/projects/{pid}/epics/{eid}/threads/manager/messages",
                json={"content": "hello", "role": "user"},
            )
        assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------------------
# 1b. post_message epic-existence + 409 message-ordering guards
# ---------------------------------------------------------------------------


class TestPostMessageEpicGuard:
    """post_message must validate the epic exists before persisting anything, and
    must not commit the user message when start_or_inject rejects it (409)."""

    async def test_nonexistent_epic_returns_404_and_creates_no_dirs(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """Posting to an epic that does not exist must 404 *before* any
        session/state directory is created (no orphaned dirs)."""
        from yukar.config import paths
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        pid, eid = "proj", "EP-GHOST"
        # Project exists, epic does not.
        await save_project(root, Project(id=pid, name=pid))

        resp = await app_client.post(
            f"/api/projects/{pid}/epics/{eid}/threads/manager/messages",
            json={"content": "hello", "role": "user"},
        )
        assert resp.status_code == 404, resp.text
        # No session directory must have been materialised for the phantom epic.
        assert not paths.session_dir(root, pid, eid).exists()
        assert not paths.messages_dir(root, pid, eid, "manager").exists()

    async def test_409_from_start_or_inject_leaves_no_committed_message(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """When start_or_inject raises (budget/arbiter/concurrent → 409), the
        user message must NOT be persisted to the Strands session — otherwise the
        next continuation run would silently consume a message the client was
        told failed."""
        from yukar.storage import session_store

        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)
        await _register_thread(root, pid, eid, "manager", role="manager")

        with patch("yukar.api.routers.threads.get_run_supervisor") as mock_get_sup:
            fake_sup = MagicMock()
            fake_sup.start_or_inject = AsyncMock(side_effect=RuntimeError("Budget limit reached"))
            mock_get_sup.return_value = fake_sup

            resp = await app_client.post(
                f"/api/projects/{pid}/epics/{eid}/threads/manager/messages",
                json={"content": "add more features", "role": "user"},
            )
        assert resp.status_code == 409, resp.text

        # The rejected message must not be in the manager session store.
        messages = session_store.list_messages(root, pid, eid, "manager")
        assert messages == [], f"Expected no committed messages after 409; got {len(messages)}"

    async def test_router_does_not_write_manager_message_fsm_is_sole_writer(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """The router must NOT write the user message to the session store.

        FSM is the sole writer (single-writer invariant).  The router only
        hands off the text to start_or_inject; the message is persisted by
        stream_async on the next manager turn.  Immediately after the POST the
        session store must be empty for the manager thread.
        """
        from yukar.storage import session_store

        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)
        await _register_thread(root, pid, eid, "manager", role="manager")

        with patch("yukar.api.routers.threads.get_run_supervisor") as mock_get_sup:
            fake_sup = MagicMock()
            fake_sup.start_or_inject = AsyncMock(return_value=False)
            mock_get_sup.return_value = fake_sup

            resp = await app_client.post(
                f"/api/projects/{pid}/epics/{eid}/threads/manager/messages",
                json={"content": "please add a /health endpoint", "role": "user"},
            )
        assert resp.status_code == 201, resp.text

        # Router must NOT have written the message — FSM writes it later.
        messages = session_store.list_messages(root, pid, eid, "manager")
        assert messages == [], (
            f"Router wrote {len(messages)} message(s) to the session store; "
            "FSM must be the sole writer"
        )


# ---------------------------------------------------------------------------
# 2. Continuation run via start_or_inject
# ---------------------------------------------------------------------------


class TestStartOrInject:
    """RunSupervisor.start_or_inject routes correctly."""

    async def test_inject_when_run_is_active(self, tmp_path: Path) -> None:
        """When a run is active, start_or_inject must call inject_hitl_message."""
        from yukar.runs.supervisor import RunSupervisor

        sup = RunSupervisor()
        # Simulate an active run by injecting a fake _RunHandle.
        fake_runner = MagicMock()
        fake_runner.inject_message = MagicMock()
        task = asyncio.create_task(asyncio.sleep(100))
        from yukar.runs.supervisor import _RunHandle

        sup._runs[("p", "EP-1")] = _RunHandle(
            run_id="run-x",
            runner=fake_runner,
            task=task,
            root=str(tmp_path),
            project_id="p",
            epic_id="EP-1",
        )
        result = await sup.start_or_inject(str(tmp_path), "p", "EP-1", "manager", "hello")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert result is True
        fake_runner.inject_message.assert_called_once_with("manager", "hello")

    async def test_starts_continuation_when_no_run(self, tmp_path: Path) -> None:
        """When no run is active, start_or_inject must call start_continuation."""
        from yukar.runs.supervisor import RunSupervisor

        sup = RunSupervisor()
        with patch.object(
            sup, "start_continuation", new_callable=AsyncMock, return_value="run-new"
        ) as mock_cont:
            result = await sup.start_or_inject(
                str(tmp_path), "p", "EP-1", "manager", "please revise T1"
            )
        assert result is False
        mock_cont.assert_awaited_once_with(
            str(tmp_path),
            "p",
            "EP-1",
            seed_prompt="please revise T1",
            manager_thread_id="manager",
            agent_role="manager",
            review_context="",
        )

    async def test_start_continuation_leaves_epic_status_untouched(
        self, tmp_path: Path
    ) -> None:
        """start_continuation never writes epic.yaml.status (user-owned 1-bit)."""
        from yukar.models.epic import Epic
        from yukar.runs.supervisor import RunSupervisor
        from yukar.storage.epic_repo import get_epic, save_epic

        root = str(tmp_path / "ws")
        pid, eid = "proj", "EP-1"
        epic = Epic(id=eid, slug="s", title="T")
        await save_epic(root, pid, epic)

        sup = RunSupervisor()

        # Patch _make_continuation_runner to return a runner whose start is instant.
        async def _instant_start(root_: str, project_id: str, epic_id: str, run_id: str) -> None:
            pass

        from yukar.runs.runner import DummyRunner

        dummy = DummyRunner()
        with (
            patch.object(dummy, "start", side_effect=_instant_start),
            patch.object(sup, "_make_continuation_runner", return_value=dummy),
        ):
            await sup.start_continuation(root, pid, eid, seed_prompt="fix this")
            await sup._runs[(pid, eid)].task

        loaded = await get_epic(root, pid, eid)
        assert loaded is not None
        assert loaded.status == "open"


# ---------------------------------------------------------------------------
# 3. Startup recovery settles crashed running/paused runs into 'waiting'
# ---------------------------------------------------------------------------


class TestRecoverInterrupted:
    async def test_running_state_becomes_waiting(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from yukar.models.run import RunState
        from yukar.runs.recovery import recover_interrupted_runs
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        pid, eid = "proj", "EP-1"
        state = RunState(
            run_id="run-abc",
            status="running",
            started_at=datetime.now(UTC),
        )
        await state_repo.save_state(root, pid, eid, state)

        count = await recover_interrupted_runs(root)
        assert count == 1

        saved = await state_repo.get_state(root, pid, eid)
        assert saved is not None
        assert saved.status == "waiting"

    async def test_paused_state_becomes_waiting(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from yukar.models.run import RunState
        from yukar.runs.recovery import recover_interrupted_runs
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        pid, eid = "proj", "EP-2"
        state = RunState(
            run_id="run-def",
            status="paused",
            started_at=datetime.now(UTC),
        )
        await state_repo.save_state(root, pid, eid, state)

        count = await recover_interrupted_runs(root)
        assert count == 1
        saved = await state_repo.get_state(root, pid, eid)
        assert saved is not None
        assert saved.status == "waiting"

    async def test_waiting_state_is_preserved(self, tmp_path: Path) -> None:
        """waiting runs must NOT be reconciled (not counted) on restart.

        The run is parked — no in-flight work.  After restart the user's
        reply triggers start_or_inject → start_continuation which resumes
        cleanly from the preserved session and state.  Legacy state files
        (awaiting_input) are read back as waiting by the model validator and
        preserved the same way.
        """
        from datetime import UTC, datetime

        from yukar.models.run import RunState
        from yukar.runs.recovery import recover_interrupted_runs
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        pid, eid = "proj", "EP-3"
        state = RunState(
            run_id="run-ghi",
            status="waiting",
            started_at=datetime.now(UTC),
        )
        await state_repo.save_state(root, pid, eid, state)

        # waiting is not counted in the reconciled total (it is not modified).
        count = await recover_interrupted_runs(root)
        assert count == 0

        saved = await state_repo.get_state(root, pid, eid)
        assert saved is not None
        assert saved.status == "waiting"

    async def test_waiting_and_running_coexist(self, tmp_path: Path) -> None:
        """When both waiting and running runs exist in different epics,
        only the running run is reconciled; the waiting run is untouched."""
        from datetime import UTC, datetime

        from yukar.models.run import RunState
        from yukar.runs.recovery import recover_interrupted_runs
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        pid = "proj"

        # waiting epic — must be preserved.
        state_ai = RunState(
            run_id="run-ai",
            status="waiting",
            started_at=datetime.now(UTC),
        )
        await state_repo.save_state(root, pid, "EP-AI", state_ai)

        # running epic — must be reconciled.
        state_run = RunState(
            run_id="run-run",
            status="running",
            started_at=datetime.now(UTC),
        )
        await state_repo.save_state(root, pid, "EP-RUN", state_run)

        count = await recover_interrupted_runs(root)
        assert count == 1  # only the running run was reconciled

        saved_ai = await state_repo.get_state(root, pid, "EP-AI")
        assert saved_ai is not None
        assert saved_ai.status == "waiting"

        saved_run = await state_repo.get_state(root, pid, "EP-RUN")
        assert saved_run is not None
        assert saved_run.status == "waiting"

    async def test_completed_state_is_not_touched(self, tmp_path: Path) -> None:
        from yukar.models.run import RunState
        from yukar.runs.recovery import recover_interrupted_runs
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        pid, eid = "proj", "EP-4"
        state = RunState(run_id="run-jkl", status="completed")
        await state_repo.save_state(root, pid, eid, state)

        count = await recover_interrupted_runs(root)
        assert count == 0
        saved = await state_repo.get_state(root, pid, eid)
        assert saved is not None
        assert saved.status == "completed"

    async def test_error_state_is_not_touched(self, tmp_path: Path) -> None:
        from yukar.models.run import RunState
        from yukar.runs.recovery import recover_interrupted_runs
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        pid, eid = "proj", "EP-5"
        state = RunState(run_id="run-mno", status="error")
        await state_repo.save_state(root, pid, eid, state)

        count = await recover_interrupted_runs(root)
        assert count == 0


# ---------------------------------------------------------------------------
# 4. POST /run with no live run is allowed (any prior outcome)
# ---------------------------------------------------------------------------


class TestStartRunFromTerminalState:
    async def test_start_run_when_not_running_is_allowed(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """An open epic with no live run → POST /run must return 202 (not 409),
        regardless of how the previous run ended (the run outcome never touches
        the epic status)."""
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        pid, eid = "p2", "EP-2"
        await save_project(root, Project(id=pid, name=pid))
        await save_epic(
            root,
            pid,
            Epic(id=eid, slug="s2", title="T2"),
        )

        resp = await app_client.post(f"/api/projects/{pid}/epics/{eid}/run")
        # 202 = started; supervisor is backed by DummyRunner in test client
        assert resp.status_code == 202, resp.text

    async def test_start_run_409_when_already_running(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """Only an already-running run must produce 409."""
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.runs.supervisor import get_supervisor
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        pid, eid = "p3", "EP-3"
        await save_project(root, Project(id=pid, name=pid))
        await save_epic(root, pid, Epic(id=eid, slug="s3", title="T3"))

        # Simulate an active run in the supervisor.
        sup = get_supervisor()
        dummy_task = asyncio.create_task(asyncio.sleep(100))
        from yukar.runs.runner import DummyRunner
        from yukar.runs.supervisor import _RunHandle

        sup._runs[(pid, eid)] = _RunHandle(
            run_id="run-active",
            runner=DummyRunner(),
            task=dummy_task,
            root=root,
            project_id=pid,
            epic_id=eid,
        )

        resp = await app_client.post(f"/api/projects/{pid}/epics/{eid}/run")
        dummy_task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await dummy_task
        del sup._runs[(pid, eid)]

        assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------------------
# 5. Index race guard
# ---------------------------------------------------------------------------


class TestEnsureReposIndexed:
    async def test_triggers_full_reindex_for_unindexed_enabled_repo(
        self, tmp_path: Path
    ) -> None:
        """_ensure_repos_indexed must call reindex_repo(full=True) for an unindexed repo."""
        from yukar.models.project import Project, Repo, RepoIndex
        from yukar.runs.supervisor import _ensure_repos_indexed
        from yukar.storage.project_repo import save_project, save_repo

        root = str(tmp_path / "ws")
        pid = "proj"
        repo_path = tmp_path / "myrepo"
        repo_path.mkdir()

        # Save a repo with index.enabled=True.
        await save_project(root, Project(id=pid, name=pid))
        repo = Repo(
            name="myrepo",
            path=str(repo_path),
            default_branch="main",
            index=RepoIndex(enabled=True),
        )
        await save_repo(root, pid, repo)

        reindex_calls: list[tuple[Any, ...]] = []

        async def _fake_reindex(pid_: str, rname: str, path: Path, *, full: bool = True) -> int:
            reindex_calls.append((pid_, rname, path, full))
            return 0

        fake_indexer = MagicMock()
        fake_indexer.reindex_repo = _fake_reindex
        fake_indexer._indexing = set()

        # Patch faiss_store where it is imported *inside* _ensure_repos_indexed.
        with patch("yukar.indexer.faiss_store.index_exists", return_value=False):
            # No index → full=True rebuild is awaited directly.
            await _ensure_repos_indexed(root, pid, fake_indexer)

        assert any(r[0] == pid and r[1] == "myrepo" and r[3] is True for r in reindex_calls), (
            f"Expected full=True reindex call, got: {reindex_calls}"
        )

    async def test_triggers_incremental_reindex_when_index_exists(
        self, tmp_path: Path
    ) -> None:
        """_ensure_repos_indexed must call reindex_repo(full=False) when an index exists."""
        from yukar.models.project import Project, Repo, RepoIndex
        from yukar.runs.supervisor import _ensure_repos_indexed
        from yukar.storage.project_repo import save_project, save_repo

        root = str(tmp_path / "ws")
        pid = "proj"
        repo_path = tmp_path / "myrepo"
        repo_path.mkdir()

        await save_project(root, Project(id=pid, name=pid))
        repo = Repo(
            name="myrepo",
            path=str(repo_path),
            default_branch="main",
            index=RepoIndex(enabled=True),
        )
        await save_repo(root, pid, repo)

        reindex_calls: list[tuple[Any, ...]] = []

        async def _fake_reindex(pid_: str, rname: str, path: Path, *, full: bool = True) -> int:
            reindex_calls.append((pid_, rname, path, full))
            return 0

        fake_indexer = MagicMock()
        fake_indexer._indexing = set()
        fake_indexer.reindex_repo = _fake_reindex

        # index_exists returns True → incremental (full=False) update is called.
        with patch("yukar.indexer.faiss_store.index_exists", return_value=True):
            await _ensure_repos_indexed(root, pid, fake_indexer)

        assert any(r[0] == pid and r[1] == "myrepo" and r[3] is False for r in reindex_calls), (
            f"Expected incremental (full=False) reindex call, got: {reindex_calls}"
        )

    async def test_no_op_when_indexer_service_is_none(self, tmp_path: Path) -> None:
        """_ensure_repos_indexed must be a no-op when no indexer_service provided."""
        from yukar.runs.supervisor import _ensure_repos_indexed

        # Should return without raising.
        await _ensure_repos_indexed(str(tmp_path), "proj", None)


# ---------------------------------------------------------------------------
# 6. EpicOrchestrator accepts continuation kwargs
# ---------------------------------------------------------------------------


class TestOrchestratorContinuationKwargs:
    def test_accepts_seed_prompt_and_is_continuation(self) -> None:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings

        llm = LLMSettings(provider="fake")
        orch = EpicOrchestrator(
            llm_settings=llm,
            git_author_name="a",
            git_author_email="a@b.com",
            seed_prompt="please revise",
            is_continuation=True,
        )
        assert orch._seed_prompt == "please revise"
        assert orch._is_continuation is True

    def test_defaults_to_non_continuation(self) -> None:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings

        llm = LLMSettings(provider="fake")
        orch = EpicOrchestrator(
            llm_settings=llm,
            git_author_name="a",
            git_author_email="a@b.com",
        )
        assert orch._seed_prompt is None
        assert orch._is_continuation is False


# ---------------------------------------------------------------------------
# 8. Continuation run: FSM is the sole writer — seed passed directly as prompt
# ---------------------------------------------------------------------------


class TestContinuationFsmSoleWriter:
    """Verify the single-writer invariant for continuation runs.

    The router no longer pre-writes the seed to the session store.
    Instead, the orchestrator passes seed_prompt directly to stream_async
    on turn-0 so FSM records it exactly once as a clean user message.
    """

    async def test_router_does_not_pre_write_seed(self, tmp_path: Path) -> None:
        """The session store must be empty before the continuation run starts.

        Under the single-writer contract the router hands the seed to
        start_or_inject (which stores it in _seed_prompt); the message is
        only written when stream_async runs on turn-0.
        """
        from yukar.storage import session_store

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-1"

        # Ensure the agent dir exists (would normally be created during run start).
        await session_store.ensure_agent(root, project_id, epic_id, "manager")

        # At this point the router has NOT written anything — the session is empty.
        messages = session_store.list_messages(root, project_id, epic_id, "manager")
        assert messages == [], (
            f"Session store has {len(messages)} message(s) before the run started; "
            "FSM must be the sole writer"
        )

    async def test_continuation_turn0_seed_written_by_fsm(self, tmp_path: Path) -> None:
        """Continuation turn-0 with a seed: FSM records exactly the seed text as the
        first user message.  Verifies via real _run_loop — not by re-implementing
        the branch logic.
        """
        import os
        import subprocess
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn
        from yukar.models.epic import Epic
        from yukar.models.project import Project, Repo, RepoCommands
        from yukar.storage import session_store
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project, save_repo

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-C1"

        # Set up a minimal git repo so the run can proceed.
        repo_path = tmp_path / "myrepo"
        repo_path.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@t.com",
        }

        def g(*args: str) -> None:
            r = subprocess.run(
                ["git", *args], cwd=str(repo_path), capture_output=True, text=True, env=env
            )
            assert r.returncode == 0, f"git {args}: {r.stderr}"

        g("init", "-b", "main")
        g("config", "user.email", "t@t.com")
        g("config", "user.name", "T")
        (repo_path / "README.md").write_text("hi\n")
        g("add", ".")
        g("commit", "-m", "init")

        await save_project(root, Project(id=project_id, name=project_id, repos=[repo_path.name]))
        await save_repo(
            root,
            project_id,
            Repo(
                name=repo_path.name,
                path=str(repo_path),
                default_branch="main",
                commands=RepoCommands(allow=["git"], deny=[]),
            ),
        )
        await save_epic(
            root,
            project_id,
            Epic(
                id=epic_id,
                slug="cont",
                title="Cont Epic",
                description="desc",
                branch="yukar/cont",
            ),
        )

        seed = "please add a /health endpoint"

        # Fake manager: a tool-less text reply — turn-0 ends and the run
        # parks in waiting (P3 turn-end semantics).
        manager_script = [
            TextTurn("Done."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            return FakeModel(script=list(manager_script))

        llm = LLMSettings(provider="fake")
        orch = EpicOrchestrator(
            llm_settings=llm,
            git_author_name="a",
            git_author_email="a@b.com",
            seed_prompt=seed,
            is_continuation=True,
        )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            await run_until_parked(orch, root, project_id, epic_id, "run-cont-seed")

        # The first FSM user message must be the seed text.
        # (Later turns add planning boilerplate — we only care about turn-0 prompt.)
        messages = session_store.list_messages(root, project_id, epic_id, "manager")
        user_msgs = [m for m in messages if m.message.role == "user"]
        assert len(user_msgs) >= 1, "FSM should have recorded at least one user message"
        first_text = user_msgs[0].message.content[0].text
        assert first_text is not None, "First user message content must be text"
        assert first_text == seed, f"FSM recorded {first_text!r} instead of seed {seed!r}"
        # Seed must not contain boilerplate.
        assert "task state" not in first_text.lower()
        assert "dispatch" not in first_text.lower()

    async def test_continuation_turn0_no_seed_resume_prompt_written_by_fsm(
        self, tmp_path: Path
    ) -> None:
        """Continuation without a seed: FSM records the generic resume prompt.
        Verifies via real _run_loop — not by re-implementing the branch logic.
        """
        import os
        import subprocess
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn
        from yukar.models.epic import Epic
        from yukar.models.project import Project, Repo, RepoCommands
        from yukar.storage import session_store
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project, save_repo

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-C2"

        repo_path = tmp_path / "myrepo2"
        repo_path.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@t.com",
        }

        def g(*args: str) -> None:
            r = subprocess.run(
                ["git", *args], cwd=str(repo_path), capture_output=True, text=True, env=env
            )
            assert r.returncode == 0, f"git {args}: {r.stderr}"

        g("init", "-b", "main")
        g("config", "user.email", "t@t.com")
        g("config", "user.name", "T")
        (repo_path / "README.md").write_text("hi\n")
        g("add", ".")
        g("commit", "-m", "init")

        await save_project(root, Project(id=project_id, name=project_id, repos=[repo_path.name]))
        await save_repo(
            root,
            project_id,
            Repo(
                name=repo_path.name,
                path=str(repo_path),
                default_branch="main",
                commands=RepoCommands(allow=["git"], deny=[]),
            ),
        )
        await save_epic(
            root,
            project_id,
            Epic(
                id=epic_id,
                slug="cont2",
                title="Cont Epic 2",
                description="desc",
                branch="yukar/cont2",
            ),
        )

        manager_script = [
            TextTurn("OK"),
            TextTurn("Done."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            return FakeModel(script=list(manager_script))

        llm = LLMSettings(provider="fake")
        orch = EpicOrchestrator(
            llm_settings=llm,
            git_author_name="a",
            git_author_email="a@b.com",
            seed_prompt=None,
            is_continuation=True,
        )

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            await run_until_parked(orch, root, project_id, epic_id, "run-cont-noseed")

        messages = session_store.list_messages(root, project_id, epic_id, "manager")
        user_msgs = [m for m in messages if m.message.role == "user"]
        assert len(user_msgs) >= 1, "FSM should have recorded at least one user message"
        first_text = user_msgs[0].message.content[0].text
        assert first_text is not None, "First user message content must be text"
        assert "previous run ended" in first_text, (
            f"Expected resume-prompt text; got {first_text!r}"
        )

    async def test_fresh_run_turn0_hitl_does_not_lose_epic_prompt(self, tmp_path: Path) -> None:
        """Regression: fresh run turn-0 with unsolicited HITL must NOT replace the
        Epic initialisation prompt.  The Epic title/description must appear in the
        FSM-written user message even when HITL texts are present.

        Before the fix, the ``elif hitl_texts:`` branch fired first and the Epic
        prompt (``_build_manager_prompt``) was never passed to stream_async.
        """
        import os
        import subprocess
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn
        from yukar.models.epic import Epic
        from yukar.models.project import Project, Repo, RepoCommands
        from yukar.storage import session_store
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project, save_repo

        root = str(tmp_path / "ws")
        project_id = "proj"
        epic_id = "EP-FRESH-HITL"

        repo_path = tmp_path / "myrepo3"
        repo_path.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@t.com",
        }

        def g(*args: str) -> None:
            r = subprocess.run(
                ["git", *args], cwd=str(repo_path), capture_output=True, text=True, env=env
            )
            assert r.returncode == 0, f"git {args}: {r.stderr}"

        g("init", "-b", "main")
        g("config", "user.email", "t@t.com")
        g("config", "user.name", "T")
        (repo_path / "README.md").write_text("hi\n")
        g("add", ".")
        g("commit", "-m", "init")

        epic_title = "Add Metrics Endpoint"
        await save_project(root, Project(id=project_id, name=project_id, repos=[repo_path.name]))
        await save_repo(
            root,
            project_id,
            Repo(
                name=repo_path.name,
                path=str(repo_path),
                default_branch="main",
                commands=RepoCommands(allow=["git"], deny=[]),
            ),
        )
        await save_epic(
            root,
            project_id,
            Epic(
                id=epic_id,
                slug="metrics",
                title=epic_title,
                description="Expose a /metrics endpoint.",
                branch="yukar/metrics",
            ),
        )

        manager_script = [
            TextTurn("OK"),
            TextTurn("Done."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            return FakeModel(script=list(manager_script))

        llm = LLMSettings(provider="fake")
        orch = EpicOrchestrator(
            llm_settings=llm,
            git_author_name="a",
            git_author_email="a@b.com",
            # Fresh run: is_continuation=False (default), no seed_prompt.
        )

        # Inject an unsolicited HITL message *before* the run starts — simulates
        # the race where a user message arrives between run creation and turn-0.
        orch.inject_message("manager", "also please add a /health endpoint")

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            await run_until_parked(orch, root, project_id, epic_id, "run-fresh-hitl")

        messages = session_store.list_messages(root, project_id, epic_id, "manager")
        user_msgs = [m for m in messages if m.message.role == "user"]
        assert len(user_msgs) >= 1, "FSM should have recorded at least one user message"
        first_text = user_msgs[0].message.content[0].text
        assert first_text is not None, "First user message content must be text"

        # The Epic title must be present — Epic prompt was NOT replaced by HITL alone.
        assert epic_title in first_text, (
            f"Epic title {epic_title!r} missing from turn-0 prompt: {first_text!r}"
        )
        # The HITL text must also be present (not silently dropped).
        assert "/health endpoint" in first_text, (
            f"HITL text missing from turn-0 prompt: {first_text!r}"
        )


# ---------------------------------------------------------------------------
# 7. Legacy RunState statuses are read back as 'waiting'
# ---------------------------------------------------------------------------


class TestRunStateLegacyCoercion:
    def test_legacy_statuses_coerce_to_waiting(self) -> None:
        """idle / awaiting_input / interrupted all meant "not running, your
        turn" and are read back as ``waiting`` by the model validator."""
        from yukar.models.run import RunState

        for legacy in ("idle", "awaiting_input", "interrupted"):
            state = RunState.model_validate({"run_id": "r1", "status": legacy})
            assert state.status == "waiting", f"{legacy} must coerce to waiting"

    def test_current_statuses_pass_through(self) -> None:
        from yukar.models.run import RunState

        for status in ("running", "paused", "waiting", "error", "completed"):
            state = RunState.model_validate({"run_id": "r1", "status": status})
            assert state.status == status

    def test_legacy_pending_question_key_is_ignored(self) -> None:
        """Old state.yaml files carry a pending_question key; pydantic's
        default extra handling must ignore it without error."""
        from yukar.models.run import RunState

        state = RunState.model_validate(
            {"run_id": "r1", "status": "awaiting_input", "pending_question": "Proceed?"}
        )
        assert state.status == "waiting"
        assert not hasattr(state, "pending_question")

    async def test_legacy_round_trip_yaml(self, tmp_path: Path) -> None:
        """A legacy 'interrupted' state file loads as waiting via the repo."""
        from yukar.config import paths
        from yukar.models.run import RunState
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        # Write a modern file first to create the directory structure, then
        # overwrite it with legacy YAML content.
        await state_repo.save_state(root, "proj", "EP-1", RunState(run_id="r1"))
        state_path = paths.state_yaml(root, "proj", "EP-1")
        state_path.write_text(
            "run_id: r1\nstatus: interrupted\npending_question: 'Proceed?'\n"
        )
        loaded = await state_repo.get_state(root, "proj", "EP-1")
        assert loaded is not None
        assert loaded.status == "waiting"
