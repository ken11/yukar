"""Regression tests for run-lifecycle hardening findings.

Covers:
1. Registry cleanup — a run handle is removed from ``RunSupervisor._runs`` when
   its task reaches a terminal state (completed / failed / cancelled), so the
   registry does not leak stale handles and inject/SSE lookups never resolve to
   a finished run.
2. inject-during-resolve — ``start_or_inject`` does NOT silently drop a manager
   message when the active run is a resolve/arbiter run (no ``inject_message``);
   it surfaces a ``RuntimeError`` instead.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Controllable fake runners
# ---------------------------------------------------------------------------


class _ControllableRunner:
    """A runner whose start() blocks until released, then optionally raises.

    Lets a test register a live run, observe ``_runs``, then drive the task to a
    terminal state deterministically.
    """

    def __init__(self, *, raise_on_finish: bool = False) -> None:
        self._release = asyncio.Event()
        self._raise = raise_on_finish
        self.inject_calls: list[tuple[str, str]] = []

    async def start(self, root: str, project_id: str, epic_id: str, run_id: str) -> None:
        await self._release.wait()
        if self._raise:
            raise RuntimeError("simulated run failure")

    def release(self) -> None:
        self._release.set()

    async def pause(self) -> None: ...

    async def resume(self) -> None: ...

    async def stop(self) -> None:
        self._release.set()

    def inject_message(self, thread_id: str, text: str) -> None:
        self.inject_calls.append((thread_id, text))


class _NoInjectRunner:
    """A runner WITHOUT inject_message — mimics ResolveRunner / ArbiterRunner."""

    def __init__(self) -> None:
        self._release = asyncio.Event()

    async def start(self, root: str, project_id: str, epic_id: str, run_id: str) -> None:
        await self._release.wait()

    def release(self) -> None:
        self._release.set()

    async def pause(self) -> None: ...

    async def resume(self) -> None: ...

    async def stop(self) -> None:
        self._release.set()


def _register(sup: Any, key: tuple[str, str], runner: Any) -> asyncio.Task[None]:
    """Register *runner* under *key* via the supervisor's own cleanup path."""
    from yukar.runs.supervisor import _RunHandle

    async def _drive() -> None:
        await runner.start("/tmp", key[0], key[1], "run-x")

    task: asyncio.Task[None] = asyncio.create_task(_drive())
    handle = _RunHandle(
        run_id="run-x",
        runner=runner,
        task=task,
        root="/tmp",
        project_id=key[0],
        epic_id=key[1],
    )
    sup._register(key, handle)
    return task


# ---------------------------------------------------------------------------
# 1. Registry cleanup on terminal state
# ---------------------------------------------------------------------------


class TestTerminalRunRemovedFromRegistry:
    async def test_completed_run_handle_removed(self) -> None:
        from yukar.runs.supervisor import RunSupervisor

        sup = RunSupervisor()
        key = ("proj", "EP-1")
        runner = _ControllableRunner()
        task = _register(sup, key, runner)

        # While running, the handle is present.
        assert key in sup._runs
        assert sup.is_running(*key)

        # Drive to completion; the done-callback must evict the handle.
        runner.release()
        await task

        assert key not in sup._runs
        assert not sup.is_running(*key)

    async def test_failed_run_handle_removed(self) -> None:
        from yukar.runs.supervisor import RunSupervisor

        sup = RunSupervisor()
        key = ("proj", "EP-2")
        runner = _ControllableRunner(raise_on_finish=True)
        task = _register(sup, key, runner)

        assert key in sup._runs
        runner.release()
        with pytest.raises(RuntimeError):
            await task

        assert key not in sup._runs

    async def test_cancelled_run_handle_removed(self) -> None:
        from yukar.runs.supervisor import RunSupervisor

        sup = RunSupervisor()
        key = ("proj", "EP-3")
        runner = _ControllableRunner()
        task = _register(sup, key, runner)

        assert key in sup._runs
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert key not in sup._runs

    async def test_inject_to_finished_run_does_not_deliver(self) -> None:
        """After a run completes (handle removed), inject must not deliver."""
        from yukar.runs.supervisor import RunSupervisor

        sup = RunSupervisor()
        key = ("proj", "EP-4")
        runner = _ControllableRunner()
        task = _register(sup, key, runner)

        runner.release()
        await task

        # The handle is gone — inject reports "not delivered" and never touches
        # the dead runner.
        delivered = sup.inject_hitl_message("proj", "EP-4", "manager", "late message")
        assert delivered is False
        assert runner.inject_calls == []

    async def test_replacement_handle_not_evicted_by_old_callback(self) -> None:
        """When a finished run is replaced by a fresh one under the same key, the
        old task's done-callback must NOT evict the live successor."""
        from yukar.runs.supervisor import RunSupervisor, _RunHandle

        sup = RunSupervisor()
        key = ("proj", "EP-5")

        old_runner = _ControllableRunner()
        old_task = _register(sup, key, old_runner)

        # Register a replacement handle BEFORE the old task's callback fires.
        new_runner = _ControllableRunner()

        async def _drive_new() -> None:
            await new_runner.start("/tmp", key[0], key[1], "run-new")

        new_task: asyncio.Task[None] = asyncio.create_task(_drive_new())
        new_handle = _RunHandle(
            run_id="run-new",
            runner=new_runner,
            task=new_task,
            root="/tmp",
            project_id=key[0],
            epic_id=key[1],
        )
        sup._register(key, new_handle)

        # Now finish the OLD run.  Its callback must see that _runs[key] is the
        # NEW handle and leave it alone.
        old_runner.release()
        await old_task

        assert sup._runs.get(key) is new_handle

        # Cleanup.
        new_runner.release()
        await new_task


# ---------------------------------------------------------------------------
# 2. inject during resolve/arbiter must not be silently dropped
# ---------------------------------------------------------------------------


class TestInjectDuringResolveNotDropped:
    async def test_start_or_inject_raises_when_active_run_cannot_inject(self) -> None:
        """A resolve/arbiter run (no inject_message) is active — start_or_inject
        must raise RuntimeError rather than silently dropping the message."""
        from yukar.runs.supervisor import RunSupervisor

        sup = RunSupervisor()
        key = ("proj", "EP-1")
        runner = _NoInjectRunner()
        task = _register(sup, key, runner)

        assert sup.is_running(*key)
        assert not sup.can_inject(*key)

        with pytest.raises(RuntimeError) as exc_info:
            await sup.start_or_inject("/tmp", "proj", "EP-1", "manager", "please handle this")
        # The error must clearly explain why (resolve/merge in progress).
        msg = str(exc_info.value).lower()
        assert "merge" in msg or "resolution" in msg or "cannot receive" in msg

        # Cleanup.
        runner.release()
        await task

    async def test_start_or_inject_delivers_when_run_can_inject(self) -> None:
        """When the active run is an orchestrator (has inject_message), the
        message is delivered and NOT treated as undeliverable."""
        from yukar.runs.supervisor import RunSupervisor

        sup = RunSupervisor()
        key = ("proj", "EP-2")
        runner = _ControllableRunner()
        task = _register(sup, key, runner)

        assert sup.can_inject(*key)
        result = await sup.start_or_inject("/tmp", "proj", "EP-2", "manager", "hello there")
        assert result is True
        assert runner.inject_calls == [("manager", "hello there")]

        # Cleanup.
        runner.release()
        await task

    async def test_can_inject_false_for_finished_run(self) -> None:
        from yukar.runs.supervisor import RunSupervisor

        sup = RunSupervisor()
        key = ("proj", "EP-3")
        runner = _ControllableRunner()
        task = _register(sup, key, runner)
        runner.release()
        await task

        assert sup.can_inject(*key) is False
