"""Run-slot semantics under the lifecycle redesign (review follow-ups).

Covers the concurrency contracts around parked conversation runs:

1. ``is_parked`` is queue-aware: a pending user message means "about to
   wake" — the run counts as executing for guards and cannot be shelved
   (shelving it would silently drop the message with the dying task's
   in-memory queue).
2. The turn slot (max_parallel_epics semaphore) is held only while a turn
   executes: released on park, re-acquired on wake, never over-released —
   parked conversations cannot starve other epics' runs.
3. The shelve/inject handshake: ``shelve_waiting`` refuses a non-parked run;
   ``inject_hitl_message`` refuses a shelving handle (the message routes to
   the continuation path instead of being lost).
4. Router integration: an operation that loses the shelve race to a waking
   run gets a 409 instead of mutating the epic under an executing turn, and
   POST /run on a parked live run shelves it and starts (202, no dead-end).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from yukar.config.settings import LLMSettings
from yukar.models.run import RunState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orchestrator(root: str) -> Any:
    from yukar.agents.orchestrator import EpicOrchestrator

    orch = EpicOrchestrator(
        llm_settings=LLMSettings(provider="fake"),
        git_author_name="Test",
        git_author_email="test@example.com",
    )
    orch._root = root
    orch._project_id = "proj"
    orch._epic_id = "ep"
    orch._run_id = "run-slot"
    orch._state = RunState(run_id="run-slot", status="running")
    orch._pub = lambda _event: None
    return orch


def _register_handle(
    sv: Any, root: str, runner: Any, project_id: str = "proj", epic_id: str = "ep"
) -> asyncio.Task[None]:
    """Register a live handle with *runner*; returns the never-ending task."""
    from yukar.runs.supervisor import _RunHandle

    async def _never() -> None:
        await asyncio.sleep(9999)

    task: asyncio.Task[None] = asyncio.create_task(_never())
    sv._runs[(project_id, epic_id)] = _RunHandle(
        run_id="run-live",
        runner=runner,
        task=task,
        root=root,
        project_id=project_id,
        epic_id=epic_id,
    )
    return task


async def _cleanup_task(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# 1. is_parked is queue-aware
# ---------------------------------------------------------------------------


class TestIsParkedQueueAware:
    async def test_pending_message_means_not_parked(self, tmp_path: Path) -> None:
        orch = _make_orchestrator(str(tmp_path / "ws"))
        orch._awaiting_user = True
        assert orch.is_parked is True

        orch.inject_message("manager", "reply")
        # About to wake: guards must treat this as executing; a shelve here
        # would cancel the task before the queued message is consumed.
        assert orch.is_parked is False

        orch._drain_pending()
        assert orch.is_parked is True

    async def test_not_parked_while_running(self, tmp_path: Path) -> None:
        orch = _make_orchestrator(str(tmp_path / "ws"))
        assert orch.is_parked is False

    async def test_deferred_nonmanager_message_means_not_parked(self, tmp_path: Path) -> None:
        """A non-manager message held in the wait loop's local deferred list
        exists only in this task's memory: is_parked must stay False so a
        shelve at that instant cannot cancel the task and drop it."""
        root = str(tmp_path / "ws")
        orch = _make_orchestrator(root)
        orch._awaiting_user = True

        wait_task = asyncio.create_task(
            orch._wait_for_user_input(root, "proj", "ep", "run-slot", orch._state, orch._pub)
        )
        try:
            # Let the wait loop block on the queue, then send a WORKER message.
            await asyncio.sleep(0.05)
            orch.inject_message("worker-1", "worker-bound text")
            # Wait until the loop has consumed it into its local deferred list.
            deadline = asyncio.get_running_loop().time() + 5.0
            while not orch._pending_messages.empty():
                assert asyncio.get_running_loop().time() < deadline, "message never consumed"
                await asyncio.sleep(0.01)
            # Queue is empty but the message is deferred in-flight: NOT parked.
            assert orch._deferred_inflight == 1
            assert orch.is_parked is False

            # The manager reply wakes the run; the deferred message returns to
            # the queue for the next _drain_pending().
            orch.inject_message("manager", "the awaited reply")
            reply = await asyncio.wait_for(wait_task, timeout=5.0)
            assert reply == "the awaited reply"
            assert orch._deferred_inflight == 0
            assert orch._drain_pending() == [("worker-1", "worker-bound text")]
        finally:
            if not wait_task.done():
                wait_task.cancel()
                with contextlib.suppress(BaseException):
                    await wait_task


# ---------------------------------------------------------------------------
# 2. Turn-slot bookkeeping
# ---------------------------------------------------------------------------


class TestTurnSlot:
    async def test_park_releases_and_wake_reacquires(self, tmp_path: Path) -> None:
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        orch = _make_orchestrator(root)
        slot = asyncio.Semaphore(1)
        orch.set_turn_slot(slot)

        await orch._acquire_turn_slot()
        assert slot._value == 0  # held while "executing"

        await orch._park_awaiting_user()
        assert slot._value == 1  # released while parked
        assert orch.is_parked is True

        # Wake: a queued manager reply re-acquires the slot and persists running.
        orch.inject_message("manager", "go on")
        reply = await orch._wait_for_user_input(
            root, "proj", "ep", "run-slot", orch._state, orch._pub
        )
        assert reply == "go on"
        assert slot._value == 0  # re-acquired for the next turn
        persisted = await state_repo.get_state(root, "proj", "ep")
        assert persisted is not None
        assert persisted.status == "running"

    async def test_release_is_idempotent_no_overrelease(self, tmp_path: Path) -> None:
        orch = _make_orchestrator(str(tmp_path / "ws"))
        slot = asyncio.Semaphore(1)
        orch.set_turn_slot(slot)

        await orch._acquire_turn_slot()
        orch._release_turn_slot()
        orch._release_turn_slot()  # e.g. park already released, then finally
        assert slot._value == 1  # a permit was never minted out of thin air

    async def test_no_slot_installed_is_a_noop(self, tmp_path: Path) -> None:
        orch = _make_orchestrator(str(tmp_path / "ws"))
        await orch._acquire_turn_slot()
        orch._release_turn_slot()  # must not raise

    async def test_cancel_during_slot_reacquire_requeues_consumed_reply(
        self, tmp_path: Path
    ) -> None:
        """A cancellation that lands while the wake path awaits the turn slot
        must put the already-consumed reply back on the queue (best-effort)
        instead of dropping it with the dying task."""
        root = str(tmp_path / "ws")
        orch = _make_orchestrator(root)
        slot = asyncio.Semaphore(1)
        orch.set_turn_slot(slot)
        await slot.acquire()  # slot held elsewhere → re-acquire will block

        orch._awaiting_user = True
        wait_task = asyncio.create_task(
            orch._wait_for_user_input(root, "proj", "ep", "run-slot", orch._state, orch._pub)
        )
        try:
            await asyncio.sleep(0.05)
            orch.inject_message("manager", "precious reply")
            # Wait until the reply is consumed and the task is blocked on the
            # slot re-acquire (_awaiting_user flips False in the same step).
            deadline = asyncio.get_running_loop().time() + 5.0
            while orch._awaiting_user or not orch._pending_messages.empty():
                assert asyncio.get_running_loop().time() < deadline, "wake never reached acquire"
                await asyncio.sleep(0.01)

            wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wait_task
            assert wait_task.cancelled()

            # The consumed reply was returned to the queue, not dropped.
            assert orch._pending_messages.get_nowait() == ("manager", "precious reply")
        finally:
            if not wait_task.done():
                wait_task.cancel()
                with contextlib.suppress(BaseException):
                    await wait_task
            slot.release()


# ---------------------------------------------------------------------------
# 3. Shelve/inject handshake (supervisor level)
# ---------------------------------------------------------------------------


class TestShelveInjectHandshake:
    async def test_shelve_refuses_run_with_pending_message(self, tmp_path: Path) -> None:
        from yukar.runs.supervisor import RunSupervisor

        root = str(tmp_path / "ws")
        sv = RunSupervisor()
        orch = _make_orchestrator(root)
        orch._awaiting_user = True
        orch.inject_message("manager", "reply that must not be lost")
        task = _register_handle(sv, root, orch)
        try:
            assert await sv.shelve_waiting("proj", "ep") is False
            assert not task.cancelled()
            assert sv.is_executing("proj", "ep") is True  # about-to-wake = executing
        finally:
            await _cleanup_task(task)
            sv._runs.pop(("proj", "ep"), None)

    async def test_inject_refused_while_shelving(self, tmp_path: Path) -> None:
        from yukar.runs.supervisor import RunSupervisor

        root = str(tmp_path / "ws")
        sv = RunSupervisor()
        orch = _make_orchestrator(root)
        orch._awaiting_user = True
        task = _register_handle(sv, root, orch)
        try:
            handle = sv._runs[("proj", "ep")]
            handle.shelving = True
            assert sv.inject_hitl_message("proj", "ep", "manager", "hello") is False
            # The message was NOT enqueued into the dying task's queue.
            assert orch._pending_messages.empty()
        finally:
            await _cleanup_task(task)
            sv._runs.pop(("proj", "ep"), None)

    async def test_shelve_succeeds_for_cleanly_parked_run(self, tmp_path: Path) -> None:
        from yukar.runs.supervisor import RunSupervisor

        root = str(tmp_path / "ws")
        sv = RunSupervisor()
        orch = _make_orchestrator(root)
        orch._awaiting_user = True
        task = _register_handle(sv, root, orch)
        assert await sv.shelve_waiting("proj", "ep") is True
        assert task.cancelled() or task.done()
        assert ("proj", "ep") not in sv._runs


# ---------------------------------------------------------------------------
# 4. Router integration
# ---------------------------------------------------------------------------


class _WakesDuringShelveRunner:
    """is_parked=True at the guard check, False at the shelve re-check —
    simulates a reply landing between the two (the shelve race window)."""

    def __init__(self) -> None:
        self._reads = 0

    @property
    def is_parked(self) -> bool:
        self._reads += 1
        return self._reads == 1


async def _seed_project_epic(client: Any) -> None:
    await client.post("/api/projects", json={"id": "proj", "name": "Proj", "repos": []})
    await client.post("/api/projects/proj/epics", json={"title": "Epic"})


class TestRouterShelveRace:
    async def test_patch_completed_409_when_run_wakes_mid_shelve(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        from yukar.runs.supervisor import get_supervisor

        await _seed_project_epic(app_client)
        sv = get_supervisor()
        task = _register_handle(sv, str(tmp_workspace), _WakesDuringShelveRunner(), "proj", "EP-1")
        try:
            resp = await app_client.patch(
                "/api/projects/proj/epics/EP-1", json={"status": "completed"}
            )
            assert resp.status_code == 409
            assert "woke" in resp.json()["detail"].lower()
            # The epic was NOT completed under the waking run.
            r2 = await app_client.get("/api/projects/proj/epics/EP-1")
            assert r2.json()["status"] == "open"
        finally:
            await _cleanup_task(task)
            sv._runs.pop(("proj", "EP-1"), None)

    async def test_start_run_shelves_parked_live_run(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """POST /run on a live-but-parked run is a restart, not a 409 dead-end."""
        from yukar.runs.supervisor import get_supervisor

        await _seed_project_epic(app_client)
        sv = get_supervisor()
        parked = MagicMock(is_parked=True)
        task = _register_handle(sv, str(tmp_workspace), parked, "proj", "EP-1")
        try:
            resp = await app_client.post("/api/projects/proj/epics/EP-1/run")
            assert resp.status_code == 202, resp.text
            # The parked live task was shelved to make room for the restart.
            assert task.cancelled() or task.done()
        finally:
            await _cleanup_task(task)
            # Stop whatever real run the 202 started so the test exits cleanly.
            with contextlib.suppress(Exception):
                await sv.stop("proj", "EP-1")
            sv._runs.pop(("proj", "EP-1"), None)

class TestPutTasksRunGuard:
    """PUT /tasks is a plan mutation: it must not race an in-flight run."""

    _TASKS_BODY = {
        "tasks": [
            {
                "id": "T1",
                "title": "Edited task",
                "status": "todo",
                "depends_on": [],
                "contract": "c",
            }
        ],
        "progress": {"done": 0, "total": 1},
    }

    async def test_put_tasks_409_while_executing(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        from yukar.runs.supervisor import get_supervisor

        await _seed_project_epic(app_client)
        sv = get_supervisor()
        executing = MagicMock(is_parked=False)
        task = _register_handle(sv, str(tmp_workspace), executing, "proj", "EP-1")
        try:
            resp = await app_client.put(
                "/api/projects/proj/epics/EP-1/tasks", json=self._TASKS_BODY
            )
            assert resp.status_code == 409, resp.text
            # The plan was NOT rewritten under the executing run.
            r2 = await app_client.get("/api/projects/proj/epics/EP-1/tasks")
            assert r2.json()["tasks"] == []
        finally:
            await _cleanup_task(task)
            sv._runs.pop(("proj", "EP-1"), None)

    async def test_put_tasks_409_when_run_wakes_mid_shelve(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        from yukar.runs.supervisor import get_supervisor

        await _seed_project_epic(app_client)
        sv = get_supervisor()
        task = _register_handle(sv, str(tmp_workspace), _WakesDuringShelveRunner(), "proj", "EP-1")
        try:
            resp = await app_client.put(
                "/api/projects/proj/epics/EP-1/tasks", json=self._TASKS_BODY
            )
            assert resp.status_code == 409, resp.text
            assert "woke" in resp.json()["detail"].lower()
        finally:
            await _cleanup_task(task)
            sv._runs.pop(("proj", "EP-1"), None)

    async def test_put_tasks_shelves_parked_live_run(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """A live run merely parked in ``waiting`` yields the slot: the plan
        edit succeeds and the parked task is shelved."""
        from yukar.runs.supervisor import get_supervisor

        await _seed_project_epic(app_client)
        sv = get_supervisor()
        parked = MagicMock(is_parked=True)
        task = _register_handle(sv, str(tmp_workspace), parked, "proj", "EP-1")
        try:
            resp = await app_client.put(
                "/api/projects/proj/epics/EP-1/tasks", json=self._TASKS_BODY
            )
            assert resp.status_code == 200, resp.text
            assert task.cancelled() or task.done()
            r2 = await app_client.get("/api/projects/proj/epics/EP-1/tasks")
            assert [t["id"] for t in r2.json()["tasks"]] == ["T1"]
        finally:
            await _cleanup_task(task)
            sv._runs.pop(("proj", "EP-1"), None)
