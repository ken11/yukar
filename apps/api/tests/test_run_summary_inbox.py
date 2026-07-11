"""Lifecycle redesign P4 — "your turn" inbox plumbing (backend).

Covers:
1. ``RunState.role`` — persistence round-trip, legacy default (state.yaml
   without the key reads as ``manager``), and the orchestrator writing the
   correct role for manager AND reviewer conversation runs.
2. ``GET /epics`` run_summary — digest present when state.yaml exists, null
   when it does not, and null (log-and-degrade, epic still listed) when
   state.yaml is corrupt.
3. Project-scope SSE — the "your turn" signals (user_input_requested /
   user_input_resolved) reach ``subscribe_project`` consumers so the board
   can update its waiting badges live.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests._helpers import make_git_repo, run_until_parked

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
        commands=RepoCommands(allow=["git"], deny=[]),
    )
    await save_repo(root, project_id, repo)
    epic = Epic(
        id=epic_id,
        slug="test-epic",
        title="Test Epic",
        description="P4 role test epic.",
        branch="yukar/ep-1-test-epic",
    )
    await save_epic(root, project_id, epic)


def _scripted_orchestrator(**kwargs: Any) -> Any:
    """An EpicOrchestrator whose LLM replays a single park-inducing TextTurn."""
    from yukar.agents.orchestrator import EpicOrchestrator
    from yukar.config.settings import LLMSettings

    return EpicOrchestrator(
        llm_settings=LLMSettings(provider="fake"),
        git_author_name="yukar",
        git_author_email="yukar@localhost",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. RunState.role
# ---------------------------------------------------------------------------


class TestRunStateRole:
    @pytest.mark.asyncio
    async def test_role_roundtrips_through_state_yaml(self, tmp_path: Path) -> None:
        from yukar.models.run import RunState
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        state = RunState(run_id="r-1", status="waiting", role="reviewer", manager_thread="rev-1")
        await state_repo.save_state(root, "p1", "EP-1", state)

        loaded = await state_repo.get_state(root, "p1", "EP-1")
        assert loaded is not None
        assert loaded.role == "reviewer"
        assert loaded.manager_thread == "rev-1"

    @pytest.mark.asyncio
    async def test_legacy_state_without_role_defaults_to_manager(self, tmp_path: Path) -> None:
        """Old state.yaml files predate the ``role`` key — they read as manager."""
        from yukar.config import paths
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        yaml_path = paths.state_yaml(root, "p1", "EP-1")
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        # Legacy vocabulary on purpose: awaiting_input status + no role key.
        yaml_path.write_text(
            "run_id: r-old\nstatus: awaiting_input\nmanager_thread: manager\n",
            encoding="utf-8",
        )

        loaded = await state_repo.get_state(root, "p1", "EP-1")
        assert loaded is not None
        assert loaded.role == "manager"
        assert loaded.status == "waiting"  # legacy status coercion still applies

    @pytest.mark.asyncio
    async def test_manager_run_writes_manager_role(self, tmp_path: Path) -> None:
        """A scripted manager run records role=manager in state.yaml."""
        from unittest.mock import patch

        from yukar.llm.fake import FakeModel, TextTurn
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        project_id, epic_id = "proj", "EP-1"
        repo = make_git_repo(tmp_path, "myrepo")
        await _bootstrap(root, project_id, epic_id, repo)

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            return FakeModel(script=[TextTurn("Here is my plan.")])

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            orch = _scripted_orchestrator()
            await run_until_parked(orch, root, project_id, epic_id, "run-mgr")

        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None
        assert state.role == "manager"
        assert state.manager_thread == "manager"
        assert state.status == "waiting"

    @pytest.mark.asyncio
    async def test_reviewer_run_writes_reviewer_role(self, tmp_path: Path) -> None:
        """A scripted reviewer run records role=reviewer + its own thread id,
        and its park signal reaches project-scope SSE subscribers with that
        thread id (the attribution P4 exists to fix)."""
        from unittest.mock import patch

        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn
        from yukar.models.events import UserInputRequestedEvent
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        project_id, epic_id = "proj", "EP-1"
        repo = make_git_repo(tmp_path, "myrepo")
        await _bootstrap(root, project_id, epic_id, repo)

        received: list[Any] = []

        async def _collect() -> None:
            async with event_bus.subscribe_project(project_id) as q:
                while True:
                    ev = await q.get()
                    if ev is None:
                        return
                    if isinstance(ev, UserInputRequestedEvent):
                        received.append(ev)
                        return

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            return FakeModel(script=[TextTurn("Review verdict: looks good.")])

        try:
            with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
                orch = _scripted_orchestrator(
                    agent_role="reviewer", manager_thread_id="rev-1", review_context="CTX"
                )
                await run_until_parked(orch, root, project_id, epic_id, "run-rev")

            state = await state_repo.get_state(root, project_id, epic_id)
            assert state is not None
            assert state.role == "reviewer"
            assert state.manager_thread == "rev-1"
            assert state.status == "waiting"

            await asyncio.wait_for(collector, timeout=5.0)
            assert len(received) == 1
            assert received[0].thread_id == "rev-1"
        finally:
            if not collector.done():
                event_bus.publish_project_sentinel(project_id)
                await asyncio.wait_for(collector, timeout=5.0)


# ---------------------------------------------------------------------------
# 2. GET /epics run_summary
# ---------------------------------------------------------------------------


async def _save_project_and_epic(
    root: str, project_id: str, epic_id: str, *, title: str = "T"
) -> None:
    from yukar.models.epic import Epic
    from yukar.models.project import Project
    from yukar.storage.epic_repo import save_epic
    from yukar.storage.project_repo import get_project, save_project

    if await get_project(root, project_id) is None:
        await save_project(root, Project(id=project_id, name=project_id))
    await save_epic(root, project_id, Epic(id=epic_id, slug="s", title=title, status="open"))


class TestEpicListRunSummary:
    @pytest.mark.asyncio
    async def test_run_summary_present_absent_and_degraded(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """One list call: state.yaml → digest, no state.yaml → null, corrupt
        state.yaml → null WITHOUT killing the list (log-and-degrade)."""
        from yukar.config import paths
        from yukar.models.run import RunState
        from yukar.storage import state_repo

        root = str(tmp_workspace)
        pid = "p-list"
        await _save_project_and_epic(root, pid, "EP-1")  # with state.yaml
        await _save_project_and_epic(root, pid, "EP-2")  # never run
        await _save_project_and_epic(root, pid, "EP-3")  # corrupt state.yaml

        await state_repo.save_state(
            root,
            pid,
            "EP-1",
            RunState(run_id="r-1", status="waiting", role="reviewer", manager_thread="rev-1"),
        )
        corrupt = paths.state_yaml(root, pid, "EP-3")
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_text("run_id: [not, a, string\n", encoding="utf-8")

        resp = await app_client.get(f"/api/projects/{pid}/epics")
        assert resp.status_code == 200
        body = {e["id"]: e for e in resp.json()}
        # The corrupt epic is still listed — degrade, don't disappear.
        assert set(body) == {"EP-1", "EP-2", "EP-3"}

        summary = body["EP-1"]["run_summary"]
        assert summary is not None
        assert summary["status"] == "waiting"
        assert summary["run_id"] == "r-1"
        assert summary["thread_id"] == "rev-1"
        assert summary["role"] == "reviewer"

        assert body["EP-2"]["run_summary"] is None
        assert body["EP-3"]["run_summary"] is None

    @pytest.mark.asyncio
    async def test_legacy_state_yaml_summarised_with_new_vocabulary(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """A pre-redesign state.yaml (awaiting_input, no role) surfaces as a
        waiting/manager digest — the board treats old parks as "your turn"."""
        from yukar.config import paths

        root = str(tmp_workspace)
        pid = "p-legacy"
        await _save_project_and_epic(root, pid, "EP-9")
        yaml_path = paths.state_yaml(root, pid, "EP-9")
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text(
            "run_id: r-old\nstatus: awaiting_input\nmanager_thread: manager\n",
            encoding="utf-8",
        )

        resp = await app_client.get(f"/api/projects/{pid}/epics")
        assert resp.status_code == 200
        (epic,) = resp.json()
        assert epic["run_summary"] == {
            "status": "waiting",
            "run_id": "r-old",
            "thread_id": "manager",
            "role": "manager",
            "last_event_at": None,
        }


# ---------------------------------------------------------------------------
# 3. Project-scope SSE — your-turn signals
# ---------------------------------------------------------------------------


class TestProjectSseYourTurn:
    @pytest.mark.asyncio
    async def test_user_input_events_fan_out_to_project_subscribers(self) -> None:
        """user_input_requested / user_input_resolved are lifecycle events:
        project-scope subscribers (the board) receive them."""
        from yukar.events import bus as event_bus
        from yukar.models.events import UserInputRequestedEvent, UserInputResolvedEvent

        pid, eid = "p-sse", "EP-sse"
        received: list[Any] = []

        async def _collect() -> None:
            async with event_bus.subscribe_project(pid) as q:
                while True:
                    ev = await q.get()
                    if ev is None:
                        return
                    received.append(ev)

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        event_bus.publish(
            pid,
            eid,
            UserInputRequestedEvent(
                project_id=pid, epic_id=eid, run_id="r-1", thread_id="rev-1", question=""
            ),
        )
        event_bus.publish(
            pid,
            eid,
            UserInputResolvedEvent(project_id=pid, epic_id=eid, run_id="r-1", thread_id="rev-1"),
        )
        event_bus.publish_project_sentinel(pid)

        await asyncio.wait_for(collector, timeout=2.0)
        assert [type(e).__name__ for e in received] == [
            "UserInputRequestedEvent",
            "UserInputResolvedEvent",
        ]
        assert received[0].thread_id == "rev-1"
        assert received[1].thread_id == "rev-1"
