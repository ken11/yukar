"""Shared test fixtures.

Provides:
  - tmp_workspace: an isolated workspace directory
  - fixture_git_repo: a pre-initialized git repo with commits and a branch
  - app_client: httpx AsyncClient backed by the FastAPI app
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def reset_fake_call_counts() -> Generator[None]:
    """Reset FakeModel per-role invocation counters before each test.

    The per-call script dispatch in ``FakeModel.from_env`` uses a module-level
    dict (``_role_invocation_counts``) to track how many times each role has
    been instantiated.  Without clearing it between tests the counters bleed
    across tests, causing per_call indices to advance unintentionally.
    """
    from yukar.llm.fake import reset_call_counts

    reset_call_counts()
    yield
    reset_call_counts()


@pytest.fixture(autouse=True)
def zero_fake_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set YUKAR_FAKE_SLEEP=0 so FakeModel text chunks are emitted instantly.

    This prevents the per-chunk asyncio.sleep in fake.py from adding latency
    to the test suite while still exercising the chunked-streaming code path.
    """
    monkeypatch.setenv("YUKAR_FAKE_SLEEP", "0")


@pytest.fixture(autouse=True)
def clear_event_bus_state() -> Generator[None]:
    """Clear event bus global state before each test.

    The replay buffer and subscriber queues are module-level globals in
    events/bus.py.  Without clearing them between tests, lifecycle events
    published in one test bleed into subsequent tests that share the same
    (project_id, epic_id) key, causing spurious replay-buffer hits that
    make collectors exit prematurely.
    """
    from yukar.events import bus as event_bus

    event_bus._queues.clear()
    event_bus._replay.clear()
    event_bus._project_queues.clear()
    event_bus._usage_queues.clear()
    event_bus._thread_token_buffer.clear()
    event_bus._thread_user_msg_buffer.clear()
    yield
    event_bus._queues.clear()
    event_bus._replay.clear()
    event_bus._project_queues.clear()
    event_bus._usage_queues.clear()
    event_bus._thread_token_buffer.clear()
    event_bus._thread_user_msg_buffer.clear()


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """Return an empty temporary workspace root."""
    ws = tmp_path / "yukar-projects"
    ws.mkdir()
    return ws


@pytest.fixture
def fixture_git_repo(tmp_path: Path) -> Path:
    """Return a path to a fresh git repo with:
    - an initial commit on 'main'
    - a feature branch 'yukar/ep-1-test-epic' with one commit
    - an uncommitted change
    """
    repo = tmp_path / "test-repo"
    repo.mkdir()

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True, env=env
        )
        assert result.returncode == 0, f"git {args} failed: {result.stderr}"
        return result.stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")

    # Initial commit
    (repo / "README.md").write_text("# test repo\n")
    git("add", ".")
    git("commit", "-m", "initial commit")

    # Feature branch
    git("checkout", "-b", "yukar/ep-1-test-epic")
    (repo / "feature.py").write_text("# feature\nprint('hello')\n")
    git("add", ".")
    git("commit", "-m", "add feature")

    # Back to main, then uncommitted change
    git("checkout", "main")
    (repo / "work_in_progress.txt").write_text("WIP\n")

    return repo


@pytest.fixture
def yukar_config_dir(tmp_path: Path) -> Generator[Path]:
    """Return a temp config dir and set YUKAR_CONFIG_DIR."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    os.environ["YUKAR_CONFIG_DIR"] = str(cfg)
    yield cfg
    del os.environ["YUKAR_CONFIG_DIR"]


@pytest_asyncio.fixture
async def app_client(tmp_workspace: Path, yukar_config_dir: Path) -> AsyncGenerator[AsyncClient]:
    """Return an async httpx client pointed at the test FastAPI app."""
    from yukar.app import create_app
    from yukar.config import paths as config_paths
    from yukar.config.settings import LLMSettings, Settings
    from yukar.indexer.embedder import FakeEmbedder
    from yukar.indexer.service import IndexerService
    from yukar.runs.supervisor import init_supervisor
    from yukar.usage.tracker import TokenUsageTracker, init_tracker

    app = create_app()
    # Override settings to use tmp workspace and fake LLM (no real credentials needed).
    settings = Settings(workspace_root=str(tmp_workspace))
    settings.llm = LLMSettings(provider="fake")
    # Disable watcher in tests to avoid background tasks.
    settings.indexer.watch = False
    app.state.settings = settings

    # Provide a FakeEmbedder-backed IndexerService so deps.get_indexer_service works.
    indexer_service = IndexerService(
        workspace_root=str(tmp_workspace),
        embedder=FakeEmbedder(),
    )
    app.state.indexer_service = indexer_service
    app.state.watcher = None

    init_supervisor(
        max_parallel_epics=settings.agent.max_parallel_epics,
        settings_getter=lambda: app.state.settings,
        indexer_service=indexer_service,
    )

    # Initialise usage tracker (no exchange provider in tests → fallback rate).
    tracker = TokenUsageTracker(
        ledger_path=config_paths.ledger_yaml(str(tmp_workspace)),
    )
    app.state.usage_tracker = tracker
    app.state.exchange_rate_provider = None
    init_tracker(tracker)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
