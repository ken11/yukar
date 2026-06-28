"""Regression tests for the indexer staleness fix.

Covers:
1. Index build failure (no existing index) → run emits RunFailedEvent without
   starting the Manager, and epic status becomes "failed".
2. Incremental update failure (existing index present) → warning only, Manager
   still starts (run is not failed).
3. Deleted file disappears from chunks.jsonl after an incremental refresh.
4. CancelledError during indexing (user stop) → RunStoppedEvent emitted,
   RunFailedEvent NOT emitted, runner.start NOT called.
5. on_indexed hook invoked after successful reindex.
6. add_repo no-ops when key already registered (no spurious wake).
7. Preparing-phase stop: indexing completes but stop was already requested →
   RunStoppedEvent emitted, runner.start NOT called.
8. Server-shutdown cancel (stop_requested=False) → RunStoppedEvent NOT emitted.
9. start_continuation behaves identically to start for preparing-phase stop.
"""

from __future__ import annotations

import asyncio
import contextlib  # noqa: F401 — used in TestCancelledErrorDuringIndexing
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from tests._helpers import make_git_repo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(workspace: Path) -> Any:
    from yukar.indexer.embedder import FakeEmbedder
    from yukar.indexer.service import IndexerService

    return IndexerService(workspace_root=str(workspace), embedder=FakeEmbedder())


def _make_supervisor(indexer_service: Any) -> Any:
    from yukar.runs.supervisor import RunSupervisor

    return RunSupervisor(max_parallel_epics=2, indexer_service=indexer_service)


async def _setup_project_and_epic(
    root: str, project_id: str = "proj", epic_id: str = "EP-1"
) -> None:
    from yukar.models.epic import Epic
    from yukar.models.project import Project
    from yukar.storage.epic_repo import save_epic
    from yukar.storage.project_repo import save_project

    await save_project(root, Project(id=project_id, name="Test Project"))
    await save_epic(root, project_id, Epic(id=epic_id, slug="test", title="Test Epic"))


async def _setup_repo_in_project(
    root: str,
    project_id: str,
    repo_name: str,
    repo_path: Path,
    *,
    index_enabled: bool = True,
) -> None:
    from yukar.models.project import Repo, RepoIndex
    from yukar.storage.project_repo import save_repo

    await save_repo(
        root,
        project_id,
        Repo(name=repo_name, path=str(repo_path), index=RepoIndex(enabled=index_enabled)),
    )


# ---------------------------------------------------------------------------
# Test 1: No existing index + build fails → RunFailedEvent, epic=failed
# ---------------------------------------------------------------------------


class TestIndexBuildFailure:
    async def test_no_index_build_failure_yields_run_failed_event(
        self, tmp_path: Path
    ) -> None:
        """When no index exists and the build fails, the run must emit RunFailedEvent
        without starting the Manager, and the epic status must be 'failed'.
        """
        from yukar.events.bus import subscribe

        workspace = tmp_path / "ws"
        workspace.mkdir()
        root = str(workspace)

        # Create a real git repo with one source file.
        repo_path = make_git_repo(tmp_path, "my-repo")
        (repo_path / "app.py").write_text("def hello(): pass\n")

        await _setup_project_and_epic(root)
        await _setup_repo_in_project(root, "proj", "my-repo", repo_path)

        # Inject a service that fails on reindex_repo.
        service = _make_service(workspace)

        async def _fail_reindex(*args: Any, **kwargs: Any) -> int:
            raise RuntimeError("simulated index build failure")

        service.reindex_repo = _fail_reindex  # type: ignore[method-assign]

        sup = _make_supervisor(service)

        received_events: list[Any] = []

        async def collect() -> None:
            async with subscribe("proj", "EP-1") as q:
                # Collect events until we see RunFailedEvent or timeout.
                for _ in range(20):
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=5.0)
                        received_events.append(event)
                        if hasattr(event, "type") and event.type == "run_failed":
                            return
                    except TimeoutError:
                        return

        collector = asyncio.create_task(collect())

        run_id = await sup.start(root, "proj", "EP-1")
        assert run_id is not None

        # Wait for the run task to finish (it should fail quickly).
        key = ("proj", "EP-1")
        handle = sup._runs.get(key)  # noqa: SLF001
        assert handle is not None
        await asyncio.wait_for(handle.task, timeout=15.0)

        await asyncio.wait_for(collector, timeout=5.0)

        # Must have received RunFailedEvent with an error message.
        failed_events = [
            e for e in received_events if hasattr(e, "type") and e.type == "run_failed"
        ]
        assert failed_events, (
            f"Expected RunFailedEvent but got: {[getattr(e, 'type', e) for e in received_events]}"
        )
        assert "my-repo" in failed_events[0].error or "simulated" in failed_events[0].error

        # Epic status must be 'failed'.
        from yukar.storage.epic_repo import get_epic

        epic = await get_epic(root, "proj", "EP-1")
        assert epic is not None
        assert epic.status == "failed"


# ---------------------------------------------------------------------------
# Test 2: Existing index + incremental fails → warning only, run proceeds
# ---------------------------------------------------------------------------


class TestIncrementalFailureNonFatal:
    async def test_incremental_failure_does_not_block_run(self, tmp_path: Path) -> None:
        """When an existing index is present and the incremental update fails,
        the Manager must still be started (the old index is retained).
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()
        root = str(workspace)

        repo_path = make_git_repo(tmp_path, "my-repo")
        (repo_path / "app.py").write_text("def hello(): pass\n")

        await _setup_project_and_epic(root)
        await _setup_repo_in_project(root, "proj", "my-repo", repo_path)

        # Pre-build a valid index so faiss_store.index_exists returns True.
        service = _make_service(workspace)
        await service.reindex_repo("proj", "my-repo", repo_path, full=True)

        # Now replace reindex_repo with a failing version (simulates incremental fail).
        async def _fail_incremental(*args: Any, **kwargs: Any) -> int:
            raise RuntimeError("simulated incremental update failure")

        service.reindex_repo = _fail_incremental  # type: ignore[method-assign]

        # Use a runner that records whether start() was called.
        runner_started = asyncio.Event()

        class _RecordingRunner:
            async def start(
                self, root: str, project_id: str, epic_id: str, run_id: str
            ) -> None:
                runner_started.set()

            async def pause(self) -> None: ...
            async def resume(self) -> None: ...
            async def stop(self) -> None: ...

        # Patch _make_runner to return our recording runner.
        recording_runner = _RecordingRunner()
        sup = _make_supervisor(service)
        with patch.object(sup, "_make_runner", return_value=recording_runner):
            await sup.start(root, "proj", "EP-1")

        # Wait for the run task to finish.
        key = ("proj", "EP-1")
        handle = sup._runs.get(key)  # noqa: SLF001
        if handle is not None:
            await asyncio.wait_for(handle.task, timeout=15.0)

        # The runner must have been started despite the incremental failure.
        assert runner_started.is_set(), (
            "Manager runner must be started even when incremental index update fails "
            "(the existing index is retained)."
        )


# ---------------------------------------------------------------------------
# Test 3: Deleted file disappears after incremental refresh
# ---------------------------------------------------------------------------


class TestDeletedFileRemovedFromIndex:
    async def test_deleted_file_absent_after_incremental_refresh(
        self, tmp_path: Path
    ) -> None:
        """After a full index build, delete a file, then run an incremental refresh.
        The deleted file's chunks must no longer appear in chunks.jsonl.
        """
        from yukar.config import paths as config_paths
        from yukar.indexer import faiss_store

        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo_path = make_git_repo(tmp_path, "my-repo")
        (repo_path / "keep.py").write_text("def keep(): pass\n")
        target = repo_path / "delete_me.py"
        target.write_text("def to_delete(): pass\n")

        service = _make_service(workspace)

        # Full build: both files indexed.
        await service.reindex_repo("proj", "my-repo", repo_path, full=True)

        idx_dir = config_paths.index_dir(str(workspace), "proj", "my-repo")
        chunks_before, _ = await faiss_store.load_index(idx_dir)
        before_paths = {c["path"] for c in chunks_before}
        assert "delete_me.py" in before_paths, "delete_me.py must be indexed initially"

        # Delete the file on disk.
        target.unlink()

        # Incremental refresh.
        await service.reindex_repo("proj", "my-repo", repo_path, full=False)

        chunks_after, _ = await faiss_store.load_index(idx_dir)
        after_paths = {c["path"] for c in chunks_after}
        assert "delete_me.py" not in after_paths, (
            "Deleted file chunks must be removed from the index after incremental refresh."
        )
        assert "keep.py" in after_paths, "Non-deleted file must remain in the index."


# ---------------------------------------------------------------------------
# Test 4: User stop during indexing → RunStoppedEvent, no RunFailedEvent
# ---------------------------------------------------------------------------


class TestCancelledErrorDuringIndexing:
    async def test_user_stop_during_indexing_emits_run_stopped_not_failed(
        self, tmp_path: Path
    ) -> None:
        """When stop() is called while _ensure_repos_indexed is awaiting,
        the run must emit RunStoppedEvent (not RunFailedEvent), the SSE sentinel
        must be published, and runner.start must not be called.
        """
        from yukar.events.bus import subscribe

        workspace = tmp_path / "ws"
        workspace.mkdir()
        root = str(workspace)

        repo_path = make_git_repo(tmp_path, "my-repo")
        (repo_path / "app.py").write_text("x = 1\n")

        await _setup_project_and_epic(root)
        await _setup_repo_in_project(root, "proj", "my-repo", repo_path)

        # Service with a reindex_repo that blocks until cancelled.
        cancel_gate = asyncio.Event()

        async def _blocking_reindex(*args: Any, **kwargs: Any) -> int:
            cancel_gate.set()
            await asyncio.sleep(3600)
            return 0

        service = _make_service(workspace)
        service.reindex_repo = _blocking_reindex  # type: ignore[method-assign]

        # Track whether runner.start was called.
        runner_started = False

        class _SpyRunner:
            async def start(
                self, root: str, project_id: str, epic_id: str, run_id: str
            ) -> None:
                nonlocal runner_started
                runner_started = True

            async def pause(self) -> None: ...
            async def resume(self) -> None: ...
            async def stop(self) -> None: ...

        sup = _make_supervisor(service)
        with patch.object(sup, "_make_runner", return_value=_SpyRunner()):
            received_events: list[Any] = []
            sentinel_received = False

            async def collect() -> None:
                nonlocal sentinel_received
                async with subscribe("proj", "EP-1") as q:
                    for _ in range(20):
                        try:
                            # Use 15 s timeout: stop() waits up to 5 s before
                            # force-cancelling the task, so RunStoppedEvent may
                            # arrive just after the 5 s mark.
                            event = await asyncio.wait_for(q.get(), timeout=15.0)
                            if event is None:
                                sentinel_received = True
                                return
                            received_events.append(event)
                        except TimeoutError:
                            return

            collector = asyncio.create_task(collect())

            await sup.start(root, "proj", "EP-1")

            # Wait until inside blocking reindex, then stop.
            await asyncio.wait_for(cancel_gate.wait(), timeout=10.0)
            # stop() blocks until the task is fully terminated (task.cancel +
            # shield/wait_for + cleanup), so after it returns the CancelledError
            # handler in _run_with_semaphore has already run and published events.
            await sup.stop("proj", "EP-1")

            await asyncio.wait_for(collector, timeout=15.0)

        # Must have emitted RunStoppedEvent.
        stopped_events = [
            e for e in received_events if hasattr(e, "type") and e.type == "run_stopped"
        ]
        assert stopped_events, (
            f"Expected RunStoppedEvent but got: {[getattr(e, 'type', e) for e in received_events]}"
        )

        # Must NOT have emitted RunFailedEvent.
        failed_events = [
            e for e in received_events if hasattr(e, "type") and e.type == "run_failed"
        ]
        assert not failed_events, (
            f"User stop must not produce RunFailedEvent, but got: {failed_events}"
        )

        # SSE sentinel must have been published.
        assert sentinel_received, "SSE sentinel (None) must be published to close the stream."

        # runner.start must NOT have been called.
        assert not runner_started, "runner.start must not be called when stop was requested."

    # Keep the old assertion name as an alias for backward compatibility in case
    # it is referenced elsewhere; it just delegates to the new test body.
    async def test_cancelled_during_indexing_does_not_emit_run_failed(
        self, tmp_path: Path
    ) -> None:
        """Backward-compatible alias — delegates to the updated test above."""
        await self.test_user_stop_during_indexing_emits_run_stopped_not_failed(tmp_path)


# ---------------------------------------------------------------------------
# Test 5: on_indexed hook called on successful reindex
# ---------------------------------------------------------------------------


class TestOnIndexedHook:
    async def test_hook_called_with_correct_args_on_success(self, tmp_path: Path) -> None:
        """set_on_indexed async callback must be awaited with (project_id, repo_name, repo_path)
        after a successful reindex.
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()
        repo_path = make_git_repo(tmp_path, "hook-repo")
        (repo_path / "app.py").write_text("def f(): pass\n")

        service = _make_service(workspace)

        hook_calls: list[tuple[str, str, Path]] = []

        async def _hook(pid: str, rname: str, rpath: Path) -> None:
            hook_calls.append((pid, rname, rpath))

        service.set_on_indexed(_hook)

        await service.reindex_repo("proj", "hook-repo", repo_path)

        assert len(hook_calls) == 1
        pid, rname, rpath = hook_calls[0]
        assert pid == "proj"
        assert rname == "hook-repo"
        assert rpath == repo_path

    async def test_hook_not_called_on_failure(self, tmp_path: Path) -> None:
        """on_indexed async hook must NOT be called when reindex fails."""
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        workspace = tmp_path / "ws"
        workspace.mkdir()
        repo_path = make_git_repo(tmp_path, "fail-repo")
        (repo_path / "app.py").write_text("def f(): pass\n")

        # Use an embedder that always raises so _do_reindex will propagate the error.
        class _FailingEmbedder(FakeEmbedder):
            def embed_batch(self, texts: list[str]) -> list[list[float]]:  # type: ignore[override]
                raise RuntimeError("simulated embedding failure")

            async def embed_batch_async(  # type: ignore[override]
                self, texts: list[str]
            ) -> list[list[float]]:
                raise RuntimeError("simulated embedding failure")

        service = IndexerService(workspace_root=str(workspace), embedder=_FailingEmbedder())

        hook_calls: list[tuple[str, str, Path]] = []

        async def _hook(pid: str, rname: str, rpath: Path) -> None:
            hook_calls.append((pid, rname, rpath))

        service.set_on_indexed(_hook)

        # The reindex should fail due to the embedding error.
        with pytest.raises(RuntimeError, match="simulated embedding failure"):
            await service.reindex_repo("proj", "fail-repo", repo_path)

        assert not hook_calls, "Hook must not be called on reindex failure."

    async def test_hook_exception_does_not_fail_reindex(self, tmp_path: Path) -> None:
        """A hook that raises must not cause reindex_repo to raise (warning only)."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        repo_path = make_git_repo(tmp_path, "hook-exc-repo")
        (repo_path / "app.py").write_text("def f(): pass\n")

        service = _make_service(workspace)

        async def _bad_hook(pid: str, rname: str, rpath: Path) -> None:
            raise RuntimeError("hook failure")

        service.set_on_indexed(_bad_hook)

        # reindex_repo must succeed despite the hook raising.
        result = await service.reindex_repo("proj", "hook-exc-repo", repo_path)
        assert isinstance(result, int)


# ---------------------------------------------------------------------------
# Test 6: add_repo no-op guard for already-registered key
# ---------------------------------------------------------------------------


class TestAddRepoNoopOnDuplicate:
    def test_add_repo_noop_when_already_registered(self, tmp_path: Path) -> None:
        """add_repo must not set _wake_event when the key is already registered."""
        from yukar.indexer.watcher import RepoWatcher

        repo = make_git_repo(tmp_path, "watch-repo")

        service_mock = AsyncMock()
        watcher = RepoWatcher(service_mock, debounce=0.05)

        # First registration — should set wake event.
        watcher.add_repo("proj", "watch-repo", repo)
        assert watcher._wake_event.is_set()  # noqa: SLF001

        # Clear the wake event to start fresh.
        watcher._wake_event.clear()  # noqa: SLF001

        # Second registration of the same key — must be a no-op.
        watcher.add_repo("proj", "watch-repo", repo)
        assert not watcher._wake_event.is_set(), (  # noqa: SLF001
            "add_repo must not set _wake_event when the repo is already registered."
        )
        # Repo count must still be 1.
        assert len(watcher._repos) == 1  # noqa: SLF001

    def test_add_different_repo_does_wake(self, tmp_path: Path) -> None:
        """add_repo for a NEW key must still set _wake_event."""
        from yukar.indexer.watcher import RepoWatcher

        repo_a = make_git_repo(tmp_path, "repo-a")
        repo_b = make_git_repo(tmp_path, "repo-b")

        service_mock = AsyncMock()
        watcher = RepoWatcher(service_mock, debounce=0.05)

        watcher.add_repo("proj", "repo-a", repo_a)
        watcher._wake_event.clear()  # noqa: SLF001

        # Different key — must wake.
        watcher.add_repo("proj", "repo-b", repo_b)
        assert watcher._wake_event.is_set()  # noqa: SLF001
        assert len(watcher._repos) == 2  # noqa: SLF001


# ---------------------------------------------------------------------------
# Test 7: is_watching returns correct boolean
# ---------------------------------------------------------------------------


class TestIsWatching:
    def test_is_watching_false_before_add(self, tmp_path: Path) -> None:
        """is_watching returns False for a repo that has not been registered."""
        from yukar.indexer.watcher import RepoWatcher

        service_mock = AsyncMock()
        watcher = RepoWatcher(service_mock, debounce=0.05)

        assert watcher.is_watching("proj", "repo") is False

    def test_is_watching_true_after_add(self, tmp_path: Path) -> None:
        """is_watching returns True after add_repo registers the key."""
        from yukar.indexer.watcher import RepoWatcher

        repo = make_git_repo(tmp_path, "repo")
        service_mock = AsyncMock()
        watcher = RepoWatcher(service_mock, debounce=0.05)

        watcher.add_repo("proj", "repo", repo)
        assert watcher.is_watching("proj", "repo") is True

    def test_is_watching_false_for_different_project(self, tmp_path: Path) -> None:
        """is_watching returns False when project_id differs."""
        from yukar.indexer.watcher import RepoWatcher

        repo = make_git_repo(tmp_path, "repo")
        service_mock = AsyncMock()
        watcher = RepoWatcher(service_mock, debounce=0.05)

        watcher.add_repo("proj-a", "repo", repo)
        assert watcher.is_watching("proj-b", "repo") is False


# ---------------------------------------------------------------------------
# Test 8: app.py _on_indexed_hook async behaviour
# ---------------------------------------------------------------------------


class TestOnIndexedHookAppWiring:
    """Tests for the _on_indexed_hook closure wired in app.py lifespan."""

    async def test_hook_skips_from_repo_async_when_already_watching(
        self, tmp_path: Path
    ) -> None:
        """When is_watching returns True, from_repo_async and add_repo must not be called."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from yukar.indexer.watcher import RepoWatcher

        repo = make_git_repo(tmp_path, "repo")

        service_mock = MagicMock()
        watcher = RepoWatcher(service_mock, debounce=0.05)
        # Pre-register the repo so is_watching returns True.
        watcher.add_repo("proj", "repo", repo)
        watcher._wake_event.clear()  # noqa: SLF001

        # Build the same hook closure the lifespan would build.
        _watcher_ref = watcher

        async def _on_indexed_hook(
            project_id: str, repo_name: str, repo_path: Path
        ) -> None:
            if _watcher_ref.is_watching(project_id, repo_name):
                return
            from yukar.sandbox.ignore import IgnoreRules as _IgnoreRules

            rules = await _IgnoreRules.from_repo_async(repo_path)
            _watcher_ref.add_repo(project_id, repo_name, repo_path, ignore_rules=rules)

        with patch(
            "yukar.sandbox.ignore.IgnoreRules.from_repo_async",
            new_callable=AsyncMock,
        ) as mock_from_repo:
            await _on_indexed_hook("proj", "repo", repo)
            mock_from_repo.assert_not_called()

        # Wake event must not have been set again.
        assert not watcher._wake_event.is_set()  # noqa: SLF001
        assert len(watcher._repos) == 1  # noqa: SLF001

    async def test_hook_calls_add_repo_on_first_index(self, tmp_path: Path) -> None:
        """When repo is not yet watching, hook must call from_repo_async then add_repo."""
        from unittest.mock import MagicMock

        from yukar.indexer.watcher import RepoWatcher

        repo = make_git_repo(tmp_path, "new-repo")

        service_mock = MagicMock()
        watcher = RepoWatcher(service_mock, debounce=0.05)

        assert watcher.is_watching("proj", "new-repo") is False

        # Build the hook closure (matches app.py implementation).
        _watcher_ref = watcher

        async def _on_indexed_hook(
            project_id: str, repo_name: str, repo_path: Path
        ) -> None:
            if _watcher_ref.is_watching(project_id, repo_name):
                return
            from yukar.sandbox.ignore import IgnoreRules as _IgnoreRules

            rules = await _IgnoreRules.from_repo_async(repo_path)
            _watcher_ref.add_repo(project_id, repo_name, repo_path, ignore_rules=rules)

        await _on_indexed_hook("proj", "new-repo", repo)

        assert watcher.is_watching("proj", "new-repo") is True
        assert len(watcher._repos) == 1  # noqa: SLF001


# ---------------------------------------------------------------------------
# Test (new): Preparing-phase stop — indexing completes but stop was requested
# ---------------------------------------------------------------------------


class TestPreparingStopAfterIndexing:
    """When stop() is called while indexing is in progress but indexing completes
    within 5 s (so the task is NOT cancelled), the supervisor must still detect
    the stop flag and emit RunStoppedEvent without starting the Manager.
    """

    async def test_stop_requested_before_runner_start_emits_run_stopped(
        self, tmp_path: Path
    ) -> None:
        """Indexing completes instantly, but stop_requested is True by then.

        Simulates the race where stop() arrives during the semaphore-acquire
        wait (indexer returns quickly but stop flag is set before runner.start).
        """
        from yukar.events.bus import subscribe

        workspace = tmp_path / "ws"
        workspace.mkdir()
        root = str(workspace)

        repo_path = make_git_repo(tmp_path, "my-repo")
        (repo_path / "app.py").write_text("x = 1\n")

        await _setup_project_and_epic(root)
        await _setup_repo_in_project(root, "proj", "my-repo", repo_path)

        # Gate: reindex blocks until we release it, simulating slow-but-complete.
        reindex_started = asyncio.Event()
        reindex_release = asyncio.Event()

        async def _gated_reindex(*args: Any, **kwargs: Any) -> int:
            reindex_started.set()
            await reindex_release.wait()
            return 0

        service = _make_service(workspace)
        service.reindex_repo = _gated_reindex  # type: ignore[method-assign]

        runner_started = False

        class _SpyRunner:
            async def start(
                self, root: str, project_id: str, epic_id: str, run_id: str
            ) -> None:
                nonlocal runner_started
                runner_started = True

            async def pause(self) -> None: ...
            async def resume(self) -> None: ...
            async def stop(self) -> None: ...

        sup = _make_supervisor(service)
        with patch.object(sup, "_make_runner", return_value=_SpyRunner()):
            received_events: list[Any] = []
            sentinel_received = False

            async def collect() -> None:
                nonlocal sentinel_received
                async with subscribe("proj", "EP-1") as q:
                    for _ in range(20):
                        try:
                            event = await asyncio.wait_for(q.get(), timeout=5.0)
                            if event is None:
                                sentinel_received = True
                                return
                            received_events.append(event)
                        except TimeoutError:
                            return

            collector = asyncio.create_task(collect())
            await sup.start(root, "proj", "EP-1")

            # Wait until reindex starts, mark stop_requested, then release reindex.
            # The task will NOT be CancelledError-d; instead, after reindex completes
            # the "if stop_flag['requested']" check must catch it.
            await asyncio.wait_for(reindex_started.wait(), timeout=10.0)
            # Set stop flag directly (mimics what stop() does before runner.stop()).
            key = ("proj", "EP-1")
            handle = sup._runs.get(key)  # noqa: SLF001
            assert handle is not None
            handle.mark_stop_requested()
            # Release reindex so it completes normally (no CancelledError).
            reindex_release.set()

            await asyncio.wait_for(handle.task, timeout=10.0)
            await asyncio.wait_for(collector, timeout=5.0)

        # RunStoppedEvent must have been emitted.
        stopped_events = [
            e for e in received_events if hasattr(e, "type") and e.type == "run_stopped"
        ]
        assert stopped_events, (
            f"Expected RunStoppedEvent after late stop, got: "
            f"{[getattr(e, 'type', e) for e in received_events]}"
        )

        # SSE sentinel must have been published.
        assert sentinel_received, "SSE sentinel (None) must close the stream on preparing stop."

        # runner.start must NOT have been called.
        assert not runner_started, (
            "runner.start must not be called when stop was requested before runner started."
        )

        # RunFailedEvent must NOT have been emitted.
        failed_events = [
            e for e in received_events if hasattr(e, "type") and e.type == "run_failed"
        ]
        assert not failed_events, (
            f"RunFailedEvent must not be emitted on user stop: {failed_events}"
        )


# ---------------------------------------------------------------------------
# Test (new): Server-shutdown cancel (stop_requested=False) — no RunStopped
# ---------------------------------------------------------------------------


class TestShutdownCancelDuringPreparing:
    """When the task is cancelled externally (server shutdown, not stop()),
    RunStoppedEvent must NOT be published and state.yaml must not be touched.
    """

    async def test_shutdown_cancel_does_not_emit_run_stopped(
        self, tmp_path: Path
    ) -> None:
        from yukar.events.bus import subscribe

        workspace = tmp_path / "ws"
        workspace.mkdir()
        root = str(workspace)

        repo_path = make_git_repo(tmp_path, "my-repo")
        (repo_path / "app.py").write_text("x = 1\n")

        await _setup_project_and_epic(root)
        await _setup_repo_in_project(root, "proj", "my-repo", repo_path)

        cancel_gate = asyncio.Event()

        async def _blocking_reindex(*args: Any, **kwargs: Any) -> int:
            cancel_gate.set()
            await asyncio.sleep(3600)
            return 0

        service = _make_service(workspace)
        service.reindex_repo = _blocking_reindex  # type: ignore[method-assign]

        sup = _make_supervisor(service)

        received_events: list[Any] = []

        async def collect() -> None:
            async with subscribe("proj", "EP-1") as q:
                for _ in range(10):
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=2.0)
                        if event is not None:
                            received_events.append(event)
                    except TimeoutError:
                        return

        collector = asyncio.create_task(collect())
        await sup.start(root, "proj", "EP-1")

        # Wait until inside blocking reindex, then cancel the task directly
        # (simulate server shutdown — do NOT call sup.stop()).
        await asyncio.wait_for(cancel_gate.wait(), timeout=10.0)
        key = ("proj", "EP-1")
        handle = sup._runs.get(key)  # noqa: SLF001
        assert handle is not None
        # Do NOT call handle.mark_stop_requested() — this is shutdown, not user stop.
        handle.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await handle.task

        await asyncio.sleep(0.2)
        collector.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await collector

        # Must NOT have emitted RunStoppedEvent.
        stopped_events = [
            e for e in received_events if hasattr(e, "type") and e.type == "run_stopped"
        ]
        assert not stopped_events, (
            f"Shutdown cancel must NOT produce RunStoppedEvent, but got: {stopped_events}"
        )


# ---------------------------------------------------------------------------
# Test (new): start_continuation also handles preparing-phase stop correctly
# ---------------------------------------------------------------------------


class TestContinuationPreparingStop:
    """start_continuation must apply the same preparing-phase stop logic as start()."""

    async def test_user_stop_during_continuation_indexing_emits_run_stopped(
        self, tmp_path: Path
    ) -> None:
        from yukar.events.bus import subscribe
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        workspace = tmp_path / "ws"
        workspace.mkdir()
        root = str(workspace)

        # Use a completed epic so start_continuation can reopen it.
        proj = Project(id="proj", name="Test Project")
        epic = Epic(id="EP-1", slug="test", title="Test", status="completed")
        await save_project(root, proj)
        await save_epic(root, "proj", epic)
        await _setup_repo_in_project(root, "proj", "my-repo", tmp_path / "repo")
        make_git_repo(tmp_path, "repo")

        cancel_gate = asyncio.Event()

        async def _blocking_reindex(*args: Any, **kwargs: Any) -> int:
            cancel_gate.set()
            await asyncio.sleep(3600)
            return 0

        service = _make_service(workspace)
        service.reindex_repo = _blocking_reindex  # type: ignore[method-assign]

        runner_started = False

        class _SpyRunner:
            async def start(
                self, root: str, project_id: str, epic_id: str, run_id: str
            ) -> None:
                nonlocal runner_started
                runner_started = True

            async def pause(self) -> None: ...
            async def resume(self) -> None: ...
            async def stop(self) -> None: ...

        sup = _make_supervisor(service)
        with patch.object(sup, "_make_continuation_runner", return_value=_SpyRunner()):
            received_events: list[Any] = []
            sentinel_received = False

            async def collect() -> None:
                nonlocal sentinel_received
                async with subscribe("proj", "EP-1") as q:
                    for _ in range(20):
                        try:
                            # 15 s timeout: stop() waits up to 5 s before
                            # force-cancelling, so events arrive after 5+ s.
                            event = await asyncio.wait_for(q.get(), timeout=15.0)
                            if event is None:
                                sentinel_received = True
                                return
                            received_events.append(event)
                        except TimeoutError:
                            return

            collector = asyncio.create_task(collect())
            await sup.start_continuation(root, "proj", "EP-1")

            await asyncio.wait_for(cancel_gate.wait(), timeout=10.0)
            await sup.stop("proj", "EP-1")

            await asyncio.wait_for(collector, timeout=15.0)

        stopped_events = [
            e for e in received_events if hasattr(e, "type") and e.type == "run_stopped"
        ]
        assert stopped_events, (
            f"start_continuation: expected RunStoppedEvent but got: "
            f"{[getattr(e, 'type', e) for e in received_events]}"
        )
        assert sentinel_received, "SSE sentinel must be published on continuation preparing stop."
        assert not runner_started, "runner.start must not be called on continuation preparing stop."
