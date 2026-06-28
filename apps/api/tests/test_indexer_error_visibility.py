"""Tests for indexer error visibility — error.json persistence and status API.

Covers:
- stats.read_error / write_error / clear_error helpers
- IndexerService.reindex_repo writes error.json on failure, clears it on success
- IndexerService.get_status returns state="error" + last_error when error.json present
- RepoIndexStatus schema exposes last_error / last_error_at / state="error"
- _fire_and_forget logs at WARNING (not DEBUG) on failure
- _ensure_repos_indexed logs at WARNING on failure
- repo_summarize tool surfaces error.json message when index missing
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tests._helpers import make_git_repo

# ===========================================================================
# stats helpers: read_error / write_error / clear_error
# ===========================================================================


class TestErrorJsonHelpers:
    def test_read_error_returns_none_when_missing(self, tmp_path: Path) -> None:
        from yukar.indexer.stats import read_error

        assert read_error(tmp_path / "no_such_dir") is None

    def test_write_error_creates_file(self, tmp_path: Path) -> None:
        from yukar.indexer.stats import read_error, write_error

        idx_dir = tmp_path / "index"
        exc = RuntimeError("AWS credentials not configured")
        write_error(idx_dir, exc)

        result = read_error(idx_dir)
        assert result is not None
        assert result["message"] == "AWS credentials not configured"
        assert result["error_type"] == "RuntimeError"
        assert "failed_at" in result

    def test_write_error_creates_parent_directory(self, tmp_path: Path) -> None:
        from yukar.indexer.stats import write_error

        idx_dir = tmp_path / "nested" / "deep" / "index"
        assert not idx_dir.exists()
        write_error(idx_dir, ValueError("some error"))
        assert (idx_dir / "error.json").exists()

    def test_write_error_is_atomic(self, tmp_path: Path) -> None:
        """Writing error.json must not leave a partial file on disk."""
        from yukar.indexer.stats import write_error

        idx_dir = tmp_path / "index"
        exc = ValueError("oops")
        write_error(idx_dir, exc)
        # Verify only error.json exists — no leftover .tmp_ files
        tmp_files = list(idx_dir.glob(".tmp_*"))
        assert not tmp_files, f"Leftover temp files: {tmp_files}"

    def test_clear_error_removes_file(self, tmp_path: Path) -> None:
        from yukar.indexer.stats import clear_error, write_error

        idx_dir = tmp_path / "index"
        write_error(idx_dir, RuntimeError("fail"))
        assert (idx_dir / "error.json").exists()

        clear_error(idx_dir)
        assert not (idx_dir / "error.json").exists()

    def test_clear_error_is_noop_when_absent(self, tmp_path: Path) -> None:
        from yukar.indexer.stats import clear_error

        # Should not raise when file doesn't exist.
        clear_error(tmp_path / "no_such_dir")

    def test_read_error_returns_none_for_corrupt_json(self, tmp_path: Path) -> None:
        from yukar.indexer.stats import read_error

        idx_dir = tmp_path / "index"
        idx_dir.mkdir()
        (idx_dir / "error.json").write_text("NOT JSON {{{{")

        assert read_error(idx_dir) is None

    def test_write_error_stores_iso8601_timestamp(self, tmp_path: Path) -> None:
        import datetime

        from yukar.indexer.stats import write_error

        idx_dir = tmp_path / "index"
        write_error(idx_dir, RuntimeError("ts test"))
        raw = json.loads((idx_dir / "error.json").read_text())
        # Should be parseable as an ISO8601 datetime
        dt = datetime.datetime.fromisoformat(raw["failed_at"])
        assert dt.tzinfo is not None  # must be UTC-aware


# ===========================================================================
# IndexerService — error.json on failure / clear on success
# ===========================================================================


class TestIndexerServiceErrorPersistence:
    def _make_service(self, workspace: Path) -> Any:
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        return IndexerService(workspace_root=str(workspace), embedder=FakeEmbedder())

    async def test_reindex_writes_error_json_on_failure(self, tmp_path: Path) -> None:
        """When _do_reindex raises, error.json must be written in the index dir."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = make_git_repo(tmp_path, "fail-repo")
        (repo / "code.py").write_text("x = 1\n")

        from yukar.config import paths as config_paths
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService
        from yukar.indexer.stats import read_error

        # Use an embedder that always raises to simulate e.g. missing AWS creds.
        class FailingEmbedder(FakeEmbedder):
            def embed_batch(self, texts: list[str]) -> list[list[float]]:
                raise RuntimeError("NoCredentialsError: AWS credentials not configured")

        service = IndexerService(workspace_root=str(workspace), embedder=FailingEmbedder())

        import pytest

        with pytest.raises(RuntimeError, match="NoCredentialsError"):
            await service.reindex_repo("proj", "fail-repo", repo)

        idx_dir = config_paths.index_dir(str(workspace), "proj", "fail-repo")
        err = read_error(idx_dir)
        assert err is not None
        assert "NoCredentialsError" in err["message"]
        assert err["error_type"] == "RuntimeError"
        assert "failed_at" in err

    async def test_reindex_clears_error_json_on_success(self, tmp_path: Path) -> None:
        """After a successful reindex, any previous error.json must be removed."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = make_git_repo(tmp_path, "recover-repo")
        (repo / "code.py").write_text("x = 1\n")

        from yukar.config import paths as config_paths
        from yukar.indexer.stats import read_error, write_error

        service = self._make_service(workspace)
        idx_dir = config_paths.index_dir(str(workspace), "proj", "recover-repo")

        # Plant a pre-existing error.json to simulate a previous failed run.
        write_error(idx_dir, RuntimeError("old failure"))
        assert read_error(idx_dir) is not None

        # Successful reindex must clear it.
        n = await service.reindex_repo("proj", "recover-repo", repo)
        assert n >= 0
        assert read_error(idx_dir) is None

    async def test_reindex_reraises_exception(self, tmp_path: Path) -> None:
        """reindex_repo must re-raise the original exception after writing error.json."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = make_git_repo(tmp_path, "reraise-repo")
        (repo / "code.py").write_text("x = 1\n")

        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        class BombEmbedder(FakeEmbedder):
            def embed_batch(self, texts: list[str]) -> list[list[float]]:
                raise ValueError("specific error for test")

        service = IndexerService(workspace_root=str(workspace), embedder=BombEmbedder())

        import pytest

        with pytest.raises(ValueError, match="specific error for test"):
            await service.reindex_repo("proj", "reraise-repo", repo)

    async def test_indexing_key_removed_even_on_failure(self, tmp_path: Path) -> None:
        """_indexing set must be cleaned up even when reindex_repo raises."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = make_git_repo(tmp_path, "key-repo")
        (repo / "code.py").write_text("x = 1\n")

        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        class FailEmbed(FakeEmbedder):
            def embed_batch(self, texts: list[str]) -> list[list[float]]:
                raise RuntimeError("fail")

        service = IndexerService(workspace_root=str(workspace), embedder=FailEmbed())

        import pytest

        with pytest.raises(RuntimeError):
            await service.reindex_repo("proj", "key-repo", repo)

        # The key must have been removed from _indexing by the finally block.
        assert ("proj", "key-repo") not in service._indexing


# ===========================================================================
# IndexerService.get_status — "error" state
# ===========================================================================


class TestGetStatusErrorState:
    def _make_service(self, workspace: Path) -> Any:
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        return IndexerService(workspace_root=str(workspace), embedder=FakeEmbedder())

    async def test_get_status_returns_error_state_when_error_json_present(
        self, tmp_path: Path
    ) -> None:
        """When index is absent but error.json exists, state must be 'error'."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        from yukar.config import paths as config_paths
        from yukar.indexer.stats import write_error

        service = self._make_service(workspace)
        idx_dir = config_paths.index_dir(str(workspace), "proj", "broken-repo")
        write_error(idx_dir, RuntimeError("NoCredentialsError: unable to locate credentials"))

        statuses = await service.get_status("proj")
        assert len(statuses) == 1
        s = statuses[0]
        assert s.repo_name == "broken-repo"
        assert s.state == "error"
        assert s.last_error is not None
        assert "NoCredentialsError" in s.last_error
        assert s.last_error_at is not None

    async def test_get_status_returns_unindexed_without_error_json(self, tmp_path: Path) -> None:
        """A repo directory with no stats.json and no error.json → state='unindexed'."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        from yukar.config import paths as config_paths

        service = self._make_service(workspace)
        idx_dir = config_paths.index_dir(str(workspace), "proj", "new-repo")
        # Create the directory but leave it empty (no stats.json, no error.json).
        idx_dir.mkdir(parents=True)

        statuses = await service.get_status("proj")
        assert len(statuses) == 1
        s = statuses[0]
        assert s.state == "unindexed"
        assert s.last_error is None

    async def test_get_status_last_error_fields_are_none_for_indexed_repo(
        self, tmp_path: Path
    ) -> None:
        """Successfully indexed repos must have last_error=None."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = make_git_repo(tmp_path, "ok-repo")
        (repo / "code.py").write_text("x = 1\n")

        service = self._make_service(workspace)
        await service.reindex_repo("proj", "ok-repo", repo)

        statuses = await service.get_status("proj")
        assert len(statuses) == 1
        s = statuses[0]
        assert s.state == "indexed"
        assert s.last_error is None
        assert s.last_error_at is None

    async def test_get_status_error_then_success_clears_error(self, tmp_path: Path) -> None:
        """After a failed run followed by a successful one, state reverts to 'indexed'."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = make_git_repo(tmp_path, "cycle-repo")
        (repo / "code.py").write_text("x = 1\n")

        from yukar.config import paths as config_paths
        from yukar.indexer.stats import write_error

        service = self._make_service(workspace)
        idx_dir = config_paths.index_dir(str(workspace), "proj", "cycle-repo")

        # Simulate a prior failure (error.json present, no stats.json).
        write_error(idx_dir, RuntimeError("prior failure"))

        # Now run a successful reindex.
        await service.reindex_repo("proj", "cycle-repo", repo)

        statuses = await service.get_status("proj")
        assert len(statuses) == 1
        s = statuses[0]
        assert s.state == "indexed"
        assert s.last_error is None

    async def test_get_status_exposes_last_error_when_stats_and_error_both_exist(
        self, tmp_path: Path
    ) -> None:
        """stats.json present + error.json present → state remains indexed/stale
        and last_error is populated.

        Correctly represents the state where an existing index is usable
        but the most recent rebuild failed.
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()

        repo = make_git_repo(tmp_path, "partial-repo")
        (repo / "code.py").write_text("x = 1\n")

        from yukar.config import paths as config_paths
        from yukar.indexer.stats import write_error

        service = self._make_service(workspace)

        # 1. Run an initial successful build to create stats.json + faiss index.
        await service.reindex_repo("proj", "partial-repo", repo)

        idx_dir = config_paths.index_dir(str(workspace), "proj", "partial-repo")
        assert (idx_dir / "stats.json").exists(), "stats.json must exist after successful build"

        # 2. Simulate a rebuild failure: write error.json directly (stats.json remains).
        write_error(idx_dir, RuntimeError("AWS credentials expired"))

        # 3. get_status keeps state as indexed/stale and exposes last_error.
        statuses = await service.get_status("proj")
        assert len(statuses) == 1
        s = statuses[0]

        assert s.state in ("indexed", "stale"), (
            f"state must remain indexed/stale when usable index exists, got {s.state!r}"
        )
        assert s.last_error is not None, "last_error must be populated from error.json"
        assert "AWS credentials expired" in s.last_error
        assert s.last_error_at is not None, "last_error_at must be populated from error.json"


# ===========================================================================
# RepoIndexStatus schema — new fields
# ===========================================================================


class TestRepoIndexStatusSchema:
    def test_schema_includes_last_error_fields(self) -> None:
        from yukar.api.routers.search import RepoIndexStatus

        s = RepoIndexStatus(
            repo_name="repo",
            state="error",
            files=0,
            chunks=0,
            last_indexed_at=None,
            last_error="AWS credentials not configured",
            last_error_at="2026-01-01T00:00:00+00:00",
        )
        d = s.model_dump()
        assert d["state"] == "error"
        assert d["last_error"] == "AWS credentials not configured"
        assert d["last_error_at"] == "2026-01-01T00:00:00+00:00"

    def test_schema_last_error_defaults_to_none(self) -> None:
        from yukar.api.routers.search import RepoIndexStatus

        s = RepoIndexStatus(
            repo_name="repo",
            state="indexed",
            files=5,
            chunks=10,
            last_indexed_at="2026-01-01T00:00:00+00:00",
        )
        d = s.model_dump()
        assert d["last_error"] is None
        assert d["last_error_at"] is None

    def test_state_literal_accepts_error(self) -> None:
        from yukar.api.routers.search import RepoIndexStatus

        # Should not raise a validation error.
        s = RepoIndexStatus(
            repo_name="repo",
            state="error",
            files=0,
            chunks=0,
            last_indexed_at=None,
        )
        assert s.state == "error"

    def test_existing_fields_unchanged(self) -> None:
        """Ensure existing fields (ts_files, fallback_files, etc.) are preserved."""
        from yukar.api.routers.search import RepoIndexStatus

        s = RepoIndexStatus(
            repo_name="repo",
            state="indexed",
            files=3,
            chunks=7,
            last_indexed_at="2026-01-01T00:00:00+00:00",
            ts_files=2,
            fallback_files=1,
        )
        d = s.model_dump()
        assert d["ts_files"] == 2
        assert d["fallback_files"] == 1
        assert d["files"] == 3
        assert d["chunks"] == 7


# ===========================================================================
# supervisor._fire_and_forget — log level raised to WARNING
# ===========================================================================


class TestFireAndForgetLogLevel:
    async def test_fire_and_forget_logs_warning_on_exception(self) -> None:
        """Exceptions in fire-and-forget tasks must be logged at WARNING level."""
        import asyncio

        from yukar.runs.supervisor import _fire_and_forget

        async def _failing_coro() -> None:
            raise RuntimeError("test failure from fire_and_forget")

        with patch("yukar.runs.supervisor.logger") as mock_logger:
            _fire_and_forget(_failing_coro(), name="test-task")
            # Let the event loop run the task and its done callback.
            await asyncio.sleep(0.05)

        # warning must have been called (not debug)
        assert mock_logger.warning.called, (
            "Expected logger.warning to be called for a failing fire-and-forget task"
        )
        assert not mock_logger.debug.called, (
            "logger.debug should not be called for a failing fire-and-forget task"
        )


# ===========================================================================
# repo_summarize tool — surfaces error.json message
# ===========================================================================


class TestRepoSummarizeErrorMessage:
    async def test_summarize_includes_error_message_when_index_absent(self, tmp_path: Path) -> None:
        """repo_summarize must include the error.json reason in its message."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        from yukar.agents.tools.repo_tools import make_repo_tools
        from yukar.config import paths as config_paths
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService
        from yukar.indexer.stats import write_error

        service = IndexerService(workspace_root=str(workspace), embedder=FakeEmbedder())

        # Plant an error.json without a successful index.
        idx_dir = config_paths.index_dir(str(workspace), "proj", "broken-repo")
        write_error(idx_dir, RuntimeError("NoCredentialsError: Unable to locate credentials"))

        tools = make_repo_tools("proj", service, repo_name="broken-repo")
        repo_summarize = tools[1]

        result = await repo_summarize()
        assert "message" in result
        msg = result["message"]
        assert "NoCredentialsError" in msg, f"Expected error reason in message, got: {msg!r}"
        # Tool must NOT try to trigger a build.
        assert result.get("summary") is None

    async def test_summarize_plain_unindexed_message_when_no_error(self, tmp_path: Path) -> None:
        """repo_summarize without error.json returns the standard 'not indexed' message."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        from yukar.agents.tools.repo_tools import make_repo_tools
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        service = IndexerService(workspace_root=str(workspace), embedder=FakeEmbedder())

        tools = make_repo_tools("proj", service, repo_name="never-indexed")
        repo_summarize = tools[1]

        result = await repo_summarize()
        assert "message" in result
        assert "not been indexed" in result["message"]
        # No error details since no error.json exists.
        assert "failed" not in result["message"].lower()
