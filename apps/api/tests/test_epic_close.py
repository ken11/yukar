"""Tests for the 1-bit epic lifecycle (open ⇄ completed, user-owned).

Covers:
1. Legacy status migration (BeforeValidator): ALL legacy values → completed
   (pre-redesign epics lock as finished history); merged back-fills ``merged_at``
   from ``updated_at``.
2. PATCH {status: "completed"} — the user's single "finish" action (approve or
   abandon), 409 while a run is active, idempotent.
3. PATCH {status: "open"} — explicit reopen.
4. POST /run — 409 when the epic is completed (no implicit reopen).
5. supervisor.start() / start_continuation() — RuntimeError when the epic is
   completed (TOCTOU guard; manager runs only — reviewer coverage lives in
   test_reviewer_role.py).
6. list_epics include_completed=False hides completed; =True shows them.
7. EpicStatusChangedEvent published on PATCH status changes.
8. EpicMergedEvent / EpicMergeProgressEvent / EpicMergeResult model validation.
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
    status: str = "open",
) -> None:
    from yukar.models.epic import Epic
    from yukar.models.project import Project
    from yukar.storage.epic_repo import save_epic
    from yukar.storage.project_repo import save_project

    await save_project(root, Project(id=project_id, name=project_id))
    # Use model_validate so a str status passes Pydantic's Literal check at
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


def _inject_fake_active_run(root: str, project_id: str, epic_id: str) -> Any:
    """Register a never-finishing run handle; caller must clean it up."""
    import asyncio
    from unittest.mock import MagicMock

    from yukar.runs.supervisor import _RunHandle, get_supervisor

    sv = get_supervisor()

    async def _never() -> None:
        await asyncio.sleep(9999)

    fake_task: asyncio.Task[None] = asyncio.create_task(_never())
    sv._runs[(project_id, epic_id)] = _RunHandle(
        run_id="run-fake",
        runner=MagicMock(),
        task=fake_task,
        root=root,
        project_id=project_id,
        epic_id=epic_id,
    )
    return fake_task


async def _cleanup_fake_run(fake_task: Any, project_id: str, epic_id: str) -> None:
    import asyncio
    import contextlib

    from yukar.runs.supervisor import get_supervisor

    fake_task.cancel()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await fake_task
    get_supervisor()._runs.pop((project_id, epic_id), None)


# ---------------------------------------------------------------------------
# 1. Legacy status migration (lazy, BeforeValidator)
# ---------------------------------------------------------------------------


class TestLegacyStatusMigration:
    """Old epic.yaml files carry the pre-redesign 7-value vocabulary; the
    model maps them onto the 1-bit lifecycle on every read."""

    @pytest.mark.parametrize(
        ("legacy", "expected"),
        [
            # ALL legacy values lock as completed: pre-redesign epics are
            # finished history; the user reopens the ones worth resuming.
            ("planned", "completed"),
            ("in_progress", "completed"),
            ("in_review", "completed"),
            ("failed", "completed"),
            ("closed", "completed"),
            ("merged", "completed"),
            ("completed", "completed"),
        ],
    )
    def test_legacy_values_map_to_one_bit(self, legacy: str, expected: str) -> None:
        from yukar.models.epic import Epic

        e = Epic.model_validate({"id": "EP-1", "slug": "s", "title": "T", "status": legacy})
        assert e.status == expected

    def test_new_value_open_passes_through(self) -> None:
        from yukar.models.epic import Epic

        e = Epic(id="EP-1", slug="s", title="T", status="open")
        assert e.status == "open"

    def test_default_status_is_open(self) -> None:
        from yukar.models.epic import Epic

        e = Epic(id="EP-1", slug="s", title="T")
        assert e.status == "open"
        assert e.merged_at is None

    def test_merged_backfills_merged_at_from_updated_at(self) -> None:
        from yukar.models.epic import Epic

        e = Epic.model_validate(
            {
                "id": "EP-1",
                "slug": "s",
                "title": "T",
                "status": "merged",
                "updated_at": "2025-05-01T12:00:00+00:00",
            }
        )
        assert e.status == "completed"
        assert e.merged_at is not None
        assert e.merged_at == e.updated_at

    def test_merged_keeps_existing_merged_at(self) -> None:
        from yukar.models.epic import Epic

        e = Epic.model_validate(
            {
                "id": "EP-1",
                "slug": "s",
                "title": "T",
                "status": "merged",
                "merged_at": "2025-04-01T00:00:00+00:00",
                "updated_at": "2025-05-01T12:00:00+00:00",
            }
        )
        assert e.status == "completed"
        assert e.merged_at is not None
        assert e.merged_at.isoformat() == "2025-04-01T00:00:00+00:00"

    def test_merged_without_updated_at_leaves_merged_at_none(self) -> None:
        """Constructor-style input without updated_at: the fact timestamp is
        unknown, so it stays None (no fabricated timestamp)."""
        from yukar.models.epic import Epic

        e = Epic.model_validate({"id": "EP-1", "slug": "s", "title": "T", "status": "merged"})
        assert e.status == "completed"
        assert e.merged_at is None

    def test_non_merged_legacy_does_not_touch_merged_at(self) -> None:
        from yukar.models.epic import Epic

        e = Epic.model_validate(
            {
                "id": "EP-1",
                "slug": "s",
                "title": "T",
                "status": "closed",
                "updated_at": "2025-05-01T12:00:00+00:00",
            }
        )
        assert e.status == "completed"
        assert e.merged_at is None

    async def test_legacy_yaml_on_disk_loads_via_get_epic(self, tmp_path: Path) -> None:
        """A pre-redesign epic.yaml on disk loads without ValidationError and
        reads back with the new vocabulary (lazy migration on read)."""
        import yaml

        from yukar.config import paths
        from yukar.storage.epic_repo import get_epic, list_epics

        root = str(tmp_path / "ws")
        pid, eid = "proj", "EP-legacy"
        yaml_path = paths.epic_yaml(root, pid, eid)
        yaml_path.parent.mkdir(parents=True)
        yaml_path.write_text(
            yaml.safe_dump(
                {
                    "id": eid,
                    "slug": "legacy",
                    "title": "Legacy",
                    "status": "in_review",
                    "created_at": "2025-05-01T00:00:00+00:00",
                    "updated_at": "2025-05-02T00:00:00+00:00",
                }
            )
        )

        loaded = await get_epic(root, pid, eid)
        assert loaded is not None
        assert loaded.status == "completed"
        # list_epics (log-and-skip path) must not silently drop the epic.
        # include_completed=True equivalent: list_epics itself returns all.
        listed = await list_epics(root, pid)
        assert [e.id for e in listed] == [eid]

    async def test_legacy_merged_yaml_on_disk_backfills_merged_at(self, tmp_path: Path) -> None:
        import yaml

        from yukar.config import paths
        from yukar.storage.epic_repo import get_epic

        root = str(tmp_path / "ws")
        pid, eid = "proj", "EP-merged"
        yaml_path = paths.epic_yaml(root, pid, eid)
        yaml_path.parent.mkdir(parents=True)
        yaml_path.write_text(
            yaml.safe_dump(
                {
                    "id": eid,
                    "slug": "m",
                    "title": "M",
                    "status": "merged",
                    "created_at": "2025-05-01T00:00:00+00:00",
                    "updated_at": "2025-05-02T00:00:00+00:00",
                }
            )
        )

        loaded = await get_epic(root, pid, eid)
        assert loaded is not None
        assert loaded.status == "completed"
        assert loaded.merged_at == loaded.updated_at


# ---------------------------------------------------------------------------
# 2. PATCH {status: "completed"} — the user's finish action
# ---------------------------------------------------------------------------


class TestCompleteViaPatch:
    async def test_complete_sets_status_and_persists(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)

        resp = await app_client.patch(
            f"/api/projects/{pid}/epics/{eid}",
            json={"status": "completed"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "completed"

        from yukar.storage.epic_repo import get_epic

        loaded = await get_epic(root, pid, eid)
        assert loaded is not None
        assert loaded.status == "completed"

    async def test_complete_is_idempotent(self, app_client: Any, tmp_workspace: Path) -> None:
        """Completing an already-completed epic succeeds (stays completed)."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="completed")

        resp = await app_client.patch(
            f"/api/projects/{pid}/epics/{eid}",
            json={"status": "completed"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

    async def test_complete_404_for_missing_epic(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        root = str(tmp_workspace)
        pid = "proj"
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        await save_project(root, Project(id=pid, name=pid))

        resp = await app_client.patch(
            f"/api/projects/{pid}/epics/EP-99",
            json={"status": "completed"},
        )
        assert resp.status_code == 404

    async def test_complete_409_when_run_active(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """Completing must return 409 while a run is active (stop it first)."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)

        fake_task = _inject_fake_active_run(root, pid, eid)
        try:
            resp = await app_client.patch(
                f"/api/projects/{pid}/epics/{eid}",
                json={"status": "completed"},
            )
            assert resp.status_code == 409
            assert "run is active" in resp.json()["detail"].lower()
        finally:
            await _cleanup_fake_run(fake_task, pid, eid)

    async def test_legacy_status_values_rejected_by_patch(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """The PATCH surface only accepts the new 1-bit vocabulary."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)

        for legacy in ("planned", "in_progress", "in_review", "failed", "closed", "merged"):
            resp = await app_client.patch(
                f"/api/projects/{pid}/epics/{eid}",
                json={"status": legacy},
            )
            assert resp.status_code == 422, f"{legacy} must be rejected"


# ---------------------------------------------------------------------------
# 3. PATCH {status: "open"} — explicit reopen
# ---------------------------------------------------------------------------


class TestReopenViaPatch:
    async def test_reopen_completed_epic(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="completed")

        resp = await app_client.patch(
            f"/api/projects/{pid}/epics/{eid}",
            json={"status": "open"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "open"

    async def test_reopen_then_run_starts(self, app_client: Any, tmp_workspace: Path) -> None:
        """After an explicit reopen, POST /run is allowed again."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="completed")

        r1 = await app_client.post(f"/api/projects/{pid}/epics/{eid}/run")
        assert r1.status_code == 409

        r2 = await app_client.patch(
            f"/api/projects/{pid}/epics/{eid}",
            json={"status": "open"},
        )
        assert r2.status_code == 200

        r3 = await app_client.post(f"/api/projects/{pid}/epics/{eid}/run")
        assert r3.status_code == 202, r3.text


# ---------------------------------------------------------------------------
# 4. POST /run — 409 when the epic is completed
# ---------------------------------------------------------------------------


class TestStartRunRejectedWhenCompleted:
    async def test_start_run_returns_409_for_completed_epic(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="completed")

        resp = await app_client.post(f"/api/projects/{pid}/epics/{eid}/run")
        assert resp.status_code == 409
        detail = resp.json()["detail"].lower()
        assert "completed" in detail
        assert "reopen" in detail


class TestMergeRejectedWhenCompleted:
    async def test_git_merge_returns_409_for_completed_epic(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """A merge mutates the default branch — read-only completed epics
        reject it before any repo lookup happens."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="completed")

        resp = await app_client.post(
            f"/api/projects/{pid}/epics/{eid}/git/merge",
            json={"repo": "some-repo"},
        )
        assert resp.status_code == 409
        detail = resp.json()["detail"].lower()
        assert "completed" in detail
        assert "reopen" in detail

    async def test_batch_merge_returns_409_for_completed_epic(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """The arbiter batch merge applies the same read-only rule as the
        single-repo merge — a completed epic in the selection rejects the
        whole request."""
        root = str(tmp_workspace)
        pid = "proj"
        await _write_project_epic(root, pid, "EP-1", status="open")
        await _write_project_epic(root, pid, "EP-2", status="completed")

        resp = await app_client.post(
            f"/api/projects/{pid}/merge",
            json={"epic_ids": ["EP-1", "EP-2"]},
        )
        assert resp.status_code == 409
        detail = resp.json()["detail"].lower()
        assert "ep-2" in detail
        assert "completed" in detail
        assert "reopen" in detail


class TestCreateTrialRejectedWhenCompleted:
    async def test_new_manager_trial_returns_409_for_completed_epic(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """A new manager trial (or same-branch continuation) is new work —
        completed epics are read-only until reopened."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="completed")

        resp = await app_client.post(
            f"/api/projects/{pid}/epics/{eid}/threads",
            json={"title": "Trial 2", "role": "manager"},
        )
        assert resp.status_code == 409
        detail = resp.json()["detail"].lower()
        assert "completed" in detail
        assert "reopen" in detail


# ---------------------------------------------------------------------------
# 5. supervisor.start() / start_continuation() TOCTOU guards
# ---------------------------------------------------------------------------


class TestSupervisorStartGuard:
    async def test_start_raises_runtime_error_when_completed(self, tmp_path: Path) -> None:
        """supervisor.start() must raise RuntimeError if the epic is completed."""
        root = str(tmp_path / "ws")
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="completed")

        from yukar.runs.supervisor import RunSupervisor

        sv = RunSupervisor()
        with pytest.raises(RuntimeError, match="completed"):
            await sv.start(root, pid, eid)


class TestSupervisorStartContinuationGuard:
    async def test_start_continuation_raises_when_completed(self, tmp_path: Path) -> None:
        """A manager continuation on a completed epic is rejected — continuing
        is not an implicit reopen; the user must reopen the epic first."""
        root = str(tmp_path / "ws")
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="completed")

        from yukar.runs.supervisor import RunSupervisor

        sv = RunSupervisor()
        with pytest.raises(RuntimeError, match="reopen"):
            await sv.start_continuation(root, pid, eid)


# ---------------------------------------------------------------------------
# 6. list_epics include_completed filter
# ---------------------------------------------------------------------------


class TestListEpicsIncludeCompleted:
    async def _seed_open_and_completed(self, root: str, pid: str) -> None:
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        await save_project(root, Project(id=pid, name=pid))
        await save_epic(root, pid, Epic(id="EP-1", slug="open", title="Open", status="open"))
        await save_epic(
            root, pid, Epic(id="EP-2", slug="done", title="Done", status="completed")
        )

    async def test_default_hides_completed(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        pid = "proj"
        await self._seed_open_and_completed(root, pid)

        resp = await app_client.get(f"/api/projects/{pid}/epics")
        assert resp.status_code == 200
        ids = [e["id"] for e in resp.json()]
        assert "EP-1" in ids
        assert "EP-2" not in ids

    async def test_include_completed_true_shows_all(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        root = str(tmp_workspace)
        pid = "proj"
        await self._seed_open_and_completed(root, pid)

        resp = await app_client.get(f"/api/projects/{pid}/epics?include_completed=true")
        assert resp.status_code == 200
        ids = [e["id"] for e in resp.json()]
        assert "EP-1" in ids
        assert "EP-2" in ids

    async def test_merged_fact_does_not_filter_open_epic(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """The merge fact is an attribute: a merged-but-open epic stays listed."""
        from datetime import UTC, datetime

        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        pid = "proj"
        await save_project(root, Project(id=pid, name=pid))
        await save_epic(
            root,
            pid,
            Epic(
                id="EP-1",
                slug="merged-open",
                title="Merged but open",
                status="open",
                merged_at=datetime.now(UTC),
            ),
        )

        resp = await app_client.get(f"/api/projects/{pid}/epics")
        assert resp.status_code == 200
        body = resp.json()
        assert [e["id"] for e in body] == ["EP-1"]
        assert body[0]["merged_at"] is not None


# ---------------------------------------------------------------------------
# 7. EpicStatusChangedEvent published on PATCH status change
# ---------------------------------------------------------------------------


class TestEpicStatusChangedEventOnPatch:
    async def test_complete_publishes_event(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)

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
        # Allow collector to register before the patch is issued.
        await asyncio.sleep(0)

        resp = await app_client.patch(
            f"/api/projects/{pid}/epics/{eid}",
            json={"status": "completed"},
        )
        assert resp.status_code == 200

        await asyncio.wait_for(collector, timeout=2.0)
        assert len(received) == 1
        assert received[0].status == "completed"
        assert received[0].epic_id == eid
        assert received[0].run_id == ""

    async def test_complete_event_reaches_project_stream(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """EpicStatusChangedEvent is in _LIFECYCLE_TYPES so it fans out to project queues."""
        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)

        import asyncio

        from yukar.events import bus as event_bus
        from yukar.models.events import EpicStatusChangedEvent

        received: list[EpicStatusChangedEvent] = []

        async def _collect_project() -> None:
            async with event_bus.subscribe_project(pid) as q:
                ev = await q.get()
                if isinstance(ev, EpicStatusChangedEvent):
                    received.append(ev)

        collector = asyncio.create_task(_collect_project())
        await asyncio.sleep(0)

        await app_client.patch(
            f"/api/projects/{pid}/epics/{eid}",
            json={"status": "completed"},
        )

        await asyncio.wait_for(collector, timeout=2.0)
        assert len(received) == 1
        assert received[0].status == "completed"

    async def test_patch_title_only_does_not_publish_event(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """PATCH with only a title change must NOT publish EpicStatusChangedEvent."""
        import contextlib

        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid)

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
        """PATCH setting the status that is already set must NOT publish an event."""
        import contextlib

        root = str(tmp_workspace)
        pid, eid = "proj", "EP-1"
        await _write_project_epic(root, pid, eid, status="completed")

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
            json={"status": "completed"},
        )
        assert resp.status_code == 200

        # Give a moment for any unexpected event to arrive.
        await asyncio.sleep(0.1)
        assert len(received) == 0
        collector.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await collector


# ---------------------------------------------------------------------------
# 8. Event model validation (EpicMerged / EpicMergeProgress / EpicMergeResult)
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
            "status": "completed",
        }
        ev = ta.validate_python(raw)
        from yukar.models.events import EpicStatusChangedEvent

        assert isinstance(ev, EpicStatusChangedEvent)

    def test_epic_merged_event_in_run_event_union(self) -> None:
        """EpicMergedEvent must be resolvable via the RunEvent discriminated union."""
        from pydantic import TypeAdapter

        from yukar.models.events import EpicMergedEvent, RunEvent

        ta: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
        raw = {
            "type": "epic_merged",
            "project_id": "p",
            "epic_id": "EP-1",
            "run_id": "",
            "ts": "2024-01-01T00:00:00+00:00",
            "merged_at": "2024-01-01T00:00:00+00:00",
        }
        ev = ta.validate_python(raw)
        assert isinstance(ev, EpicMergedEvent)

    def test_epic_merged_event_is_lifecycle_type(self) -> None:
        """EpicMergedEvent must replay + fan out like other lifecycle events."""
        from yukar.events.bus import _LIFECYCLE_TYPES
        from yukar.models.events import EpicMergedEvent

        assert EpicMergedEvent in _LIFECYCLE_TYPES

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
