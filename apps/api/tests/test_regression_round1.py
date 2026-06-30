"""Regression tests for code-review fix items.

Covers fixes #1-#9 from the review:
  1. DiffResult.repo uses registered name, not dir basename
  2. PEP 758 except syntax (structural; covered by import tests)
  3. Path traversal prevention in config/paths.py
  4. epic.yaml.status sync with run state
  5. create_project validates all repos before writing files
  6. append_message lock prevents index collision
  7. _write_settings_sync uses atomic write (no direct open)
  8. SSE sentinel (None) closes streams on run completion / stop
  9. PUT /api/settings expands ~ in workspace_root
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Fix #1 — DiffResult.repo uses registered name, not directory basename
# ---------------------------------------------------------------------------


class TestDiffResultRepo:
    async def test_repo_name_is_registered_name_not_dirname(self, fixture_git_repo: Path) -> None:
        """When the registered repo name differs from the directory basename,
        DiffResult.repo must contain the registered name."""
        from yukar.git.diff import get_diff

        # The fixture_git_repo directory is called "test-repo".
        # Pass a different registered name to simulate the mismatch.
        result = await get_diff(
            fixture_git_repo,
            mode="working",
            repo_name="my-registered-name",
        )
        assert result.repo == "my-registered-name", (
            f"Expected 'my-registered-name', got {result.repo!r}"
        )

    async def test_repo_name_falls_back_to_dirname_when_not_supplied(
        self, fixture_git_repo: Path
    ) -> None:
        """Without an explicit repo_name, basename is used as a fallback."""
        from yukar.git.diff import get_diff

        result = await get_diff(fixture_git_repo, mode="working")
        assert result.repo == fixture_git_repo.name

    async def test_git_diff_endpoint_repo_is_registered_name(
        self,
        app_client: AsyncClient,
        tmp_path: Path,
    ) -> None:
        """The /git/diff endpoint sets DiffResult.repo to the registered name."""
        import os
        import subprocess

        # Create a repo whose directory name differs from its registered name.
        repo_dir = tmp_path / "dir-name"
        repo_dir.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@t.com",
        }

        def git(*args: str) -> None:
            subprocess.run(
                ["git", *args], cwd=str(repo_dir), env=env, check=True, capture_output=True
            )

        git("init", "-b", "main")
        git("config", "user.email", "t@t.com")
        git("config", "user.name", "T")
        (repo_dir / "f.txt").write_text("hello")
        git("add", ".")
        git("commit", "-m", "init")

        await app_client.post(
            "/api/projects",
            json={
                "id": "rn-proj",
                "name": "Repo Name Test",
                "repos": [
                    {"name": "registered-name", "path": str(repo_dir), "default_branch": "main"}
                ],
            },
        )
        await app_client.post("/api/projects/rn-proj/epics", json={"title": "E"})

        r = await app_client.get(
            "/api/projects/rn-proj/epics/EP-1/git/diff?mode=working&repo=registered-name"
        )
        assert r.status_code == 200
        data = r.json()
        assert data["repo"] == "registered-name", (
            f"Expected 'registered-name', got {data['repo']!r}"
        )


# ---------------------------------------------------------------------------
# Fix #1 — DummyRunner uses actual registered repo name in events
# ---------------------------------------------------------------------------


class TestDummyRunnerRepoName:
    async def test_worker_events_use_registered_repo_name(self, tmp_workspace: Path) -> None:
        """WorkerStartedEvent.repo must be the registered repo name, not 'repo'."""
        import os
        import subprocess

        from yukar.events.bus import subscribe
        from yukar.models.epic import Epic
        from yukar.models.events import WorkerStartedEvent
        from yukar.models.project import Project, Repo
        from yukar.runs.runner import DummyRunner
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project, save_repo

        root = str(tmp_workspace)
        project_id = "rp-proj"

        # Create a minimal git repo so save_repo has a valid path
        repo_dir = tmp_workspace / "my-actual-repo"
        repo_dir.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@t.com",
        }

        def git(*args: str) -> None:
            subprocess.run(
                ["git", *args], cwd=str(repo_dir), env=env, check=True, capture_output=True
            )

        git("init", "-b", "main")
        git("config", "user.email", "t@t.com")
        git("config", "user.name", "T")
        (repo_dir / "f.txt").write_text("x")
        git("add", ".")
        git("commit", "-m", "init")

        project = Project(id=project_id, name="P", repos=["special-repo-name"])
        await save_project(root, project)
        await save_repo(root, project_id, Repo(name="special-repo-name", path=str(repo_dir)))
        await save_epic(root, project_id, Epic(id="EP-1", slug="test", title="T"))

        received: list[object] = []

        async def collect() -> None:
            async with subscribe(project_id, "EP-1") as q:
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=10.0)
                        if event is None:
                            break
                        received.append(event)
                    except TimeoutError:
                        break

        runner = DummyRunner()
        collector = asyncio.create_task(collect())
        await asyncio.sleep(0.05)
        await runner.start(root, project_id, "EP-1", "run-x")
        await asyncio.wait_for(collector, timeout=15.0)

        worker_events = [e for e in received if isinstance(e, WorkerStartedEvent)]
        assert worker_events, "Expected at least one WorkerStartedEvent"
        for ev in worker_events:
            assert ev.repo == "special-repo-name", f"Expected 'special-repo-name', got {ev.repo!r}"


# ---------------------------------------------------------------------------
# Fix #2 — PEP 758 syntax is corrected (structural: just import them)
# ---------------------------------------------------------------------------


class TestPep758Fix:
    def test_supervisor_importable(self) -> None:
        from yukar.runs import supervisor  # noqa: F401

    def test_session_store_importable(self) -> None:
        from yukar.storage import session_store  # noqa: F401


# ---------------------------------------------------------------------------
# Fix #3 — Path traversal prevention
# ---------------------------------------------------------------------------


class TestPathTraversal:
    def test_dotdot_project_id_raises(self, tmp_workspace: Path) -> None:
        from yukar.config import paths

        with pytest.raises(ValueError, match="project_id"):
            paths.project_dir(str(tmp_workspace), "../etc")

    def test_slash_project_id_raises(self, tmp_workspace: Path) -> None:
        from yukar.config import paths

        with pytest.raises(ValueError, match="project_id"):
            paths.project_dir(str(tmp_workspace), "foo/bar")

    def test_empty_project_id_raises(self, tmp_workspace: Path) -> None:
        from yukar.config import paths

        with pytest.raises(ValueError, match="project_id"):
            paths.project_dir(str(tmp_workspace), "")

    def test_dot_project_id_raises(self, tmp_workspace: Path) -> None:
        from yukar.config import paths

        with pytest.raises(ValueError, match="project_id"):
            paths.project_dir(str(tmp_workspace), ".")

    def test_dotdot_epic_id_raises(self, tmp_workspace: Path) -> None:
        from yukar.config import paths

        with pytest.raises(ValueError, match="epic_id"):
            paths.epic_dir(str(tmp_workspace), "proj", "../etc")

    def test_dotdot_repo_name_raises(self, tmp_workspace: Path) -> None:
        from yukar.config import paths

        with pytest.raises(ValueError, match="repo_name"):
            paths.repo_yaml(str(tmp_workspace), "proj", "../etc")

    def test_dotdot_agent_id_raises(self, tmp_workspace: Path) -> None:
        from yukar.config import paths

        with pytest.raises(ValueError, match="agent_id"):
            paths.agent_dir(str(tmp_workspace), "proj", "EP-1", "../etc")

    def test_valid_segment_accepted(self, tmp_workspace: Path) -> None:
        from yukar.config import paths

        # Should not raise
        p = paths.project_dir(str(tmp_workspace), "my-project")
        assert p.name == "my-project"

    async def test_traversal_attempt_via_api_returns_422(self, app_client: AsyncClient) -> None:
        """project_id with a slash in it must be rejected at the HTTP level."""
        # FastAPI path parameters cannot contain unencoded slashes; we test
        # the ID we control: project creation body with a bad project_id.
        # The only way to inject this is via the create endpoint.
        # A double-dot in a path segment hits the PathSegmentError→422 handler.
        # NOTE: FastAPI will strip path parameters at routing level for slashes,
        # so we test the case that actually reaches our code: a valid URL
        # but a dot-dot id embedded in a query parameter or body.
        # Use a repo_name with traversal via the git status endpoint.
        await app_client.post("/api/projects", json={"id": "p", "name": "P", "repos": []})
        await app_client.post("/api/projects/p/epics", json={"title": "E"})

        r = await app_client.get("/api/projects/p/epics/EP-1/git/status?repo=../etc")
        # repo_yaml will raise PathSegmentError → 422
        assert r.status_code == 422

    def test_path_segment_error_is_value_error_subclass(self) -> None:
        """PathSegmentError must be a ValueError subclass so existing catch-sites work."""
        from yukar.config.paths import PathSegmentError, _validate_segment

        with pytest.raises(ValueError):
            _validate_segment("../etc", "test_label")

        with pytest.raises(PathSegmentError):
            _validate_segment("../etc", "test_label")

    async def test_corrupted_epic_yaml_returns_500_not_422(
        self,
        tmp_workspace: Path,
        yukar_config_dir: Path,  # noqa: ARG002
    ) -> None:
        """A corrupted epic.yaml (YAML parse error) must return 500, not 422.

        422 must only come from PathSegmentError (traversal guard).
        Broken YAML must not leak internal details to clients via a 422 response.

        Uses raise_server_exceptions=False so the ASGI client converts the
        unhandled server exception to a 500 response rather than re-raising it.
        """
        from httpx import ASGITransport, AsyncClient

        from yukar.app import create_app
        from yukar.config import paths
        from yukar.config.settings import Settings
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.runs.supervisor import init_supervisor
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="corrupt-proj", name="C"))
        await save_epic(root, "corrupt-proj", Epic(id="EP-1", slug="bad", title="Bad"))

        # Overwrite epic.yaml with syntactically invalid YAML
        epic_path = paths.epic_yaml(root, "corrupt-proj", "EP-1")
        epic_path.write_text(": invalid: [yaml: content\n")

        app = create_app()
        settings = Settings(workspace_root=root)
        app.state.settings = settings
        init_supervisor(max_parallel_epics=settings.agent.max_parallel_epics)

        # raise_app_exceptions=False: ASGI returns 500 instead of re-raising
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            r = await client.get("/api/projects/corrupt-proj/epics/EP-1")

        assert r.status_code == 500, (
            f"Expected 500 for corrupted YAML, got {r.status_code}: {r.text}"
        )
        # Must NOT be 422 (no PathSegmentError involved)
        assert r.status_code != 422


# ---------------------------------------------------------------------------
# Fix #4 — epic.yaml.status syncs with run lifecycle (supervisor owns transitions)
# ---------------------------------------------------------------------------


class TestEpicStatusSync:
    async def test_epic_status_set_to_in_progress_on_run_start(self, tmp_workspace: Path) -> None:
        """Supervisor sets epic status to in_progress before the runner task starts."""
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.runs.supervisor import RunSupervisor
        from yukar.storage.epic_repo import get_epic, save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="ep-proj", name="P"))
        await save_epic(root, "ep-proj", Epic(id="EP-1", slug="s", title="T"))

        # Verify initial status
        epic = await get_epic(root, "ep-proj", "EP-1")
        assert epic is not None
        assert epic.status == "planned"

        sup = RunSupervisor(max_parallel_epics=2)
        # supervisor.start sets in_progress synchronously before returning
        await sup.start(root, "ep-proj", "EP-1")

        epic = await get_epic(root, "ep-proj", "EP-1")
        assert epic is not None
        assert epic.status == "in_progress"

        # Let run complete
        key = ("ep-proj", "EP-1")
        handle = sup._runs[key]  # noqa: SLF001
        await asyncio.wait_for(handle.task, timeout=20.0)

        # The Manager finishing means the work AWAITS USER REVIEW — not done.
        # Only the user reaches completed/merged. See models.epic.EpicStatus.
        epic = await get_epic(root, "ep-proj", "EP-1")
        assert epic is not None
        assert epic.status == "in_review"

    async def test_epic_status_via_api_run(self, app_client: AsyncClient) -> None:
        """POST /run sets epic status to in_progress; after completion it's in_review."""
        from yukar.events.bus import subscribe

        await app_client.post("/api/projects", json={"id": "sp", "name": "SP", "repos": []})
        await app_client.post("/api/projects/sp/epics", json={"title": "SyncEpic"})

        # Epic starts as planned
        r = await app_client.get("/api/projects/sp/epics/EP-1")
        assert r.json()["status"] == "planned"

        completed = asyncio.Event()

        async def collect() -> None:
            async with subscribe("sp", "EP-1") as q:
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=10.0)
                        if event is None:
                            break
                        if hasattr(event, "type") and event.type == "run_completed":
                            completed.set()
                    except TimeoutError:
                        break

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0.05)

        r = await app_client.post("/api/projects/sp/epics/EP-1/run")
        assert r.status_code == 202

        await asyncio.wait_for(completed.wait(), timeout=20.0)
        await asyncio.wait_for(collector, timeout=3.0)

        # ``run_completed`` is emitted by the runner (orchestrator) as its final
        # act, *before* the supervisor persists ``epic.status="in_review"`` — the
        # supervisor owns that write and runs it after ``runner.start()`` returns.
        # The event is therefore observable a beat before the terminal status
        # lands on disk, so poll for it rather than asserting it is already
        # persisted the instant the event fires (the sibling test synchronises on
        # the run task itself for the same reason). The run finishing leaves the
        # epic awaiting the user's review, NOT completed.
        status = None
        for _ in range(50):
            r = await app_client.get("/api/projects/sp/epics/EP-1")
            status = r.json()["status"]
            if status == "in_review":
                break
            await asyncio.sleep(0.05)
        assert status == "in_review"


# ---------------------------------------------------------------------------
# Fix #5 — create_project validates repos before writing project.yaml
# ---------------------------------------------------------------------------


class TestCreateProjectRepoValidation:
    async def test_invalid_repo_leaves_no_orphan(
        self, app_client: AsyncClient, tmp_path: Path
    ) -> None:
        """If a repo path is invalid, project.yaml must not be written."""
        r = await app_client.post(
            "/api/projects",
            json={
                "id": "orphan-proj",
                "name": "Orphan",
                "repos": [{"name": "bad", "path": str(tmp_path / "nonexistent")}],
            },
        )
        assert r.status_code == 422

        # project.yaml must not exist
        r2 = await app_client.get("/api/projects/orphan-proj")
        assert r2.status_code == 404

    async def test_second_invalid_repo_leaves_no_orphan(
        self, app_client: AsyncClient, tmp_path: Path, fixture_git_repo: Path
    ) -> None:
        """First repo valid, second invalid — still no project.yaml written."""
        r = await app_client.post(
            "/api/projects",
            json={
                "id": "partial-proj",
                "name": "Partial",
                "repos": [
                    {"name": "ok-repo", "path": str(fixture_git_repo)},
                    {"name": "bad-repo", "path": str(tmp_path / "nonexistent")},
                ],
            },
        )
        assert r.status_code == 422

        r2 = await app_client.get("/api/projects/partial-proj")
        assert r2.status_code == 404


# ---------------------------------------------------------------------------
# Fix #6 — append_message index lock prevents collision
# ---------------------------------------------------------------------------


class TestAppendMessageLock:
    async def test_concurrent_appends_have_unique_indices(self, tmp_workspace: Path) -> None:
        """Concurrent calls to append_message must produce unique message_ids."""
        from yukar.storage.session_store import append_message, list_messages

        root = str(tmp_workspace)
        n = 10
        await asyncio.gather(
            *[append_message(root, "cp", "EP-1", "ag", "user", f"msg {i}") for i in range(n)]
        )
        messages = list_messages(root, "cp", "EP-1", "ag")
        assert len(messages) == n
        ids = [m.message_id for m in messages]
        assert len(set(ids)) == n, f"Duplicate message_ids found: {ids}"


# ---------------------------------------------------------------------------
# Fix #2 — supervisor start→stop integration (stop path coverage)
# ---------------------------------------------------------------------------


class TestSupervisorStopIntegration:
    async def test_stop_while_running(self, tmp_workspace: Path) -> None:
        """Supervisor.stop() should terminate an active run without error."""
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.runs.supervisor import RunSupervisor
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="sv-proj", name="P"))
        await save_epic(root, "sv-proj", Epic(id="EP-1", slug="s", title="T"))

        sup = RunSupervisor(max_parallel_epics=2)
        await sup.start(root, "sv-proj", "EP-1")
        assert sup.is_running("sv-proj", "EP-1")

        # Stop while running — must not raise
        await sup.stop("sv-proj", "EP-1")
        assert not sup.is_running("sv-proj", "EP-1")

    async def test_stop_not_running_is_noop(self, tmp_workspace: Path) -> None:
        """Stopping a non-existent run is a no-op."""
        from yukar.runs.supervisor import RunSupervisor

        sup = RunSupervisor()
        await sup.stop("no-proj", "EP-99")  # Must not raise

    async def test_start_after_stop(self, tmp_workspace: Path) -> None:
        """A new run can be started after a stop."""
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.runs.supervisor import RunSupervisor
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="sv2-proj", name="P"))
        await save_epic(root, "sv2-proj", Epic(id="EP-1", slug="s", title="T"))

        sup = RunSupervisor(max_parallel_epics=2)
        first_run_id = await sup.start(root, "sv2-proj", "EP-1")
        await sup.stop("sv2-proj", "EP-1")

        # Re-start
        await save_epic(root, "sv2-proj", Epic(id="EP-1", slug="s", title="T"))
        second_run_id = await sup.start(root, "sv2-proj", "EP-1")
        assert second_run_id != first_run_id
        await sup.stop("sv2-proj", "EP-1")


# ---------------------------------------------------------------------------
# Fix #7 — loader._write_settings_sync is atomic (no open("w"))
# ---------------------------------------------------------------------------


class TestWriteSettingsSyncIsAtomic:
    def test_write_settings_sync_creates_valid_yaml(
        self, tmp_path: Path, yukar_config_dir: Path
    ) -> None:
        """_write_settings_sync must create a readable YAML file atomically."""
        from yukar.config.loader import load_settings, settings_path
        from yukar.storage.yaml_io import read_yaml

        # load_settings will call _write_settings_sync if file absent
        load_settings()
        path = settings_path()
        assert path.exists()
        raw = read_yaml(path)
        assert "workspace_root" in raw


# ---------------------------------------------------------------------------
# Fix #8 — SSE sentinel closes streams on run completion
# ---------------------------------------------------------------------------


class TestSseSentinel:
    async def test_sentinel_delivered_to_all_subscribers(self, tmp_workspace: Path) -> None:
        """None sentinel is delivered to every subscriber after run completion."""
        from yukar.events.bus import subscribe
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.runs.runner import DummyRunner
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="sse-proj", name="P"))
        await save_epic(root, "sse-proj", Epic(id="EP-1", slug="s", title="T"))

        received_sentinels = [False, False]

        async def collect(idx: int) -> None:
            async with subscribe("sse-proj", "EP-1") as q:
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=12.0)
                        if event is None:
                            received_sentinels[idx] = True
                            break
                    except TimeoutError:
                        break

        c1 = asyncio.create_task(collect(0))
        c2 = asyncio.create_task(collect(1))
        await asyncio.sleep(0.05)

        runner = DummyRunner()
        await runner.start(root, "sse-proj", "EP-1", "run-s1")
        await asyncio.gather(c1, c2)

        assert received_sentinels[0], "Subscriber 0 did not receive sentinel"
        assert received_sentinels[1], "Subscriber 1 did not receive sentinel"

    async def test_sentinel_on_stop(self, tmp_workspace: Path) -> None:
        """Sentinel is published even when a run is stopped early."""
        from yukar.events.bus import subscribe
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.runs.runner import DummyRunner
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="stop-proj", name="P"))
        await save_epic(root, "stop-proj", Epic(id="EP-1", slug="s", title="T"))

        received_sentinel = asyncio.Event()

        async def collect() -> None:
            async with subscribe("stop-proj", "EP-1") as q:
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=8.0)
                        if event is None:
                            received_sentinel.set()
                            break
                    except TimeoutError:
                        break

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0.05)

        runner = DummyRunner()
        run_task = asyncio.create_task(runner.start(root, "stop-proj", "EP-1", "run-stop"))
        await asyncio.sleep(0.1)
        await runner.stop()
        await asyncio.wait_for(run_task, timeout=5.0)
        await asyncio.wait_for(received_sentinel.wait(), timeout=3.0)
        assert received_sentinel.is_set()
        await collector


# ---------------------------------------------------------------------------
# Fix #9 — PUT /api/settings expands ~ in workspace_root
# ---------------------------------------------------------------------------


class TestSettingsTildeExpansion:
    async def test_tilde_path_expanded_before_comparison(self, app_client: AsyncClient) -> None:
        """A ``~`` workspace_root is expanded before the runtime-change guard runs.

        Expansion still happens (fix #9). Because the expanded value differs from
        the current root that is wired into the supervisor at startup, the change
        is rejected at runtime (see ``put_settings``); the 422 detail must mention
        workspace_root rather than be a validation error on an un-expanded ``~``.
        """
        r = await app_client.get("/api/settings")
        current = r.json()
        current["workspace_root"] = "~/my-workspace"
        r2 = await app_client.put("/api/settings", json=current)
        assert r2.status_code == 422
        assert "workspace_root" in r2.json()["detail"]

    async def test_unchanged_workspace_root_accepted(self, app_client: AsyncClient) -> None:
        """PUT with the current (already-expanded) workspace_root is persisted."""
        r = await app_client.get("/api/settings")
        current = r.json()
        # Echo back the current root unchanged → passes the runtime-change guard.
        r2 = await app_client.put("/api/settings", json=current)
        assert r2.status_code == 200
        assert r2.json()["workspace_root"] == current["workspace_root"]

    async def test_changed_absolute_workspace_root_rejected(
        self, app_client: AsyncClient
    ) -> None:
        """An absolute root different from the startup root is rejected at runtime."""
        r = await app_client.get("/api/settings")
        current = r.json()
        current["workspace_root"] = "/tmp/absolute"
        r2 = await app_client.put("/api/settings", json=current)
        assert r2.status_code == 422
        assert "workspace_root" in r2.json()["detail"]


# ---------------------------------------------------------------------------
# Fix #1 — event bus replay buffer for lifecycle events
# ---------------------------------------------------------------------------


class TestEventBusReplayBuffer:
    """Verify replay-buffer behaviour introduced to fix late-subscriber race."""

    async def test_late_subscriber_receives_lifecycle_events(self) -> None:
        """A subscriber that connects AFTER publish still receives lifecycle events."""
        from yukar.events import bus as event_bus
        from yukar.events.bus import subscribe
        from yukar.models.events import RunCompletedEvent, RunStartedEvent

        started = RunStartedEvent(project_id="rp1", epic_id="ep1", run_id="r1")
        completed = RunCompletedEvent(project_id="rp1", epic_id="ep1", run_id="r1")

        # Publish before any subscriber exists.
        event_bus.publish("rp1", "ep1", started)
        event_bus.publish("rp1", "ep1", completed)

        # Now subscribe — should receive replayed events immediately.
        async with subscribe("rp1", "ep1") as q:
            ev1 = await asyncio.wait_for(q.get(), timeout=1.0)
            ev2 = await asyncio.wait_for(q.get(), timeout=1.0)

        assert getattr(ev1, "type", None) == "run_started"
        assert getattr(ev2, "type", None) == "run_completed"

    async def test_token_event_not_replayed(self) -> None:
        """TokenEvent must NOT be stored in the replay buffer."""
        from yukar.events import bus as event_bus
        from yukar.events.bus import subscribe
        from yukar.models.events import RunStartedEvent, TokenEvent

        started = RunStartedEvent(project_id="rp2", epic_id="ep2", run_id="r2")
        token = TokenEvent(project_id="rp2", epic_id="ep2", run_id="r2", thread_id="t1", delta="x")

        event_bus.publish("rp2", "ep2", started)
        event_bus.publish("rp2", "ep2", token)

        # Late subscriber — only the lifecycle event should be replayed.
        async with subscribe("rp2", "ep2") as q:
            ev = await asyncio.wait_for(q.get(), timeout=1.0)
            # Queue should now be empty (token was not buffered).
            assert q.empty(), "TokenEvent must not be in the replay buffer"

        assert getattr(ev, "type", None) == "run_started"

    async def test_sentinel_not_replayed(self) -> None:
        """None sentinel must NOT be stored in the replay buffer."""
        from yukar.events import bus as event_bus
        from yukar.events.bus import subscribe
        from yukar.models.events import RunStartedEvent

        started = RunStartedEvent(project_id="rp3", epic_id="ep3", run_id="r3")
        event_bus.publish("rp3", "ep3", started)
        event_bus.publish("rp3", "ep3", None)  # Sentinel — must not be buffered.

        # Late subscriber: should see the replayed lifecycle event, not None.
        async with subscribe("rp3", "ep3") as q:
            # Drain replayed events (should be exactly one).
            ev = await asyncio.wait_for(q.get(), timeout=1.0)
            assert getattr(ev, "type", None) == "run_started"
            # The queue must be empty; sentinel was not replayed.
            assert q.empty(), "None sentinel must not be replayed to late subscriber"

    async def test_new_run_clears_old_buffer(self) -> None:
        """Publishing a new RunStartedEvent clears the previous run's buffer."""
        from yukar.events import bus as event_bus
        from yukar.events.bus import subscribe
        from yukar.models.events import RunCompletedEvent, RunStartedEvent

        # Publish first run lifecycle.
        run1_started = RunStartedEvent(project_id="rp4", epic_id="ep4", run_id="run-1")
        run1_completed = RunCompletedEvent(project_id="rp4", epic_id="ep4", run_id="run-1")
        event_bus.publish("rp4", "ep4", run1_started)
        event_bus.publish("rp4", "ep4", run1_completed)

        # Start a new run — old buffer must be cleared.
        run2_started = RunStartedEvent(project_id="rp4", epic_id="ep4", run_id="run-2")
        event_bus.publish("rp4", "ep4", run2_started)

        # Late subscriber should only see run-2 events (not run-1 completed).
        async with subscribe("rp4", "ep4") as q:
            ev = await asyncio.wait_for(q.get(), timeout=1.0)
            assert q.empty(), (
                "Buffer should contain only run-2 RunStartedEvent; run-1 events must be cleared"
            )

        assert getattr(ev, "run_id", None) == "run-2", (
            f"Expected run_id='run-2', got {getattr(ev, 'run_id', None)!r}"
        )


# ---------------------------------------------------------------------------
# Fix #2 — resolve_runner: MERGE_HEAD is not left behind on CancelledError path
# ---------------------------------------------------------------------------


class TestResolveRunnerCancelCleanup:
    """Verify that Task.cancel() during a resolve run leaves no MERGE_HEAD."""

    async def test_task_cancel_aborts_merge(self, tmp_path: Path) -> None:
        """After asyncio.Task.cancel(), MERGE_HEAD must be absent in the worktree."""
        import os
        import subprocess
        from unittest.mock import patch

        from yukar.config import paths as p
        from yukar.config.settings import LLMSettings
        from yukar.git.resolve import merge_in_progress
        from yukar.llm.fake import FakeModel, TextTurn
        from yukar.models.epic import Epic
        from yukar.models.project import Project, Repo, RepoCommands
        from yukar.runs.resolve_runner import ResolveRunner
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project, save_repo

        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@t.com",
        }

        # Build a repo with a conflict so the merge is left in-progress.
        repo = tmp_path / "cancel-repo"
        repo.mkdir()

        def g(*args: str) -> str:
            r = subprocess.run(
                ["git", *args], cwd=str(repo), capture_output=True, text=True, env=env
            )
            assert r.returncode == 0, f"git {args}: {r.stderr}"
            return r.stdout.strip()

        g("init", "-b", "main")
        g("config", "user.email", "t@t.com")
        g("config", "user.name", "T")
        (repo / "file.txt").write_text("original\n")
        g("add", ".")
        g("commit", "-m", "initial")

        epic_branch = "yukar/ep-1-cancel"
        g("checkout", "-b", epic_branch)
        (repo / "file.txt").write_text("epic side\n")
        g("add", ".")
        g("commit", "-m", "epic")
        g("checkout", "main")
        (repo / "file.txt").write_text("main side\n")
        g("add", ".")
        g("commit", "-m", "main")

        # Bootstrap workspace.
        root = str(tmp_path / "ws")
        project_id = "cancel-proj"
        epic_id = "EP-1"

        project = Project(id=project_id, name=project_id, repos=[repo.name])
        await save_project(root, project)
        repo_model = Repo(
            name=repo.name,
            path=str(repo),
            default_branch="main",
            commands=RepoCommands(allow=[], deny=[]),
        )
        await save_repo(root, project_id, repo_model)
        epic = Epic(id=epic_id, slug="cancel-epic", title="Cancel Test", branch=epic_branch)
        await save_epic(root, project_id, epic)

        worktree = p.worktree_dir(root, project_id, epic_id, "manager", repo.name)

        # The fake agent hangs forever (simulates a slow agent that gets cancelled).
        # We patch Agent.stream_async on the class so it blocks in the async loop.
        hang_event = asyncio.Event()

        async def hanging_stream_async(self: object, prompt: str):  # type: ignore[misc]
            await hang_event.wait()  # blocks until cancelled
            return
            yield  # make it an async generator

        fake_model = FakeModel(script=[TextTurn("never reached")])

        def fake_create_model(settings: object, role: object = None) -> FakeModel:
            return fake_model

        from strands import Agent

        with (
            patch("yukar.runs.resolve_runner.create_model", side_effect=fake_create_model),
            patch.object(Agent, "stream_async", hanging_stream_async),
        ):
            runner = ResolveRunner(
                llm_settings=LLMSettings(provider="fake"),
                repo_name=repo.name,
                git_author_name="yukar",
                git_author_email="yukar@localhost",
            )
            task = asyncio.create_task(runner.start(root, project_id, epic_id, "cancel-run"))
            # Give the runner enough time to start the merge and block in the agent.
            await asyncio.sleep(0.5)

            # Hard-cancel the task (simulates supervisor 5s timeout path).
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        # MERGE_HEAD must be absent — the finally clause must have aborted.
        assert not await merge_in_progress(worktree), (
            "MERGE_HEAD must be absent after Task.cancel(); finally clause must abort merge"
        )


# ---------------------------------------------------------------------------
# Storage resilience — one corrupt record must not 500 the whole collection
# (the "EP-6 disappeared" bug class). list_epics / list_projects / list_repos
# must skip a malformed YAML and return the remaining entries.
# ---------------------------------------------------------------------------


class TestListEpicsSkipsCorruptEntry:
    async def test_corrupt_epic_yaml_does_not_abort_list(self, tmp_workspace: Path) -> None:
        """A malformed epic.yaml must be skipped, not raise out of list_epics."""
        from yukar.config import paths
        from yukar.models.epic import Epic
        from yukar.storage import epic_repo

        root = str(tmp_workspace)
        project_id = "proj"

        good = Epic(id="EP-1", slug="good", title="Good epic")
        await epic_repo.save_epic(root, project_id, good)

        # A second epic whose YAML is unparseable garbage.
        bad_yaml = paths.epic_yaml(root, project_id, "EP-2")
        bad_yaml.parent.mkdir(parents=True, exist_ok=True)
        bad_yaml.write_text("{ this: is: not: valid: yaml ][", encoding="utf-8")

        epics = await epic_repo.list_epics(root, project_id)

        ids = [e.id for e in epics]
        assert ids == ["EP-1"], f"corrupt EP-2 must be skipped, got {ids}"

    async def test_schema_invalid_epic_yaml_does_not_abort_list(self, tmp_workspace: Path) -> None:
        """Well-formed YAML that fails Epic schema validation is also skipped."""
        from yukar.config import paths
        from yukar.models.epic import Epic
        from yukar.storage import epic_repo

        root = str(tmp_workspace)
        project_id = "proj"

        good = Epic(id="EP-1", slug="good", title="Good epic")
        await epic_repo.save_epic(root, project_id, good)

        # Valid YAML, but missing the required id/slug/title → ValidationError.
        bad_yaml = paths.epic_yaml(root, project_id, "EP-2")
        bad_yaml.parent.mkdir(parents=True, exist_ok=True)
        bad_yaml.write_text("description: orphaned\n", encoding="utf-8")

        epics = await epic_repo.list_epics(root, project_id)
        assert [e.id for e in epics] == ["EP-1"]


class TestListProjectsSkipsCorruptEntry:
    async def test_corrupt_project_yaml_does_not_abort_list(self, tmp_workspace: Path) -> None:
        """A malformed project.yaml must be skipped, not raise out of list_projects."""
        from yukar.config import paths
        from yukar.models.project import Project
        from yukar.storage import project_repo

        root = str(tmp_workspace)

        good = Project(id="proj-good", name="Good Project")
        await project_repo.save_project(root, good)

        # A second project dir with unparseable project.yaml.
        bad_yaml = paths.project_yaml(root, "proj-bad")
        bad_yaml.parent.mkdir(parents=True, exist_ok=True)
        bad_yaml.write_text(":\n  - not valid ]]", encoding="utf-8")

        projects = await project_repo.list_projects(root)

        ids = [p.id for p in projects]
        assert ids == ["proj-good"], f"corrupt proj-bad must be skipped, got {ids}"


class TestListReposSkipsCorruptEntry:
    async def test_corrupt_repo_yaml_does_not_abort_list(self, tmp_workspace: Path) -> None:
        """A malformed repo YAML must be skipped, not raise out of list_repos."""
        from yukar.config import paths
        from yukar.models.project import Repo
        from yukar.storage import project_repo

        root = str(tmp_workspace)
        project_id = "proj"

        good = Repo(name="good-repo", path="/tmp/good-repo")
        await project_repo.save_repo(root, project_id, good)

        # A second repo YAML that is unparseable.
        bad_yaml = paths.repo_yaml(root, project_id, "bad-repo")
        bad_yaml.parent.mkdir(parents=True, exist_ok=True)
        bad_yaml.write_text("name: [unterminated", encoding="utf-8")

        repos = await project_repo.list_repos(root, project_id)

        names = [r.name for r in repos]
        assert names == ["good-repo"], f"corrupt bad-repo must be skipped, got {names}"


# ---------------------------------------------------------------------------
# Frontmatter parse failure must not leak the raw '---...---' block as body.
# ---------------------------------------------------------------------------


class TestFrontmatterParseFailure:
    def test_malformed_frontmatter_does_not_leak_block_as_body(self) -> None:
        """On a YAML parse error the '---...---' block must not become body."""
        from yukar.storage.frontmatter_io import parse_frontmatter

        content = "---\nkey: [unterminated\n---\nreal body text\n"
        meta, body = parse_frontmatter(content)

        assert meta == {}, "metadata must be empty on parse failure"
        assert "---" not in body, "the frontmatter delimiters must not leak into body"
        assert "key: [unterminated" not in body, "the raw YAML block must not leak into body"
        assert body == "real body text\n"

    def test_non_mapping_frontmatter_does_not_leak_block_as_body(self) -> None:
        """Well-formed but non-dict frontmatter yields empty meta and the trailing body."""
        from yukar.storage.frontmatter_io import parse_frontmatter

        content = "---\n- just\n- a\n- list\n---\nbody after list\n"
        meta, body = parse_frontmatter(content)

        assert meta == {}
        assert body == "body after list\n"

    def test_well_formed_frontmatter_still_parses(self) -> None:
        """Regression guard: valid frontmatter still yields meta + body."""
        from yukar.storage.frontmatter_io import parse_frontmatter

        content = "---\nname: thing\ndescription: a thing\n---\nthe body\n"
        meta, body = parse_frontmatter(content)

        assert meta == {"name": "thing", "description": "a thing"}
        assert body == "the body\n"

    def test_no_frontmatter_returns_content_unchanged(self) -> None:
        """Regression guard: content without a leading '---' is returned as-is."""
        from yukar.storage.frontmatter_io import parse_frontmatter

        content = "just some markdown\nno frontmatter here\n"
        meta, body = parse_frontmatter(content)

        assert meta == {}
        assert body == content


# ---------------------------------------------------------------------------
# atomic_write_bytes durability — fsync added before/after os.replace.
# fsync itself is not unit-testable; assert the written content is intact and
# no temp files are left behind.
# ---------------------------------------------------------------------------


class TestAtomicWriteDurability:
    async def test_content_intact_and_no_temp_leftovers(self, tmp_path: Path) -> None:
        from yukar.storage.atomic import atomic_write_bytes

        target = tmp_path / "sub" / "out.bin"
        payload = b"durable\x00bytes\n" * 100
        await atomic_write_bytes(target, payload)

        assert target.read_bytes() == payload
        # The temp file (".tmp_*") must have been renamed away, not left behind.
        leftovers = list(target.parent.glob(".tmp_*"))
        assert leftovers == [], f"temp files leaked: {leftovers}"

    async def test_overwrite_replaces_content(self, tmp_path: Path) -> None:
        from yukar.storage.atomic import atomic_write_bytes

        target = tmp_path / "out.bin"
        await atomic_write_bytes(target, b"first")
        await atomic_write_bytes(target, b"second")
        assert target.read_bytes() == b"second"
