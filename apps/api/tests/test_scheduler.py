"""Tests for runs/scheduler.py — WorkerScheduler concurrency gates.

Covers:
- max_parallel_workers upper bound (semaphore).
- Same-repo serialisation (repo lock).
- Different-repo parallelism.
- Slot release allows re-acquisition.
"""

from __future__ import annotations

import asyncio

import pytest


class TestWorkerSchedulerSemaphore:
    """WorkerScheduler respects max_parallel_workers."""

    async def test_semaphore_limits_concurrency(self) -> None:
        """At most max_parallel_workers tasks run concurrently."""
        from yukar.runs.scheduler import WorkerScheduler

        scheduler = WorkerScheduler(max_parallel_workers=2)

        running: list[int] = []
        peak: list[int] = []

        async def _work(idx: int) -> None:
            async with scheduler.slot(f"repo-{idx}"):
                running.append(idx)
                peak.append(len(running))
                await asyncio.sleep(0.05)
                running.remove(idx)

        # Launch 4 tasks — only 2 should run at a time.
        await asyncio.gather(*[_work(i) for i in range(4)])

        assert max(peak) <= 2, f"Peak concurrency {max(peak)} exceeded limit of 2"

    async def test_single_worker_allowed(self) -> None:
        """max_parallel_workers=1 serialises all tasks."""
        from yukar.runs.scheduler import WorkerScheduler

        scheduler = WorkerScheduler(max_parallel_workers=1)
        order: list[int] = []

        async def _work(idx: int) -> None:
            async with scheduler.slot(f"repo-{idx}"):
                order.append(idx)
                await asyncio.sleep(0.01)

        await asyncio.gather(*[_work(i) for i in range(3)])
        assert len(order) == 3

    def test_invalid_max_raises(self) -> None:
        from yukar.runs.scheduler import WorkerScheduler

        with pytest.raises(ValueError, match="max_parallel_workers must be >= 1"):
            WorkerScheduler(max_parallel_workers=0)


class TestWorkerSchedulerRepoLock:
    """Same-repo tasks are serialised; different-repo tasks run in parallel."""

    async def test_same_repo_serialised(self) -> None:
        """Two tasks for the same repo must not overlap."""
        from yukar.runs.scheduler import WorkerScheduler

        scheduler = WorkerScheduler(max_parallel_workers=4)

        inside_count: list[int] = [0]
        overlap_detected: list[bool] = [False]

        async def _work() -> None:
            async with scheduler.slot("shared-repo"):
                inside_count[0] += 1
                if inside_count[0] > 1:
                    overlap_detected[0] = True
                await asyncio.sleep(0.05)
                inside_count[0] -= 1

        await asyncio.gather(_work(), _work())

        assert not overlap_detected[0], "Two tasks ran concurrently on the same repo"

    async def test_different_repos_parallel(self) -> None:
        """Tasks for different repos may run in parallel."""
        from yukar.runs.scheduler import WorkerScheduler

        scheduler = WorkerScheduler(max_parallel_workers=4)

        started_at: dict[str, float] = {}
        finished_at: dict[str, float] = {}

        async def _work(repo: str) -> None:
            async with scheduler.slot(repo):
                started_at[repo] = asyncio.get_event_loop().time()
                await asyncio.sleep(0.05)
                finished_at[repo] = asyncio.get_event_loop().time()

        await asyncio.gather(_work("repo-a"), _work("repo-b"))

        # Both must have started before either finished (overlap proves parallel).
        assert started_at["repo-a"] < finished_at["repo-b"], "Tasks did not overlap"
        assert started_at["repo-b"] < finished_at["repo-a"], "Tasks did not overlap"

    async def test_slot_released_and_reacquired(self) -> None:
        """After releasing a slot it can be acquired again."""
        from yukar.runs.scheduler import WorkerScheduler

        scheduler = WorkerScheduler(max_parallel_workers=1)

        results: list[str] = []

        async def _work(tag: str) -> None:
            async with scheduler.slot("repo"):
                results.append(tag)

        await _work("first")
        await _work("second")

        assert results == ["first", "second"]


class TestWorkerSchedulerObservability:
    """Scheduler observation / event order tests."""

    async def test_worker_started_both_appear_before_either_finishes(self) -> None:
        """For two different-repo tasks, worker_started for both appears before either
        worker_completed, proving real parallel dispatch.

        This mirrors the orchestrator integration test contract.
        """
        from yukar.runs.scheduler import WorkerScheduler

        scheduler = WorkerScheduler(max_parallel_workers=4)

        log: list[str] = []
        gate = asyncio.Event()

        async def _work(repo: str) -> None:
            async with scheduler.slot(repo):
                log.append(f"started:{repo}")
                # Wait until the gate is opened (i.e., both tasks have started).
                await gate.wait()
                log.append(f"finished:{repo}")

        async def _driver() -> None:
            # Give both tasks a moment to start.
            await asyncio.sleep(0.05)
            gate.set()

        await asyncio.gather(
            _work("repo-x"),
            _work("repo-y"),
            _driver(),
        )

        # Both started before either finished.
        idx_start_x = log.index("started:repo-x")
        idx_start_y = log.index("started:repo-y")
        idx_fin_x = log.index("finished:repo-x")
        idx_fin_y = log.index("finished:repo-y")

        assert idx_start_x < idx_fin_y, "repo-x started after repo-y finished"
        assert idx_start_y < idx_fin_x, "repo-y started after repo-x finished"
