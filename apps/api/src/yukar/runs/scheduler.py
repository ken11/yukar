"""WorkerScheduler — per-epic concurrency gate for Worker agents.

One instance per Epic Run (created by the orchestrator at run start).

Invariants
----------
- max_parallel_workers: asyncio.Semaphore caps total concurrent workers within
  one Epic Run.  Configured via settings.agent.max_parallel_workers.
- repo_lock: per-repo asyncio.Lock ensures at most one Worker touches a given
  repo at a time.  Workers assigned to different repos run in parallel; workers
  assigned to the same repo are serialised.
- Acquisition order is always semaphore-first then repo_lock.  Because every
  caller follows the same order, circular-wait (deadlock) cannot occur.

Usage
-----
    scheduler = WorkerScheduler(max_parallel_workers=4)

    async def run_task(task: Task) -> None:
        async with scheduler.slot(task.repo or "default"):
            await _run_one_attempt(...)

    async with asyncio.TaskGroup() as tg:
        for task in runnable_tasks:
            tg.create_task(run_task(task))
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager


class WorkerScheduler:
    """Concurrency gate for Worker agents within a single Epic Run.

    Args:
        max_parallel_workers: Maximum number of workers that may run
            concurrently across all repos in this Run.  Must be >= 1.
    """

    def __init__(self, max_parallel_workers: int = 4) -> None:
        if max_parallel_workers < 1:
            raise ValueError(f"max_parallel_workers must be >= 1, got {max_parallel_workers}")
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(max_parallel_workers)
        # repo_name → Lock.  defaultdict creates a Lock lazily per repo.
        self._repo_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @asynccontextmanager
    async def slot(self, repo_name: str) -> AsyncGenerator[None]:
        """Acquire concurrency slot for *repo_name*.

        Acquires the global semaphore first, then the per-repo lock.  Both are
        released in reverse order on exit.  This fixed acquisition order
        prevents deadlocks when multiple coroutines compete for the same pair.

        Usage::

            async with scheduler.slot("my-repo"):
                # At most max_parallel_workers workers run simultaneously.
                # Only one worker runs for "my-repo" at any time.
                await do_work()
        """
        async with self._semaphore, self._repo_locks[repo_name]:
            yield
