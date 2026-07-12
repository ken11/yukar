"""Shared git bootstrap + run-lifecycle helpers for tests.

Promoted from the per-file copies that several test modules duplicated
(many self-labelled "duplicated from test_orchestration.py").

Lifecycle redesign: a conversation run (Manager / Reviewer) never
completes on its own — every ended turn parks the run in ``waiting`` and the
run task stays alive for the next user message.  Tests that drive a scripted
orchestrator therefore wait for the park (``wait_for_run_status``) and then
stop the run (``run_until_parked``) instead of awaiting ``start()``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any


def git_env() -> dict[str, str]:
    """Return os.environ augmented with a deterministic git identity."""
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
    }


def make_git_repo(parent: Path, name: str = "repo") -> Path:
    """Create a minimal git repo under *parent*/*name* with one commit on 'main'.

    Returns the repo path.
    """
    repo = parent / name
    repo.mkdir()
    env = git_env()

    def g(*args: str) -> str:
        r = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True, env=env
        )
        assert r.returncode == 0, f"git {args}: {r.stderr}"
        return r.stdout.strip()

    g("init", "-b", "main")
    g("config", "user.email", "test@test.com")
    g("config", "user.name", "Test")
    (repo / "README.md").write_text("# test\n")
    g("add", ".")
    g("commit", "-m", "initial")
    return repo


async def wait_until(
    predicate: Callable[[], Awaitable[bool]],
    *,
    timeout: float = 15.0,
    interval: float = 0.05,
    message: str = "condition",
) -> None:
    """Poll an async predicate until it returns True or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"Timed out after {timeout}s waiting for {message}")


async def wait_for_run_status(
    root: str,
    project_id: str,
    epic_id: str,
    status: str = "waiting",
    *,
    timeout: float = 15.0,
) -> Any:
    """Poll state.yaml until it reaches *status*; return the RunState.

    The canonical way to detect "the scripted turn ended" under turn-end
    semantics: the run parks in ``waiting`` (it is the user's turn).
    """
    from yukar.storage import state_repo

    deadline = time.monotonic() + timeout
    state: Any = None
    while time.monotonic() < deadline:
        state = await state_repo.get_state(root, project_id, epic_id)
        if state is not None and state.status == status:
            return state
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"Timed out after {timeout}s waiting for run status {status!r} "
        f"(last: {getattr(state, 'status', None)!r})"
    )


async def run_until_parked(
    orch: Any,
    root: str,
    project_id: str,
    epic_id: str,
    run_id: str,
    *,
    timeout: float = 30.0,
) -> None:
    """Drive a scripted conversation run through its turn and stop at the park.

    Starts ``orch.start`` as a task, waits for the run to park in ``waiting``
    (the scripted turn ended), then issues a user stop so the run task
    finishes deterministically.  state.yaml stays ``waiting`` afterwards —
    a conversation run has no completed state.
    """
    run_task = asyncio.create_task(orch.start(root, project_id, epic_id, run_id))
    try:
        await wait_for_run_status(root, project_id, epic_id, "waiting", timeout=timeout)
    except BaseException:
        run_task.cancel()
        with contextlib.suppress(BaseException):
            await run_task
        raise
    await orch.stop()
    await asyncio.wait_for(run_task, timeout=10.0)
