"""Regression tests for recovery per-run isolation.

Finding: ``recover_interrupted_runs`` aborted ALL recovery (and, since it is
awaited on the startup path, server startup) if a single per-run iteration
raised an error that the inner handlers did not catch.  Each per-run iteration
is now wrapped so one bad run/epic directory is logged and skipped while the
remaining runs still recover.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest


async def _seed_running_state(root: str, project_id: str, epic_id: str) -> None:
    from yukar.models.run import RunState
    from yukar.storage import state_repo

    await state_repo.save_state(
        root,
        project_id,
        epic_id,
        RunState(run_id=f"run-{epic_id}", status="running", started_at=datetime.now(UTC)),
    )


class TestRecoveryIsolatesBadRunDir:
    async def test_one_bad_epic_does_not_abort_others(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When one epic's recovery step raises unexpectedly, the other epic is
        still reconciled and recovery does not propagate the error."""
        from yukar.runs import recovery
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        pid = "proj"
        await _seed_running_state(root, pid, "EP-BAD")
        await _seed_running_state(root, pid, "EP-OK")

        real_state_yaml = recovery.paths.state_yaml

        def _boom_for_bad(r: str, p: str, e: str) -> Path:
            if e == "EP-BAD":
                raise OSError("simulated unexpected fs error during recovery")
            return real_state_yaml(r, p, e)

        # Patch the path lookup used inside the per-run loop body (outside the
        # inner try/except) so EP-BAD raises an error that, pre-fix, would have
        # aborted the entire recovery loop.
        monkeypatch.setattr(recovery.paths, "state_yaml", _boom_for_bad)

        # Must NOT raise — the bad epic is isolated.
        count = await recovery.recover_interrupted_runs(root)

        # EP-OK was still reconciled.
        assert count == 1
        ok_state = await state_repo.get_state(root, pid, "EP-OK")
        assert ok_state is not None
        assert ok_state.status == "interrupted"

        # EP-BAD was left untouched (still "running"); it was skipped, not crashed.
        # Restore the real path lookup so we can read it back.
        monkeypatch.setattr(recovery.paths, "state_yaml", real_state_yaml)
        bad_state = await state_repo.get_state(root, pid, "EP-BAD")
        assert bad_state is not None
        assert bad_state.status == "running"

    async def test_bad_project_dir_does_not_abort_recovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An error enumerating one project's epics must not abort recovery of
        other projects."""
        from yukar.runs import recovery
        from yukar.storage import state_repo

        root = str(tmp_path / "ws")
        await _seed_running_state(root, "proj-bad", "EP-1")
        await _seed_running_state(root, "proj-ok", "EP-1")

        real_epics_dir = recovery.paths.epics_dir

        def _boom_for_bad_project(r: str, p: str) -> Path:
            if p == "proj-bad":
                raise OSError("simulated error enumerating project epics")
            return real_epics_dir(r, p)

        monkeypatch.setattr(recovery.paths, "epics_dir", _boom_for_bad_project)

        count = await recovery.recover_interrupted_runs(root)

        # proj-ok still recovered.
        assert count == 1
        ok_state = await state_repo.get_state(root, "proj-ok", "EP-1")
        assert ok_state is not None
        assert ok_state.status == "interrupted"
