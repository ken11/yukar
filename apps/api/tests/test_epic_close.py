"""Tests for Epic Close (Feature 1) — status widening, close endpoint, guards,
include_closed filter, event scaffolding.

Covers:
1. Status enum widening: Epic model accepts "closed" and "merged".
2. POST /epics/{epic_id}/close — sets status="closed", returns Epic.
3. POST /epics/{epic_id}/close — 409 when run is active.
4. POST /run — 409 when epic is closed.
5. supervisor.start() — RuntimeError when epic is closed (TOCTOU guard).
6. supervisor.start_continuation() — RuntimeError when epic is closed.
7. list_epics include_closed=False hides closed; include_closed=True shows them.
8. PATCH /epics/{epic_id} can reopen a closed epic (status → planned/in_progress).
9. EpicStatusChangedEvent published on close.
10. EpicMergeProgressEvent and EpicMergeResult model validation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _write_project_epic(
    root: str,
    project_id: str = "proj",
    epic_id: str = "EP-1",
    status: str = "planned",
) -> None:
    from yukar.models.epic import Epic
    from yukar.models.project import Project
    from yukar.storage.epic_repo import save_epic
    from yukar.storage.project_repo import save_project

    await save_project(root, Project(id=project_id, name=project_id))
    # Use model_validate so the str status passes Pydantic's Literal check at
    # runtime without triggering a static type error in the helper signature.
    epic = Epic.model_validate(
        {
            "id": epic_id,
            "slug": "test",
            "title": "Test",
            "branch": "yukar/ep-1-test",
            "status": status,
        }
    )
    await save_epic(root, project_id, epic)


# ---------------------------------------------------------------------------
# 1. Status enum widening
# ---------------------------------------------------------------------------


class TestEpicStatusWidening:
    def test_closed_status_accepted(self) -> None:
        from yukar.models.epic import Epic

        e = Epic(id="EP-1", slug="s", title="T", status="closed")
        assert e.status == "closed"

    def test_merged_status_accepted(self) -> None:
        from yukar.models.epic import Epic

        e = Epic(id="EP-1", slug="s", title="T", status="merged")
        assert e.status == "merged"

    def test_default_status_still_planned(self) -> None:
        from yukar.models.epic import Epic

        e = Epic(id="EP-1", slug="s", title="T")
        assert e.status == "planned"

    def test_patch_request_accepts_closed(self) -> None:
        from yukar.api.routers.epics import PatchEpicRequest

        req = PatchEpicRequest(status="closed")
        assert req.status == "closed"

    def test_patch_request_accepts_merged(self) -> None:
        from yukar.api.routers.epics import PatchEpicRequest

        req = PatchEpicRequest(status="merged")
        assert req.status == "merged"


# ---------------------------------------------------------------------------
# 2. POST /epics/{epic_id}/close — happy path
# ---------------------------------------------------------------------------


class TestCloseEndpointHappyPath:
    async def test_close_sets_status_closed(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)

        resp = await app_client.post(f"/api/projects/{pid}/epics/{eid}/close")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "closed"
        assert body["id"] == eid

    async def test_close_persists_to_disk(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)

        await app_client.post(f"/api/projects/{pid}/epics/{eid}/close")

        from yukar.storage.epic_repo import get_epic

        loaded = await get_epic(root, pid, eid)
        assert loaded is not None
        assert loaded.status == "closed"

    async def test_close_idempotent(self, app_client: Any, tmp_workspace: Path) -> None:
        """Closing an already-closed epic succeeds (status stays 'closed')."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="closed")

        resp = await app_client.post(f"/api/projects/{pid}/epics/{eid}/close")
        assert resp.status_code == 200
        assert resp.json()["status"] == "closed"

    async def test_close_404_for_missing_epic(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        pid = "proj"
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        await save_project(root, Project(id=pid, name=pid))

        resp = await app_client.post(f"/api/projects/{pid}/epics/EP-99/close")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 3. POST /close — 409 when run is active
# ---------------------------------------------------------------------------


class TestCloseEndpoint409WhenRunning:
    async def test_close_409_when_run_active(self, app_client: Any, tmp_workspace: Path) -> None:
        """Close must return 409 if a run is currently active."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)

        from yukar.runs.supervisor import get_supervisor

        sv = get_supervisor()
        # Fake an active run by injecting a handle with a non-done task.
        import asyncio

        async def _never_finishes() -> None:
            await asyncio.sleep(9999)

        fake_task: asyncio.Task[None] = asyncio.create_task(_never_finishes())
        try:
            from unittest.mock import MagicMock

            from yukar.runs.supervisor import _RunHandle

            sv._runs[(pid, eid)] = _RunHandle(
                run_id="run-fake",
                runner=MagicMock(),
                task=fake_task,
                root=root,
                project_id=pid,
                epic_id=eid,
            )

            resp = await app_client.post(f"/api/projects/{pid}/epics/{eid}/close")
            assert resp.status_code == 409
            assert "run is active" in resp.json()["detail"].lower()
        finally:
            fake_task.cancel()
            import contextlib

            with contextlib.suppress(Exception, asyncio.CancelledError):
                await fake_task
            sv._runs.pop((pid, eid), None)


# ---------------------------------------------------------------------------
# 4. POST /run — 409 when epic is closed
# ---------------------------------------------------------------------------


class TestStartRunRejectedWhenClosed:
    async def test_start_run_returns_409_for_closed_epic(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="closed")

        resp = await app_client.post(f"/api/projects/{pid}/epics/{eid}/run")
        assert resp.status_code == 409
        assert "closed" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 5. supervisor.start() TOCTOU guard
# ---------------------------------------------------------------------------


class TestSupervisorStartGuard:
    async def test_start_raises_runtime_error_when_closed(self, tmp_path: Path) -> None:
        """supervisor.start() must raise RuntimeError if the epic is closed."""
        root = str(tmp_path / "ws")
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="closed")

        from yukar.runs.supervisor import RunSupervisor

        sv = RunSupervisor()
        with pytest.raises(RuntimeError, match="closed"):
            await sv.start(root, pid, eid)


# ---------------------------------------------------------------------------
# 6. supervisor.start_continuation() TOCTOU guard
# ---------------------------------------------------------------------------


class TestSupervisorStartContinuationGuard:
    async def test_start_continuation_raises_when_closed(self, tmp_path: Path) -> None:
        """supervisor.start_continuation() must raise RuntimeError if epic is closed."""
        root = str(tmp_path / "ws")
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="closed")

        from yukar.runs.supervisor import RunSupervisor

        sv = RunSupervisor()
        with pytest.raises(RuntimeError, match="closed"):
            await sv.start_continuation(root, pid, eid)


# ---------------------------------------------------------------------------
# 7. list_epics include_closed filter
# ---------------------------------------------------------------------------


class TestListEpicsIncludeClosed:
    async def test_default_hides_closed(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        pid = "proj"
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        await save_project(root, Project(id=pid, name=pid))
        await save_epic(
            root,
            pid,
            Epic(id="EP-1", slug="open", title="Open", status="planned"),
        )
        await save_epic(
            root,
            pid,
            Epic(id="EP-2", slug="closed", title="Closed", status="closed"),
        )

        resp = await app_client.get(f"/api/projects/{pid}/epics")
        assert resp.status_code == 200
        ids = [e["id"] for e in resp.json()]
        assert "EP-1" in ids
        assert "EP-2" not in ids

    async def test_include_closed_true_shows_all(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        root = str(tmp_workspace)
        pid = "proj"
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        await save_project(root, Project(id=pid, name=pid))
        await save_epic(
            root,
            pid,
            Epic(id="EP-1", slug="open", title="Open", status="planned"),
        )
        await save_epic(
            root,
            pid,
            Epic(id="EP-2", slug="closed", title="Closed", status="closed"),
        )

        resp = await app_client.get(f"/api/projects/{pid}/epics?include_closed=true")
        assert resp.status_code == 200
        ids = [e["id"] for e in resp.json()]
        assert "EP-1" in ids
        assert "EP-2" in ids

    async def test_merged_epic_not_filtered(self, app_client: Any, tmp_workspace: Path) -> None:
        """Merged epics are NOT filtered by include_closed=false — only 'closed' is."""
        root = str(tmp_workspace)
        pid = "proj"
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        await save_project(root, Project(id=pid, name=pid))
        await save_epic(
            root,
            pid,
            Epic(id="EP-1", slug="merged", title="Merged", status="merged"),
        )

        resp = await app_client.get(f"/api/projects/{pid}/epics")
        assert resp.status_code == 200
        ids = [e["id"] for e in resp.json()]
        assert "EP-1" in ids


# ---------------------------------------------------------------------------
# 8. PATCH can reopen a closed epic
# ---------------------------------------------------------------------------


class TestPatchReopensClosedEpic:
    async def test_patch_status_planned_reopens_epic(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="closed")

        resp = await app_client.patch(
            f"/api/projects/{pid}/epics/{eid}",
            json={"status": "planned"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "planned"

    async def test_patch_status_in_progress(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="closed")

        resp = await app_client.patch(
            f"/api/projects/{pid}/epics/{eid}",
            json={"status": "in_progress"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "in_progress"


# ---------------------------------------------------------------------------
# 9. EpicStatusChangedEvent published on close
# ---------------------------------------------------------------------------


class TestEpicStatusChangedEventOnClose:
    async def test_close_publishes_event(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)

        from yukar.events import bus as event_bus
        from yukar.models.events import EpicStatusChangedEvent

        received: list[EpicStatusChangedEvent] = []

        async def _collect() -> None:
            async with event_bus.subscribe(pid, eid) as q:
                ev = await q.get()
                if isinstance(ev, EpicStatusChangedEvent):
                    received.append(ev)

        import asyncio

        collector = asyncio.create_task(_collect())
        # Allow collector to register before close is called.
        await asyncio.sleep(0)

        resp = await app_client.post(f"/api/projects/{pid}/epics/{eid}/close")
        assert resp.status_code == 200

        await asyncio.wait_for(collector, timeout=2.0)
        assert len(received) == 1
        assert received[0].status == "closed"
        assert received[0].epic_id == eid
        assert received[0].run_id == ""

    async def test_close_event_reaches_project_stream(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """EpicStatusChangedEvent is in _LIFECYCLE_TYPES so it fans out to project queues."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)

        from yukar.events import bus as event_bus
        from yukar.models.events import EpicStatusChangedEvent

        received: list[EpicStatusChangedEvent] = []

        async def _collect_project() -> None:
            async with event_bus.subscribe_project(pid) as q:
                ev = await q.get()
                if isinstance(ev, EpicStatusChangedEvent):
                    received.append(ev)

        import asyncio

        collector = asyncio.create_task(_collect_project())
        await asyncio.sleep(0)

        await app_client.post(f"/api/projects/{pid}/epics/{eid}/close")

        await asyncio.wait_for(collector, timeout=2.0)
        assert len(received) == 1
        assert received[0].status == "closed"


# ---------------------------------------------------------------------------
# 10. EpicMergeProgressEvent / EpicMergeResult model validation
# ---------------------------------------------------------------------------


class TestEpicMergeEventModels:
    def test_epic_merge_result_defaults(self) -> None:
        from yukar.models.events import EpicMergeResult

        r = EpicMergeResult(epic_id="EP-1", status="merged")
        assert r.detail == ""
        assert r.repos == []

    def test_epic_merge_result_all_statuses(self) -> None:
        from yukar.models.events import EpicMergeResult

        statuses = ["merged", "conflict_unresolved", "vetting_refused", "skipped", "error"]
        for s in statuses:
            r = EpicMergeResult.model_validate({"epic_id": "EP-1", "status": s})
            assert r.status == s

    def test_epic_merge_progress_event_defaults(self) -> None:
        from yukar.models.events import EpicMergeProgressEvent

        ev = EpicMergeProgressEvent(
            project_id="p",
            epic_id="",
            run_id="run-123",
            total=3,
            completed=0,
        )
        assert ev.type == "epic_merge_progress"
        assert ev.current_epic_id is None
        assert ev.phase == ""
        assert ev.results == []

    def test_epic_merge_progress_event_with_results(self) -> None:
        from yukar.models.events import EpicMergeProgressEvent, EpicMergeResult

        ev = EpicMergeProgressEvent(
            project_id="p",
            epic_id="",
            run_id="run-123",
            total=2,
            completed=1,
            current_epic_id="EP-2",
            phase="merging",
            results=[
                EpicMergeResult(
                    epic_id="EP-1",
                    status="merged",
                    repos=["repo-a"],
                )
            ],
        )
        assert ev.completed == 1
        assert ev.results[0].epic_id == "EP-1"

    def test_epic_status_changed_event_in_run_event_union(self) -> None:
        """EpicStatusChangedEvent must be resolvable via the RunEvent discriminated union."""
        from pydantic import TypeAdapter

        from yukar.models.events import RunEvent

        ta: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
        raw = {
            "type": "epic_status_changed",
            "project_id": "p",
            "epic_id": "EP-1",
            "run_id": "",
            "ts": "2024-01-01T00:00:00+00:00",
            "status": "closed",
        }
        ev = ta.validate_python(raw)
        from yukar.models.events import EpicStatusChangedEvent

        assert isinstance(ev, EpicStatusChangedEvent)

    def test_epic_merge_progress_event_in_run_event_union(self) -> None:
        """EpicMergeProgressEvent must be resolvable via the RunEvent discriminated union."""
        from pydantic import TypeAdapter

        from yukar.models.events import RunEvent

        ta: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
        raw = {
            "type": "epic_merge_progress",
            "project_id": "p",
            "epic_id": "",
            "run_id": "run-1",
            "ts": "2024-01-01T00:00:00+00:00",
            "total": 3,
            "completed": 1,
        }
        ev = ta.validate_python(raw)
        from yukar.models.events import EpicMergeProgressEvent

        assert isinstance(ev, EpicMergeProgressEvent)


# ---------------------------------------------------------------------------
# 11. EpicStatusChangedEvent published on PATCH status change
# ---------------------------------------------------------------------------


class TestEpicStatusChangedEventOnPatch:
    async def test_patch_status_change_publishes_event(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """PATCH with a new status value publishes EpicStatusChangedEvent."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="planned")

        import asyncio

        from yukar.events import bus as event_bus
        from yukar.models.events import EpicStatusChangedEvent

        received: list[EpicStatusChangedEvent] = []

        async def _collect() -> None:
            async with event_bus.subscribe(pid, eid) as q:
                ev = await q.get()
                if isinstance(ev, EpicStatusChangedEvent):
                    received.append(ev)

        collector = asyncio.create_task(_collect())
        # Allow collector to register before patch is called.
        await asyncio.sleep(0)

        resp = await app_client.patch(
            f"/api/projects/{pid}/epics/{eid}",
            json={"status": "in_progress"},
        )
        assert resp.status_code == 200

        await asyncio.wait_for(collector, timeout=2.0)
        assert len(received) == 1
        assert received[0].status == "in_progress"
        assert received[0].epic_id == eid
        assert received[0].run_id == ""

    async def test_patch_title_only_does_not_publish_event(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """PATCH with only a title change must NOT publish EpicStatusChangedEvent."""
        import contextlib

        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="planned")

        import asyncio

        from yukar.events import bus as event_bus
        from yukar.models.events import EpicStatusChangedEvent

        received: list[EpicStatusChangedEvent] = []

        async def _collect() -> None:
            async with event_bus.subscribe(pid, eid) as q:
                ev = await q.get()
                if isinstance(ev, EpicStatusChangedEvent):
                    received.append(ev)

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        resp = await app_client.patch(
            f"/api/projects/{pid}/epics/{eid}",
            json={"title": "New Title"},
        )
        assert resp.status_code == 200

        # Give a moment for any unexpected event to arrive.
        await asyncio.sleep(0.1)
        assert len(received) == 0
        collector.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await collector

    async def test_patch_same_status_does_not_publish_event(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """PATCH setting the same status that is already set must NOT publish an event."""
        import contextlib

        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="in_progress")

        import asyncio

        from yukar.events import bus as event_bus
        from yukar.models.events import EpicStatusChangedEvent

        received: list[EpicStatusChangedEvent] = []

        async def _collect() -> None:
            async with event_bus.subscribe(pid, eid) as q:
                ev = await q.get()
                if isinstance(ev, EpicStatusChangedEvent):
                    received.append(ev)

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        resp = await app_client.patch(
            f"/api/projects/{pid}/epics/{eid}",
            json={"status": "in_progress"},
        )
        assert resp.status_code == 200

        # Give a moment for any unexpected event to arrive.
        await asyncio.sleep(0.1)
        assert len(received) == 0
        collector.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await collector
