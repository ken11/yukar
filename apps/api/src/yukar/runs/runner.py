"""RunnerProtocol + DummyRunner.

DummyRunner simulates an agent run by publishing a scripted sequence of events
to the event bus at 0.5–1 s intervals, and updating state.yaml accordingly.

DummyRunner is a permanent no-LLM/test fallback used by the supervisor when
no real orchestrator is configured (e.g. in integration tests).  It satisfies
RunnerProtocol and can be swapped in via dependency injection.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import UTC, datetime
from typing import Protocol

from yukar.events import bus as event_bus
from yukar.models.events import (
    EvalResultEvent,
    RunCompletedEvent,
    RunStartedEvent,
    TaskUpdateEvent,
    TokenEvent,
    WorkerCompletedEvent,
    WorkerStartedEvent,
)
from yukar.models.run import ActiveWorker, RunState
from yukar.storage import state_repo
from yukar.storage.project_repo import list_repos


class RunnerProtocol(Protocol):
    """Interface for epic run executors."""

    async def start(
        self,
        root: str,
        project_id: str,
        epic_id: str,
        run_id: str,
    ) -> None:
        """Start the run and drive it to completion (or until cancelled)."""
        ...

    async def pause(self) -> None: ...

    async def resume(self) -> None: ...

    async def stop(self) -> None: ...


class DummyRunner:
    """Simulated runner for M1 SSE smoke-testing.

    Publishes a canned sequence of RunEvents and updates state.yaml.
    """

    def __init__(self) -> None:
        self._paused = asyncio.Event()
        self._paused.set()  # Not paused initially
        self._stopped = False

    async def start(
        self,
        root: str,
        project_id: str,
        epic_id: str,
        run_id: str,
    ) -> None:
        def pub(event: object) -> None:
            event_bus.publish(project_id, epic_id, event)

        async def wait() -> None:
            await asyncio.sleep(random.uniform(0.5, 1.0))
            await self._paused.wait()

        # Resolve the first registered repo name for this project.
        # Falls back to a sentinel so events are never misleadingly labeled.
        repos = await list_repos(root, project_id)
        first_repo_name: str = repos[0].name if repos else "unknown-repo"

        # Update state to running.
        # NOTE: epic.yaml.status transitions (in_progress/completed/failed) are
        # managed by supervisor.py, not here.  runner only owns state.yaml.
        state = RunState(
            run_id=run_id,
            status="running",
            started_at=datetime.now(UTC),
        )
        await state_repo.save_state(root, project_id, epic_id, state)

        try:
            pub(
                RunStartedEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                )
            )
            await wait()

            if self._stopped:
                return

            # Simulate task decomposition
            for task_id in ("T1", "T2"):
                pub(
                    TaskUpdateEvent(
                        project_id=project_id,
                        epic_id=epic_id,
                        run_id=run_id,
                        task_id=task_id,
                        status="todo",
                        title=f"Task {task_id}",
                    )
                )

            await wait()

            if self._stopped:
                return

            # Simulate Worker T1
            worker_id = f"worker-{uuid.uuid4().hex[:8]}"
            pub(
                WorkerStartedEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                    worker_id=worker_id,
                    task_id="T1",
                    repo=first_repo_name,
                )
            )
            state.active_workers = [
                ActiveWorker(worker_id=worker_id, task_id="T1", repo=first_repo_name)
            ]
            await state_repo.save_state(root, project_id, epic_id, state)

            for token in ("Hello", " from", " DummyRunner", "!"):
                await wait()
                if self._stopped:
                    return
                pub(
                    TokenEvent(
                        project_id=project_id,
                        epic_id=epic_id,
                        run_id=run_id,
                        thread_id=worker_id,
                        delta=token,
                    )
                )

            pub(
                WorkerCompletedEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                    worker_id=worker_id,
                    task_id="T1",
                    repo=first_repo_name,
                )
            )
            pub(
                TaskUpdateEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                    task_id="T1",
                    status="done",
                    title="Task T1",
                )
            )
            state.active_workers = []
            await state_repo.save_state(root, project_id, epic_id, state)

            await wait()

            if self._stopped:
                return

            # Simulate Evaluator.
            # NOTE: DummyRunner is the M1 fake for SSE smoke-testing.  It
            # intentionally omits the evaluator-tree events (EvaluatorStartedEvent
            # with a real eval_id) that the real EpicOrchestrator emits.
            # eval_id defaults to "" here; the frontend reducer treats empty
            # eval_id as a no-op, so this is harmless.  A full parity
            # implementation would generate a synthetic eval_id and emit
            # EvaluatorStartedEvent before EvalResultEvent — left for the real
            # orchestrator path which already does this correctly.
            pub(
                EvalResultEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                    worker_id=worker_id,
                    accepted=True,
                    feedback="Looks good!",
                )
            )

            await wait()

            if self._stopped:
                return

            # Worker T2
            worker_id2 = f"worker-{uuid.uuid4().hex[:8]}"
            pub(
                WorkerStartedEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                    worker_id=worker_id2,
                    task_id="T2",
                    repo=first_repo_name,
                )
            )
            pub(
                TaskUpdateEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                    task_id="T2",
                    status="in_progress",
                    title="Task T2",
                )
            )
            await wait()
            if self._stopped:
                return
            pub(
                WorkerCompletedEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                    worker_id=worker_id2,
                    task_id="T2",
                    repo=first_repo_name,
                )
            )
            pub(
                TaskUpdateEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                    task_id="T2",
                    status="done",
                    title="Task T2",
                )
            )

            await wait()

            # Complete — update state.yaml only.
            # epic.yaml.status → completed is handled by supervisor._run_with_semaphore.
            state.status = "completed"
            state.active_workers = []
            state.last_event_at = datetime.now(UTC)
            await state_repo.save_state(root, project_id, epic_id, state)

            pub(
                RunCompletedEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                )
            )
        finally:
            # Publish sentinel so SSE streams close naturally (fix #8).
            # This fires on normal completion, stop, and cancellation.
            # Multiple subscribers all receive the sentinel via fan-out.
            event_bus.publish(project_id, epic_id, None)

    async def pause(self) -> None:
        self._paused.clear()

    async def resume(self) -> None:
        self._paused.set()

    async def stop(self) -> None:
        self._stopped = True
        self._paused.set()  # Unblock any wait
