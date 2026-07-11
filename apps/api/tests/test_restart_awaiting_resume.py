"""End-to-end: a run parked in ``waiting`` survives a server restart and
resumes when the user replies (lifecycle redesign P3).

This is the empirical proof for the recovery.py contract (a waiting run is
preserved as-is) + the continuation-resume path.  It drives a real
orchestrator to the park (the Manager presents its plan in the message body
and ends its turn), simulates a crash (graceful-shutdown cancel of the live
task), runs ``recover_interrupted_runs`` (=restart), then starts a
continuation orchestrator with the user's reply as the seed and asserts the
Manager picks up the session, dispatches, and parks again — without
crashing on session restore or losing the conversation.

Mechanics only (FakeModel): the model's semantic understanding of the prior
question is out of scope; what matters is that restore + continuation does
not break, strand the run, or park before doing the work.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests._helpers import make_git_repo as _make_bare_repo
from tests._helpers import wait_for_run_status

from .test_ask_user_gate import _bootstrap


@pytest.mark.asyncio
async def test_waiting_survives_restart_and_resumes_on_reply(tmp_path: Path) -> None:
    from yukar.agents.orchestrator import EpicOrchestrator
    from yukar.config.settings import LLMSettings
    from yukar.events import bus as event_bus
    from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
    from yukar.models.events import (
        RunCompletedEvent,
        UserInputRequestedEvent,
        WorkerStartedEvent,
    )
    from yukar.runs.recovery import recover_interrupted_runs
    from yukar.storage import state_repo

    git_repo = _make_bare_repo(tmp_path, "myrepo")
    root = str(tmp_path / "ws")
    project_id = "proj"
    epic_id = "EP-1"
    await _bootstrap(root, project_id, epic_id, git_repo)

    # --- Phase 1: drive a fresh run to the park (persist the session) ---
    # The plan/question is plain message text; ending the turn parks the run.
    plan_script = [
        ToolUseTurn(
            tool_name="task_update",
            tool_input={
                "task_id": "T1",
                "title": "Write hello.py",
                "status": "todo",
                "repo": git_repo.name,
            },
        ),
        TextTurn("Plan: T1=Write hello.py. Proceed?"),
    ]
    # Continuation manager (after the reply): dispatch then report.
    resume_script = [
        ToolUseTurn(
            tool_name="dispatch",
            tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
        ),
        TextTurn("Done!"),
    ]
    worker_script = [
        ToolUseTurn(
            tool_name="fs_write",
            tool_input={"path": "hello.py", "content": "print('hello')\n"},
        ),
        TextTurn("Done."),
    ]
    evaluator_script = [
        ToolUseTurn(tool_name="submit_verdict", tool_input={"accepted": True, "feedback": ""}),
        TextTurn("Accepted."),
    ]

    def make_factory(manager_script: list[Any]) -> Any:
        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            r = role or "worker"
            if r == "manager":
                return FakeModel(script=list(manager_script))
            if r == "worker":
                return FakeModel(script=list(worker_script))
            return FakeModel(script=list(evaluator_script))

        return fake_create_model

    events: list[Any] = []
    awaiting = asyncio.Event()

    async def _collect() -> None:
        async for ev in event_bus.event_stream(project_id, epic_id):
            events.append(ev)
            if isinstance(ev, UserInputRequestedEvent):
                awaiting.set()

    collector = asyncio.create_task(_collect())
    await asyncio.sleep(0)

    llm = LLMSettings(provider="fake")
    orch1 = EpicOrchestrator(
        llm_settings=llm, git_author_name="yukar", git_author_email="yukar@localhost"
    )

    with patch(
        "yukar.agents.orchestrator.create_model", side_effect=make_factory(plan_script)
    ):
        run1 = asyncio.create_task(orch1.start(root, project_id, epic_id, "run-1"))
        await asyncio.wait_for(awaiting.wait(), timeout=10.0)
        await wait_for_run_status(root, project_id, epic_id, "waiting", timeout=10.0)

        # --- Simulate a graceful server shutdown ---
        # A `yukar serve` stop (SIGTERM/Ctrl-C) cancels the waiting task WITHOUT
        # an explicit supervisor.stop() — i.e. self._stopped stays False.  The
        # orchestrator's CancelledError handler must NOT rewrite state.yaml in
        # that case (the historical bug: it used to clobber the parked state at
        # shutdown).  We do NOT force-write the state back here on purpose: the
        # assertion below proves shutdown preserves it.
        run1.cancel()
        await asyncio.gather(run1, return_exceptions=True)

    # The shutdown cancel must leave the waiting snapshot intact on disk.
    state = await state_repo.get_state(root, project_id, epic_id)
    assert state is not None
    assert state.status == "waiting", (
        f"shutdown must NOT clobber the parked waiting state, got {state.status!r}"
    )

    # --- Phase 2: restart recovery must PRESERVE waiting ---
    await recover_interrupted_runs(root)
    state = await state_repo.get_state(root, project_id, epic_id)
    assert state is not None, "state.yaml should still exist after recovery"
    assert state.status == "waiting", (
        f"waiting must survive restart untouched, got {state.status!r}"
    )

    # --- Phase 3: user approves the plan and replies -> continuation resumes ---
    # Plan approval is an explicit user operation recorded on disk (what
    # POST /plan/approval does); the reply alone would leave the continuation's
    # dispatch host-rejected.  Record it before seeding the reply.
    from datetime import UTC, datetime

    from yukar.models.task import PlanApproval, compute_plan_hash
    from yukar.storage import plan_approval_repo, tasks_repo

    tasks_file = await tasks_repo.get_tasks(root, project_id, epic_id)
    await plan_approval_repo.save_plan_approval(
        root,
        project_id,
        epic_id,
        PlanApproval(
            tasks_hash=compute_plan_hash(tasks_file.tasks),
            approved_at=datetime.now(UTC),
        ),
    )

    # orch1's teardown published a None sentinel that closed the first
    # subscriber, so subscribe a fresh collector for the continuation run.
    collector.cancel()
    await asyncio.gather(collector, return_exceptions=True)

    events2: list[Any] = []

    async def _collect2() -> None:
        async for ev in event_bus.event_stream(project_id, epic_id):
            events2.append(ev)

    collector2 = asyncio.create_task(_collect2())
    await asyncio.sleep(0)

    orch2 = EpicOrchestrator(
        llm_settings=llm,
        git_author_name="yukar",
        git_author_email="yukar@localhost",
        seed_prompt="Looks good, proceed!",
        is_continuation=True,
    )
    with patch(
        "yukar.agents.orchestrator.create_model", side_effect=make_factory(resume_script)
    ):
        run2 = asyncio.create_task(orch2.start(root, project_id, epic_id, "run-2"))
        # The continuation dispatches, reports, and parks (a conversation run
        # never completes — the park is its resting point).  Wait for run-2's
        # OWN park: the stale waiting file from run-1 is still on disk when
        # the continuation starts, so key the wait on run_id.
        from tests._helpers import wait_until

        async def _run2_parked() -> bool:
            st = await state_repo.get_state(root, project_id, epic_id)
            return st is not None and st.run_id == "run-2" and st.status == "waiting"

        try:
            await wait_until(_run2_parked, timeout=30.0, message="run-2 to park in waiting")
        finally:
            if not run2.done():
                await orch2.stop()
        await asyncio.wait_for(run2, timeout=10.0)

    # orch2 publishes a None sentinel on teardown → collector2 drains and ends.
    await asyncio.wait_for(collector2, timeout=5.0)

    # The continuation must have dispatched a worker after the reply...
    assert any(isinstance(e, WorkerStartedEvent) for e in events2), (
        "continuation should dispatch a worker after the reply"
    )
    # ...and completed T1.
    tf = await tasks_repo.get_tasks(root, project_id, epic_id)
    t1 = next(t for t in tf.tasks if t.id == "T1")
    assert t1.status == "done", f"continuation should finish T1, got {t1.status!r}"

    # A conversation run never emits run_completed (P3).  Filter by run_id:
    # any RunCompletedEvent here would be a regression.
    assert not any(
        isinstance(e, RunCompletedEvent) and e.run_id == "run-2" for e in events2
    ), "a conversation continuation must not emit run_completed"

    # The continuation parks exactly once — AFTER doing the work (its terminal
    # park), never before (which would strand the reply).
    run2_parks = [
        e for e in events2 if isinstance(e, UserInputRequestedEvent) and e.run_id == "run-2"
    ]
    assert len(run2_parks) == 1, (
        f"continuation should park exactly once (terminal park), got {len(run2_parks)}"
    )

    final = await state_repo.get_state(root, project_id, epic_id)
    assert final is not None
    assert final.status == "waiting", f"resumed run should rest in waiting, got {final.status!r}"
