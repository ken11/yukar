"""Finding verification: watcher-task-gc

finding[watcher-task-gc] (fixed in G1-G8 batch): indexer/watcher.py now holds a
strong-reference set _reindex_tasks for fire-and-forget reindex tasks, and tasks
remove themselves via add_done_callback when they finish.

Review fix #5: stop() must cancel in-flight reindex tasks.
Previously stop() only cancelled the watcher background task (_task) but left any
running reindex tasks alive after shutdown.  The fix cancels all tasks in
_reindex_tasks and awaits their completion before returning.

Test strategy
----------
1. Structural test: RepoWatcher has the _reindex_tasks attribute (set).
2. GC demonstration test: Task is retained in _reindex_tasks.
3. Characterization test: Task becomes active immediately after _fire_reindex.
4. Removed from set by done_callback: _reindex_tasks is empty after completion.
5. stop() in-flight cancel (review fix #5): in-flight tasks are cancelled after stop().
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import weakref
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_watcher(debounce: float = 0.0) -> Any:
    """Return a RepoWatcher with a mock IndexerService (no real FAISS needed)."""
    from yukar.indexer.watcher import RepoWatcher

    service = MagicMock()
    service.reindex_repo = AsyncMock(return_value=42)
    return RepoWatcher(service=service, debounce=debounce)


def _make_ignore_rules(path: Any) -> Any:
    """Return a minimal IgnoreRules for use in tests (no real gitignore files needed)."""
    from pathlib import Path

    from yukar.sandbox.ignore import IgnoreRules

    return IgnoreRules(
        repo_root=Path(path) if not isinstance(path, Path) else path,
        global_spec=None,
        root_spec=None,
        nested={},
    )


def _make_watched_key() -> tuple[str, str]:
    return ("proj-test", "repo-test")


# ---------------------------------------------------------------------------
# Test 1: Structural test — RepoWatcher must have strong-reference set _reindex_tasks
# ---------------------------------------------------------------------------


async def test_watcher_has_strong_ref_set_for_reindex_tasks() -> None:
    """Expect a RepoWatcher instance to have the _reindex_tasks attribute (set).

    Fixed — regression guard: _reindex_tasks now exists and holds strong references to
    fire-and-forget tasks so they are not garbage-collected before completion.
    """
    watcher = _make_watcher()
    # Verify the strong-reference set exists and is a set type
    assert hasattr(watcher, "_reindex_tasks"), (
        "RepoWatcher has no _reindex_tasks attribute. "
        "A strong-reference set for Tasks returned by create_task must be added."
    )
    assert isinstance(watcher._reindex_tasks, set), (
        "_reindex_tasks must be set[asyncio.Task]."
    )


# ---------------------------------------------------------------------------
# Test 2: GC demonstration test — Task must not be GC'd after dropping external references
# ---------------------------------------------------------------------------


async def test_reindex_task_survives_gc_after_fire() -> None:
    """Task created by _fire_reindex must still be alive after gc.collect().

    Fixed — regression guard: _fire_reindex now stores the Task in _reindex_tasks, providing
    an explicit strong reference so GC cannot collect the Task prematurely. asyncio internally
    keeps pending tasks in its own structures too, but the intentional set makes the contract
    explicit and observable.

    GC collection in CPython happens when refcount drops to 0; in the test environment
    asyncio's _ready / _scheduled hold internal references so collection is not immediate.
    The goal is to confirm the "intentional strong reference" via _reindex_tasks is present.

    Verification steps:
      1. Call _fire_reindex
      2. Track pending tasks other than asyncio.current_task() with weakrefs
      3. Run gc.collect() for all 3 generations
      4. Confirm the weakrefs are still alive (referents are not None)
         → without a strong-reference set this assertion may fail
    """
    from pathlib import Path

    from yukar.indexer.watcher import RepoWatcher, _WatchedRepo

    service = MagicMock()

    # Use a long sleep to keep _do_reindex "in flight" (won't complete during test)
    async def _slow_reindex(*args: Any, **kwargs: Any) -> int:
        await asyncio.sleep(60)  # does not complete during the test
        return 0

    service.reindex_repo = _slow_reindex

    watcher = RepoWatcher(service=service, debounce=0.0)

    # Inject _WatchedRepo directly (watchfiles not needed)
    key = ("proj", "repo")
    watched = _WatchedRepo(
        project_id="proj",
        repo_name="repo",
        repo_path=Path("/tmp"),
        ignore_rules=_make_ignore_rules(Path("/tmp")),
    )
    watcher._repos[key] = watched

    # Call _fire_reindex to create a Task (return value is discarded inside watcher)
    watcher._fire_reindex(key)

    # Collect the created Task from asyncio and attach weakrefs
    current = asyncio.current_task()
    all_tasks = asyncio.all_tasks()
    reindex_tasks = [
        t for t in all_tasks if t is not current and "reindex" in (t.get_name() or "")
    ]

    assert len(reindex_tasks) >= 1, "reindex Task was not created"

    task_refs = [weakref.ref(t) for t in reindex_tasks]

    # Release external strong references (the reindex_tasks list)
    del reindex_tasks

    # Run full GC (all 3 generations)
    gc.collect(0)
    gc.collect(1)
    gc.collect(2)

    # Without _reindex_tasks or similar strong-reference set, the guarantee that
    # weakrefs remain alive depends only on asyncio loop internals.
    # After the fix, watcher._reindex_tasks holds the Task, so we can check explicitly.
    assert hasattr(watcher, "_reindex_tasks"), (
        "watcher._reindex_tasks does not exist — no intentional strong-reference set for Tasks."
    )
    reindex_tasks_set = cast(set[asyncio.Task[Any]], watcher._reindex_tasks)  # type: ignore[attr-defined]
    assert any(
        t in reindex_tasks_set for ref in task_refs if (t := ref()) is not None  # type: ignore[assignment]
    ), "Created reindex Task is not in _reindex_tasks"

    # Cleanup: cancel the long sleep tasks
    for ref in task_refs:
        t = ref()
        if t is not None and not t.done():
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t


# ---------------------------------------------------------------------------
# Test 3: Characterization test — Task must be active immediately after _fire_reindex (pass)
# ---------------------------------------------------------------------------


async def test_fire_reindex_creates_active_task() -> None:
    """Verify that at least one reindex Task is in asyncio.all_tasks()
    immediately after _fire_reindex (characterization test that currently PASSes).

    Note: this only confirms "create_task produced a Task" and does not
    guarantee GC safety. GC safety is covered by tests 1 and 2.
    """
    from pathlib import Path

    from yukar.indexer.watcher import RepoWatcher, _WatchedRepo

    service = MagicMock()
    reindex_started = asyncio.Event()

    async def _mock_reindex(*args: Any, **kwargs: Any) -> int:
        reindex_started.set()
        await asyncio.sleep(60)  # does not complete during the test
        return 0

    service.reindex_repo = _mock_reindex

    watcher = RepoWatcher(service=service, debounce=0.0)
    key = ("proj", "repo")
    watched = _WatchedRepo(
        project_id="proj",
        repo_name="repo",
        repo_path=Path("/tmp"),
        ignore_rules=_make_ignore_rules(Path("/tmp")),
    )
    watcher._repos[key] = watched

    before_tasks = set(asyncio.all_tasks())

    # Call _fire_reindex
    watcher._fire_reindex(key)

    # Yield one step to allow the Task to start
    await asyncio.sleep(0)

    after_tasks = set(asyncio.all_tasks())
    new_tasks = after_tasks - before_tasks

    reindex_new = [t for t in new_tasks if "reindex" in (t.get_name() or "")]
    assert len(reindex_new) >= 1, (
        "_fire_reindex did not call create_task, or Task name does not contain 'reindex'."
    )

    # Cleanup
    for t in reindex_new:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t


# ---------------------------------------------------------------------------
# Test 4: Removed from set by done_callback (behavior check — fixed, regression guard)
# ---------------------------------------------------------------------------


async def test_reindex_task_removed_from_set_on_done() -> None:
    """When a reindex Task completes, it must be removed from _reindex_tasks.

    This confirms the set does not grow unboundedly (self-cleanup via done_callback).
    Only PASSes after the fix.
    """
    from pathlib import Path

    from yukar.indexer.watcher import RepoWatcher, _WatchedRepo

    service = MagicMock()
    service.reindex_repo = AsyncMock(return_value=5)

    watcher = RepoWatcher(service=service, debounce=0.0)
    key = ("proj", "repo")
    watched = _WatchedRepo(
        project_id="proj",
        repo_name="repo",
        repo_path=Path("/tmp"),
        ignore_rules=_make_ignore_rules(Path("/tmp")),
    )
    watcher._repos[key] = watched

    # Execute _fire_reindex
    watcher._fire_reindex(key)

    # Wait for Task to complete
    await asyncio.sleep(0)  # start the Task
    await asyncio.sleep(0)  # consume the internal await of _do_reindex

    # Verify _reindex_tasks is empty after completion
    assert hasattr(watcher, "_reindex_tasks"), "_reindex_tasks attribute is missing"
    # Give AsyncMock a chance to complete
    tasks_set = cast(set[asyncio.Task[Any]], watcher._reindex_tasks)  # type: ignore[attr-defined]
    for _ in range(10):
        await asyncio.sleep(0)
        if not tasks_set:
            break

    assert len(tasks_set) == 0, (
        f"Entries remain in _reindex_tasks after Task completion: {tasks_set}"
    )


# ---------------------------------------------------------------------------
# Test 5: stop() cancels in-flight reindex tasks (review fix #5)
# ---------------------------------------------------------------------------


async def test_stop_cancels_inflight_reindex_tasks() -> None:
    """When stop() is called, in-flight tasks in _reindex_tasks must be cancelled.

    Before the fix, stop() only cancelled the watcher's main task (_task),
    leaving reindex tasks alive. After the fix, stop() cancels all tasks in
    _reindex_tasks and collects them with await asyncio.gather.
    """
    from pathlib import Path

    from yukar.indexer.watcher import RepoWatcher, _WatchedRepo

    service = MagicMock()
    task_started = asyncio.Event()
    task_cancelled = asyncio.Event()

    async def _slow_reindex(*args: Any, **kwargs: Any) -> int:
        task_started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            task_cancelled.set()
            raise
        return 0

    service.reindex_repo = _slow_reindex

    watcher = RepoWatcher(service=service, debounce=0.0)
    key = ("proj", "repo")
    watched = _WatchedRepo(
        project_id="proj",
        repo_name="repo",
        repo_path=Path("/tmp"),
        ignore_rules=_make_ignore_rules(Path("/tmp")),
    )
    watcher._repos[key] = watched

    # Call _fire_reindex directly to create an in-flight task
    watcher._fire_reindex(key)

    # Wait until the task starts
    await asyncio.wait_for(task_started.wait(), timeout=2.0)

    # Confirm in-flight task is in _reindex_tasks
    assert len(watcher._reindex_tasks) == 1, (
        "_reindex_tasks should have 1 task after _fire_reindex"
    )

    # Call stop()
    await asyncio.wait_for(watcher.stop(), timeout=2.0)

    # Confirm the task received CancelledError
    assert task_cancelled.is_set(), (
        "In-flight reindex task did not receive CancelledError after stop(). "
        "stop() should cancel all tasks in _reindex_tasks."
    )

    # Confirm _reindex_tasks is empty (discarded by done_callback)
    assert len(watcher._reindex_tasks) == 0, (
        "Tasks remain in _reindex_tasks after stop()."
    )
