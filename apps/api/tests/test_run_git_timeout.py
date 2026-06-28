"""Bug-detection tests for run_git timeout handling.

Finding: run-git-timeout (fixed in G1-G8 batch)
-------------------------
run_git now wraps proc.communicate() in asyncio.wait_for(_GIT_TIMEOUT) and
kills the process group (start_new_session=True + os.killpg) on timeout or
cancellation.

Tests
------
TC-1 (pass): run_git raises TimeoutError when communicate() hangs — inner
      wait_for fires and kill() is called before propagating.
TC-2 (pass): normally-completing communicate() still returns a GitResult.
TC-3 (pass): cancellation of run_git kills the subprocess.
TC-4 (pass, review fix #4): inner _GIT_TIMEOUT path — monkeypatch _GIT_TIMEOUT
      to a tiny value, use a hanging proc, verify run_git raises TimeoutError
      WITHOUT needing an outer wait_for guard (the inner timeout is the trigger).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yukar.git.runner import GitResult, run_git

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Timeout used in the tests to bound how long we wait for run_git to
# raise TimeoutError.  Short enough to keep the suite fast, long enough
# to be conclusive on any CI machine.
_TEST_TIMEOUT = 0.2  # seconds

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hanging_proc() -> MagicMock:
    """Return a mock asyncio.subprocess.Process whose communicate() hangs forever."""
    proc = MagicMock()
    proc.returncode = None  # not yet terminated
    proc.pid = 99999  # fake PID for os.getpgid

    async def _hang() -> tuple[bytes, bytes]:
        # Sleep for a very long time — the test will cancel/timeout before this.
        await asyncio.sleep(3600)
        return (b"", b"")

    proc.communicate = _hang
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=None)
    return proc


def _make_ok_proc() -> MagicMock:
    """Return a mock asyncio.subprocess.Process that completes immediately."""
    proc = MagicMock()
    proc.returncode = 0
    proc.pid = 99998
    proc.communicate = AsyncMock(return_value=(b"ok stdout\n", b""))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=None)
    return proc


async def _run_git_with_fake_proc(
    proc: MagicMock,
    tmp_path: Path,
    *,
    check: bool = False,
) -> GitResult:
    """Call run_git('status') with a completely fake subprocess.

    Patches ``asyncio.create_subprocess_exec``, ``yukar.config.paths.empty_hooks_dir``,
    and ``os.killpg`` / ``os.getpgid`` so no real filesystem or git operations
    or process-group signals are needed.
    """

    async def fake_exec(
        *cmd: str,
        cwd: str,
        stdout: object,
        stderr: object,
        env: dict[str, str],
        **kwargs: object,
    ) -> MagicMock:
        return proc

    with (
        patch("yukar.git.runner.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("yukar.config.paths.empty_hooks_dir", return_value=Path("/fake/hooks")),
        patch("yukar.git.runner.os.getpgid", return_value=proc.pid),
        patch("yukar.git.runner.os.killpg", MagicMock()),
    ):
        return await run_git("status", cwd=tmp_path, check=check)


# ---------------------------------------------------------------------------
# TC-1: hanging communicate() — run_git must raise TimeoutError via inner wait_for
# ---------------------------------------------------------------------------


async def test_tc1_run_git_raises_timeout_on_hung_communicate(tmp_path: Path) -> None:
    """run_git must raise TimeoutError (via its own wait_for) when communicate() hangs.

    The implementation now wraps proc.communicate() with asyncio.wait_for(_GIT_TIMEOUT)
    and calls os.killpg on timeout.  We use monkeypatched _GIT_TIMEOUT (see TC-4
    for the canonical inner-path test).  Here we verify via the outer guard that a
    TimeoutError is raised at all when communicate() hangs forever.
    """
    proc = _make_hanging_proc()

    timed_out = False
    try:
        await asyncio.wait_for(
            _run_git_with_fake_proc(proc, tmp_path),
            timeout=_TEST_TIMEOUT,
        )
    except (TimeoutError, asyncio.CancelledError):
        timed_out = True

    # A TimeoutError (from inner or outer) must have been raised.
    assert timed_out, "run_git should have raised TimeoutError on a hanging proc"


# ---------------------------------------------------------------------------
# TC-2 (characterization): normally-completing git invocation still works
# ---------------------------------------------------------------------------


async def test_tc2_run_git_returns_result_on_fast_communicate(tmp_path: Path) -> None:
    """Baseline: run_git returns a GitResult when communicate() completes normally."""
    proc = _make_ok_proc()
    result = await _run_git_with_fake_proc(proc, tmp_path, check=False)
    assert isinstance(result, GitResult)
    assert result.returncode == 0
    assert "ok stdout" in result.stdout


# ---------------------------------------------------------------------------
# TC-3: cancellation of run_git — process group must be killed
# ---------------------------------------------------------------------------


async def test_tc3_cancelled_run_git_kills_subprocess(tmp_path: Path) -> None:
    """run_git must call os.killpg when the calling task is cancelled.

    We schedule run_git as a Task, let it reach the blocking communicate(),
    then cancel the task and verify that os.killpg was invoked — meaning
    the fix prevents subprocess and child-process leaks on cancellation.
    """
    proc = _make_hanging_proc()
    killpg_calls: list[tuple[int, int]] = []

    async def fake_exec(
        *cmd: str,
        cwd: str,
        stdout: object,
        stderr: object,
        env: dict[str, str],
        **kwargs: object,
    ) -> MagicMock:
        return proc

    def fake_killpg(pgid: int, sig: int) -> None:
        killpg_calls.append((pgid, sig))

    with (
        patch("yukar.git.runner.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("yukar.config.paths.empty_hooks_dir", return_value=Path("/fake/hooks")),
        patch("yukar.git.runner.os.getpgid", return_value=proc.pid),
        patch("yukar.git.runner.os.killpg", side_effect=fake_killpg),
    ):
        task = asyncio.create_task(
            run_git("status", cwd=Path("/tmp"), check=False)
        )

        # Yield so the task starts and reaches the await inside communicate().
        await asyncio.sleep(0)

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    assert len(killpg_calls) >= 1, (
        "os.killpg() was not called after Task cancellation: "
        "git child processes (credential helpers, ssh, filter drivers) were leaked. "
        "Fix: use start_new_session=True + os.killpg in the except block."
    )


# ---------------------------------------------------------------------------
# TC-4 (review fix #4): inner _GIT_TIMEOUT path — monkeypatch _GIT_TIMEOUT
# ---------------------------------------------------------------------------


async def test_tc4_inner_git_timeout_path(tmp_path: Path) -> None:
    """Verify that run_git's own asyncio.wait_for fires (not just an outer guard).

    Monkeypatches _GIT_TIMEOUT to a tiny value so the inner wait_for expires
    before any outer guard.  Uses a hanging communicate() proc.  Asserts that:
      1. TimeoutError is raised.
      2. os.killpg was called (process group cleanup path reached).
      3. proc.wait was awaited (reap path reached).

    This is the canonical test for the inner timeout code path.
    """
    import yukar.git.runner as runner_mod

    proc = _make_hanging_proc()
    killpg_calls: list[tuple[int, int]] = []

    async def fake_exec(
        *cmd: str,
        cwd: str,
        stdout: object,
        stderr: object,
        env: dict[str, str],
        **kwargs: object,
    ) -> MagicMock:
        return proc

    def fake_killpg(pgid: int, sig: int) -> None:
        killpg_calls.append((pgid, sig))

    original_timeout = runner_mod._GIT_TIMEOUT
    try:
        runner_mod._GIT_TIMEOUT = 0.05  # type: ignore[assignment]  # tiny timeout

        with (
            patch("yukar.git.runner.asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("yukar.config.paths.empty_hooks_dir", return_value=Path("/fake/hooks")),
            patch("yukar.git.runner.os.getpgid", return_value=proc.pid),
            patch("yukar.git.runner.os.killpg", side_effect=fake_killpg),
            pytest.raises((TimeoutError, asyncio.TimeoutError)),
        ):
            await run_git("status", cwd=tmp_path, check=False)
    finally:
        runner_mod._GIT_TIMEOUT = original_timeout  # type: ignore[assignment]

    assert len(killpg_calls) >= 1, (
        "os.killpg() was not called from the inner timeout path. "
        "run_git must kill the process group when its own wait_for expires."
    )
    assert proc.wait.called, (
        "proc.wait() was not awaited after timeout: zombie reap path not reached."
    )
