"""End-to-end: a run parked in awaiting_input survives a server restart and
resumes when the user replies.

This is the empirical proof for the recovery.py change (awaiting_input is NOT
downgraded to interrupted) + the continuation-resume path. It drives a real
orchestrator to awaiting_input (persisting a Strands session that ends on an
``ask_user`` tool exchange), simulates a crash (abandon the task + force the
on-disk state back to the awaiting snapshot), runs ``recover_interrupted_runs``
(=restart), then starts a continuation orchestrator with the user's reply as the
seed and asserts the Manager picks up the session, dispatches, and completes —
without re-entering awaiting_input or crashing on session restore.

Mechanics only (FakeModel): the model's semantic understanding of the prior
ask_user is out of scope; what matters is that restore + continuation does not
break, re-ask, or strand the run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests._helpers import make_git_repo as _make_bare_repo

from .test_ask_user_gate import _bootstrap


@pytest.mark.asyncio
async def test_awaiting_input_survives_restart_and_resumes_on_reply(tmp_path: Path) -> None:
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

    # --- Phase 1: drive a fresh run to awaiting_input (persist the session) ---
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
        ToolUseTurn(
            tool_name="ask_user",
            tool_input={"question": "Plan: T1=Write hello.py. Proceed?"},
        ),
        TextTurn("Waiting for user approval."),
    ]
    # Continuation manager (after the reply): dispatch then complete.
    resume_script = [
        ToolUseTurn(
            tool_name="dispatch",
            tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
        ),
        ToolUseTurn(tool_name="complete_epic", tool_input={}),
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

        state = await state_repo.get_state(root, project_id, epic_id)
        assert state is not None and state.status == "awaiting_input"
        assert state.pending_question == "Plan: T1=Write hello.py. Proceed?"

        # --- Simulate a graceful server shutdown ---
        # A `yukar serve` stop (SIGTERM/Ctrl-C) cancels the awaiting task WITHOUT
        # an explicit supervisor.stop() — i.e. self._stopped stays False. The
        # orchestrator's CancelledError handler must NOT rewrite state.yaml in
        # that case (the bug: it used to clobber awaiting_input → idle +
        # pending_question=None at shutdown). We do NOT force-write the state
        # back here on purpose: the assertion below proves shutdown preserves it.
        run1.cancel()
        await asyncio.gather(run1, return_exceptions=True)

    # The shutdown cancel must leave the awaiting snapshot intact on disk.
    state = await state_repo.get_state(root, project_id, epic_id)
    assert state is not None
    assert state.status == "awaiting_input", (
        f"shutdown must NOT clobber awaiting_input, got {state.status!r}"
    )
    assert state.pending_question == "Plan: T1=Write hello.py. Proceed?", (
        "shutdown must NOT clear pending_question"
    )

    # --- Phase 2: restart recovery must PRESERVE awaiting_input ---
    await recover_interrupted_runs(root)
    state = await state_repo.get_state(root, project_id, epic_id)
    assert state is not None, "state.yaml should still exist after recovery"
    assert state.status == "awaiting_input", (
        f"awaiting_input must survive restart, got {state.status!r}"
    )
    assert state.pending_question == "Plan: T1=Write hello.py. Proceed?", (
        "pending_question must survive restart"
    )

    # --- Phase 3: user replies -> continuation orchestrator resumes ---
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
        await asyncio.wait_for(orch2.start(root, project_id, epic_id, "run-2"), timeout=30.0)

    # orch2 publishes a None sentinel on completion → collector2 drains and ends.
    await asyncio.wait_for(collector2, timeout=5.0)

    # The continuation must have dispatched a worker and completed the run...
    assert any(isinstance(e, WorkerStartedEvent) for e in events2), (
        "continuation should dispatch a worker after the reply"
    )
    assert any(isinstance(e, RunCompletedEvent) for e in events2), "continuation should complete"
    # ...without re-entering awaiting_input during the resume turn. Filter by
    # run_id: any UserInputRequestedEvent here is orch1's ("run-1"), replayed
    # from the in-memory bus buffer (a real process restart wipes it; this
    # single-process test does not). The continuation run is "run-2".
    assert not any(
        isinstance(e, UserInputRequestedEvent) and e.run_id == "run-2" for e in events2
    ), "continuation (run-2) should NOT re-ask"

    final = await state_repo.get_state(root, project_id, epic_id)
    assert final is not None
    assert final.status == "completed", f"resumed run should complete, got {final.status!r}"
    assert final.pending_question is None, "pending_question cleared after resume"
