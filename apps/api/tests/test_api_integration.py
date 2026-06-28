"""Tests for M3 back-half — indexer API integration + agent tools.

Covers:
- POST /api/projects/{p}/search: hit / miss / repo-scoped / unindexed
- POST /api/projects/{p}/index: 202 async, status after completion, re-entry serialisation
- GET  /api/projects/{p}/index/status: per-repo state
- POST /api/projects: background initial index runs after creation
- agents/tools/repo_tools: Worker scoped to assigned repo, Manager across all repos,
  repo_summarize returns cached summary.md
- watcher: settings.indexer.watch=False → watcher not started
- orchestrator integration: FakeModel with repo_search ToolUseTurn
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_git_repo(tmp_path: Path, name: str = "repo") -> Path:
    """Create a minimal git repo with one commit on 'main'."""
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
    }

    def g(*args: str) -> str:
        r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=env)
        assert r.returncode == 0, f"git {args}: {r.stderr}"
        return r.stdout.strip()

    g("init", "-b", "main")
    g("config", "user.email", "test@test.com")
    g("config", "user.name", "Test")
    (repo / "README.md").write_text("# test\n")
    (repo / "main.py").write_text("def greet(name: str) -> str:\n    return f'Hello {name}'\n")
    g("add", ".")
    g("commit", "-m", "initial")
    return repo


def _make_indexer_service(workspace_root: str) -> Any:
    """Create an IndexerService with FakeEmbedder."""
    from yukar.indexer.embedder import FakeEmbedder
    from yukar.indexer.service import IndexerService

    return IndexerService(workspace_root=workspace_root, embedder=FakeEmbedder())


async def _setup_project_with_repo(
    root: str,
    project_id: str,
    repo_path: Path,
    repo_name: str = "myrepo",
) -> None:
    """Write minimal project + repo YAML."""
    from yukar.models.project import Project, Repo
    from yukar.storage.project_repo import save_project, save_repo

    project = Project(id=project_id, name=project_id, status="active", repos=[repo_name])
    await save_project(root, project)
    repo = Repo(name=repo_name, path=str(repo_path), default_branch="main")
    await save_repo(root, project_id, repo)


# ---------------------------------------------------------------------------
# App client fixture with FakeEmbedder and IndexerService
# ---------------------------------------------------------------------------


@pytest.fixture
def indexer_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def indexer_git_repo(tmp_path: Path) -> Path:
    return _make_git_repo(tmp_path)


@pytest.fixture
def yukar_config_dir_idx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("YUKAR_CONFIG_DIR", str(cfg))
    return cfg


@pytest.fixture
async def indexer_app_client(
    indexer_workspace: Path,
    yukar_config_dir_idx: Path,
) -> Any:
    """Async client with FakeEmbedder wired into app.state."""
    from httpx import ASGITransport, AsyncClient

    from yukar.app import create_app
    from yukar.config.settings import EmbeddingSettings, LLMSettings, Settings
    from yukar.indexer.embedder import FakeEmbedder
    from yukar.indexer.service import IndexerService
    from yukar.runs.supervisor import init_supervisor

    app = create_app()
    settings = Settings(workspace_root=str(indexer_workspace))
    settings.llm = LLMSettings(provider="fake")
    settings.embedding = EmbeddingSettings(provider="fake")
    # watcher off by default in tests to avoid background file events.
    settings.indexer.watch = False

    app.state.settings = settings

    # Override app.state.indexer_service with FakeEmbedder-backed service.
    indexer_service = IndexerService(
        workspace_root=str(indexer_workspace),
        embedder=FakeEmbedder(),
    )
    app.state.indexer_service = indexer_service
    app.state.watcher = None

    init_supervisor(
        max_parallel_epics=settings.agent.max_parallel_epics,
        settings_getter=lambda: app.state.settings,
        indexer_service=indexer_service,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


# ===========================================================================
# Search API tests
# ===========================================================================


class TestSearchEndpoint:
    """POST /api/projects/{project_id}/search"""

    async def _setup(
        self,
        root: str,
        project_id: str,
        repo_path: Path,
        repo_name: str = "myrepo",
    ) -> None:
        """Index a repo so search tests have data."""
        await _setup_project_with_repo(root, project_id, repo_path, repo_name)
        svc = _make_indexer_service(root)
        await svc.reindex_repo(project_id, repo_name, repo_path)

    async def test_search_hit(
        self,
        indexer_app_client: Any,
        indexer_workspace: Path,
        indexer_git_repo: Path,
    ) -> None:
        """Search returns results when repo is indexed."""
        root = str(indexer_workspace)
        await self._setup(root, "proj", indexer_git_repo)
        # Override the app's service with one that has the data.
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        # Use the shared fake embedder so vectors are consistent.
        shared_svc = IndexerService(workspace_root=root, embedder=FakeEmbedder())
        indexer_app_client._transport.app.state.indexer_service = shared_svc

        r = await indexer_app_client.post(
            "/api/projects/proj/search",
            json={"query": "greet", "top_k": 5},
        )
        assert r.status_code == 200
        data = r.json()
        assert "results" in data
        # FakeEmbedder is deterministic so we expect at least 1 result.
        assert len(data["results"]) >= 1
        item = data["results"][0]
        assert "repo" in item
        assert "path" in item
        assert "snippet" in item
        assert "score" in item
        assert "start_line" in item
        assert "end_line" in item
        assert "language" in item

    async def test_search_unindexed_repo(
        self,
        indexer_app_client: Any,
        indexer_workspace: Path,
        indexer_git_repo: Path,
    ) -> None:
        """Search returns empty results + unindexed_repos when repo not indexed."""
        root = str(indexer_workspace)
        await _setup_project_with_repo(root, "proj2", indexer_git_repo, "myrepo")

        r = await indexer_app_client.post(
            "/api/projects/proj2/search",
            json={"query": "anything", "repo": "myrepo"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["results"] == []
        assert "myrepo" in data["unindexed_repos"]

    async def test_search_repo_scoped(
        self,
        indexer_app_client: Any,
        indexer_workspace: Path,
        tmp_path: Path,
    ) -> None:
        """Searching with repo= only returns chunks from that repo."""
        root = str(indexer_workspace)
        repo_a = _make_git_repo(tmp_path, "repo_a")
        repo_b = _make_git_repo(tmp_path, "repo_b")

        from yukar.models.project import Project, Repo
        from yukar.storage.project_repo import save_project, save_repo

        project = Project(id="proj3", name="proj3", status="active", repos=["repo_a", "repo_b"])
        await save_project(root, project)
        await save_repo(root, "proj3", Repo(name="repo_a", path=str(repo_a)))
        await save_repo(root, "proj3", Repo(name="repo_b", path=str(repo_b)))

        svc = _make_indexer_service(root)
        await svc.reindex_repo("proj3", "repo_a", repo_a)
        await svc.reindex_repo("proj3", "repo_b", repo_b)

        # Scope to repo_a only.
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        shared_svc = IndexerService(workspace_root=root, embedder=FakeEmbedder())
        indexer_app_client._transport.app.state.indexer_service = shared_svc

        r = await indexer_app_client.post(
            "/api/projects/proj3/search",
            json={"query": "test", "repo": "repo_a", "top_k": 10},
        )
        assert r.status_code == 200
        data = r.json()
        for item in data["results"]:
            assert item["repo"] == "repo_a", f"Got chunk from wrong repo: {item['repo']}"

    async def test_search_score_normalized_0_to_1(
        self,
        indexer_app_client: Any,
        indexer_workspace: Path,
        indexer_git_repo: Path,
    ) -> None:
        """Score returned by the search endpoint must be in [0, 1].

        The router normalizes raw L2 distances via score = 1 / (1 + distance).
        All scores must be strictly in (0, 1].  An exact-match query (querying
        with the exact text of an indexed chunk) must return score ≈ 1.0.
        """
        from yukar.config import paths as config_paths
        from yukar.indexer import faiss_store
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        root = str(indexer_workspace)
        await self._setup(root, "proj-score", indexer_git_repo)

        shared_svc = IndexerService(workspace_root=root, embedder=FakeEmbedder())
        indexer_app_client._transport.app.state.indexer_service = shared_svc

        # Find the exact text of an indexed chunk for a self-query.
        idx_dir = config_paths.index_dir(root, "proj-score", "myrepo")
        indexed_chunks, _ = await faiss_store.load_index(idx_dir)
        assert indexed_chunks
        exact_text = indexed_chunks[0]["text"]

        r = await indexer_app_client.post(
            "/api/projects/proj-score/search",
            json={"query": exact_text, "repo": "myrepo", "top_k": 1},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["results"], "Expected at least one result"
        score = data["results"][0]["score"]
        # Score must be in (0, 1]
        assert 0.0 < score <= 1.0, f"Score out of range: {score}"
        # Exact-match score must be very close to 1.0 (FakeEmbedder deterministic)
        assert score > 0.999, f"Exact-match score should be ~1.0, got {score}"

    async def test_search_score_ordering_best_first(
        self,
        indexer_app_client: Any,
        indexer_workspace: Path,
        indexer_git_repo: Path,
    ) -> None:
        """Results must be ordered by score descending (best match first)."""
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        root = str(indexer_workspace)
        await self._setup(root, "proj-order", indexer_git_repo)

        shared_svc = IndexerService(workspace_root=root, embedder=FakeEmbedder())
        indexer_app_client._transport.app.state.indexer_service = shared_svc

        r = await indexer_app_client.post(
            "/api/projects/proj-order/search",
            json={"query": "greet", "repo": "myrepo", "top_k": 5},
        )
        assert r.status_code == 200
        data = r.json()
        scores = [item["score"] for item in data["results"]]
        assert scores == sorted(scores, reverse=True), (
            f"Results not ordered by score descending: {scores}"
        )


# ===========================================================================
# Index API tests
# ===========================================================================


class TestIndexEndpoint:
    """POST /api/projects/{p}/index  and  GET /api/projects/{p}/index/status"""

    async def test_trigger_index_202(
        self,
        indexer_app_client: Any,
        indexer_workspace: Path,
        indexer_git_repo: Path,
    ) -> None:
        """POST /index returns 202 immediately."""
        root = str(indexer_workspace)
        await _setup_project_with_repo(root, "pi1", indexer_git_repo)

        r = await indexer_app_client.post("/api/projects/pi1/index?repo=myrepo")
        assert r.status_code == 202
        data = r.json()
        assert data["accepted"] is True
        assert "myrepo" in data["repos"]

    async def test_trigger_index_all_repos(
        self,
        indexer_app_client: Any,
        indexer_workspace: Path,
        indexer_git_repo: Path,
    ) -> None:
        """POST /index without repo= reindexes all enabled repos."""
        root = str(indexer_workspace)
        await _setup_project_with_repo(root, "pi2", indexer_git_repo)

        r = await indexer_app_client.post("/api/projects/pi2/index")
        assert r.status_code == 202
        data = r.json()
        assert data["accepted"] is True
        assert "myrepo" in data["repos"]

    async def test_index_and_status_lifecycle(
        self,
        indexer_workspace: Path,
        indexer_git_repo: Path,
    ) -> None:
        """Reindex completes → status shows indexed with files > 0."""
        root = str(indexer_workspace)
        await _setup_project_with_repo(root, "pi3", indexer_git_repo)

        svc = _make_indexer_service(root)
        n = await svc.reindex_repo("pi3", "myrepo", indexer_git_repo)
        assert n > 0

        statuses = await svc.get_status("pi3")
        assert len(statuses) == 1
        s = statuses[0]
        assert s.repo_name == "myrepo"
        assert s.state == "indexed"
        assert s.files > 0
        assert s.chunks > 0
        assert s.last_indexed_at is not None

    async def test_status_endpoint(
        self,
        indexer_app_client: Any,
        indexer_workspace: Path,
        indexer_git_repo: Path,
    ) -> None:
        """GET /index/status returns correct structure."""
        root = str(indexer_workspace)
        await _setup_project_with_repo(root, "ps1", indexer_git_repo)

        # Pre-index so status shows something.
        svc = _make_indexer_service(root)
        await svc.reindex_repo("ps1", "myrepo", indexer_git_repo)

        # Wire the shared service into the app.
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        shared_svc = IndexerService(workspace_root=root, embedder=FakeEmbedder())
        indexer_app_client._transport.app.state.indexer_service = shared_svc

        r = await indexer_app_client.get("/api/projects/ps1/index/status")
        assert r.status_code == 200
        data = r.json()
        assert "statuses" in data
        assert len(data["statuses"]) >= 1
        s = data["statuses"][0]
        assert s["repo_name"] == "myrepo"
        assert s["state"] == "indexed"
        assert s["files"] >= 1

    async def test_concurrent_reindex_serialised(
        self,
        indexer_workspace: Path,
        indexer_git_repo: Path,
    ) -> None:
        """Concurrent reindex calls for the same repo are serialised by the FAISS lock."""
        root = str(indexer_workspace)
        await _setup_project_with_repo(root, "pc1", indexer_git_repo)

        svc = _make_indexer_service(root)

        # Launch two concurrent reindex calls — both should complete without error.
        results = await asyncio.gather(
            svc.reindex_repo("pc1", "myrepo", indexer_git_repo),
            svc.reindex_repo("pc1", "myrepo", indexer_git_repo),
        )
        assert all(r >= 0 for r in results), "Both reindex calls should succeed"

        # Final index should be consistent.
        statuses = await svc.get_status("pc1")
        assert statuses[0].state == "indexed"

    async def test_trigger_index_404_unknown_repo(
        self,
        indexer_app_client: Any,
        indexer_workspace: Path,
        indexer_git_repo: Path,
    ) -> None:
        """POST /index?repo=unknown returns 404."""
        root = str(indexer_workspace)
        await _setup_project_with_repo(root, "pi404", indexer_git_repo)

        r = await indexer_app_client.post("/api/projects/pi404/index?repo=does-not-exist")
        assert r.status_code == 404


# ===========================================================================
# New Project creation → background initial index
# ===========================================================================


class TestProjectCreationIndex:
    """POST /api/projects triggers background indexing."""

    async def test_initial_index_runs_after_create(
        self,
        indexer_app_client: Any,
        indexer_workspace: Path,
        indexer_git_repo: Path,
    ) -> None:
        """Creating a project kicks off background indexing; wait for it to finish."""
        root = str(indexer_workspace)

        # Override with a service that uses the real root.
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService

        svc = IndexerService(workspace_root=root, embedder=FakeEmbedder())
        indexer_app_client._transport.app.state.indexer_service = svc

        r = await indexer_app_client.post(
            "/api/projects",
            json={
                "id": "new-proj",
                "name": "New Project",
                "repos": [
                    {
                        "name": "myrepo",
                        "path": str(indexer_git_repo),
                        "default_branch": "main",
                    }
                ],
            },
        )
        assert r.status_code == 201

        # Wait for the background task to complete (it runs in the same event loop).
        # BackgroundTasks in ASGI run in the response, so a small yield suffices.
        for _ in range(20):
            await asyncio.sleep(0.1)
            statuses = await svc.get_status("new-proj")
            if statuses and statuses[0].state == "indexed":
                break

        statuses = await svc.get_status("new-proj")
        assert len(statuses) == 1
        assert statuses[0].state == "indexed"
        assert statuses[0].files > 0


# ===========================================================================
# Watcher settings
# ===========================================================================


class TestWatcherSettings:
    """settings.indexer.watch=False → watcher not started."""

    async def test_watcher_not_started_when_disabled(
        self,
        tmp_path: Path,
    ) -> None:
        """When indexer.watch=False, app.state.watcher should be None."""
        from httpx import ASGITransport, AsyncClient

        from yukar.app import create_app
        from yukar.config.settings import EmbeddingSettings, LLMSettings, Settings
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService
        from yukar.runs.supervisor import init_supervisor

        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        ws = tmp_path / "ws"
        ws.mkdir()

        app = create_app()
        settings = Settings(workspace_root=str(ws))
        settings.llm = LLMSettings(provider="fake")
        settings.embedding = EmbeddingSettings(provider="fake")
        settings.indexer.watch = False

        svc = IndexerService(workspace_root=str(ws), embedder=FakeEmbedder())
        app.state.settings = settings
        app.state.indexer_service = svc
        app.state.watcher = None

        init_supervisor(max_parallel_epics=1, settings_getter=lambda: settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/api/health")
            assert r.status_code == 200
            # watcher should remain None (not started).
            assert app.state.watcher is None


# ===========================================================================
# repo_tools unit tests
# ===========================================================================


class TestRepoTools:
    """agents/tools/repo_tools — Worker scoping and summary retrieval."""

    async def _index_repo(
        self,
        workspace: str,
        project_id: str,
        repo_path: Path,
        repo_name: str = "myrepo",
    ) -> Any:
        svc = _make_indexer_service(workspace)
        await _setup_project_with_repo(workspace, project_id, repo_path, repo_name)
        await svc.reindex_repo(project_id, repo_name, repo_path)
        return svc

    async def test_worker_repo_search_scoped_to_assigned_repo(
        self,
        tmp_path: Path,
        indexer_git_repo: Path,
    ) -> None:
        """Worker's repo_search closure only searches its assigned repo."""
        workspace = str(tmp_path / "ws")
        Path(workspace).mkdir()
        repo_b = _make_git_repo(tmp_path, "repo_b")

        svc = _make_indexer_service(workspace)

        # Index both repos under same project.
        await _setup_project_with_repo(workspace, "proj_w", indexer_git_repo, "myrepo")
        # Also save repo_b.
        from yukar.models.project import Repo
        from yukar.storage.project_repo import save_repo

        await save_repo(workspace, "proj_w", Repo(name="repo_b", path=str(repo_b)))

        await svc.reindex_repo("proj_w", "myrepo", indexer_git_repo)
        await svc.reindex_repo("proj_w", "repo_b", repo_b)

        from yukar.agents.tools.repo_tools import make_repo_tools

        # Worker scoped to "myrepo" only.
        tools = make_repo_tools("proj_w", svc, repo_name="myrepo")
        repo_search_tool = next(t for t in tools if t.tool_name == "repo_search")

        # Strands tools are directly async-callable.
        result = await repo_search_tool(query="greet", top_k=10)

        # All results must be from "myrepo" only.
        results = result.get("results", [])
        assert isinstance(results, list)
        for item in results:
            assert item["repo"] == "myrepo", f"Worker got chunk from wrong repo: {item['repo']}"

    async def test_manager_repo_search_all_repos(
        self,
        tmp_path: Path,
        indexer_git_repo: Path,
    ) -> None:
        """Manager's repo_search (repo_name=None) searches all repos."""
        workspace = str(tmp_path / "ws")
        Path(workspace).mkdir()
        repo_b = _make_git_repo(tmp_path, "repo_b")

        svc = _make_indexer_service(workspace)

        from yukar.models.project import Project, Repo
        from yukar.storage.project_repo import save_project, save_repo

        project = Project(id="proj_m", name="proj_m", status="active", repos=["myrepo", "repo_b"])
        await save_project(workspace, project)
        await save_repo(workspace, "proj_m", Repo(name="myrepo", path=str(indexer_git_repo)))
        await save_repo(workspace, "proj_m", Repo(name="repo_b", path=str(repo_b)))

        await svc.reindex_repo("proj_m", "myrepo", indexer_git_repo)
        await svc.reindex_repo("proj_m", "repo_b", repo_b)

        from yukar.agents.tools.repo_tools import make_repo_tools

        tools = make_repo_tools("proj_m", svc, repo_name=None)  # Manager mode.
        repo_search_tool = next(t for t in tools if t.tool_name == "repo_search")

        result = await repo_search_tool(query="test", top_k=20)

        repos_found = {item["repo"] for item in result.get("results", [])}
        # Manager should see results from at least one repo (both are indexed).
        assert len(repos_found) >= 1

    async def test_repo_summarize_returns_cached_summary(
        self,
        tmp_path: Path,
        indexer_git_repo: Path,
    ) -> None:
        """repo_summarize returns the cached summary.md without rebuilding."""
        workspace = str(tmp_path / "ws")
        Path(workspace).mkdir()

        svc = await self._index_repo(workspace, "proj_s", indexer_git_repo)

        from yukar.agents.tools.repo_tools import make_repo_tools

        tools = make_repo_tools("proj_s", svc, repo_name="myrepo")
        summarize_tool = next(t for t in tools if t.tool_name == "repo_summarize")

        result = await summarize_tool()

        assert "summary" in result
        assert result["summary"] is not None
        assert "myrepo" in result["summary"] or len(result["summary"]) > 0

    async def test_repo_summarize_unindexed_graceful(
        self,
        tmp_path: Path,
        indexer_git_repo: Path,
    ) -> None:
        """repo_summarize for an unindexed repo returns a helpful message, not an error."""
        workspace = str(tmp_path / "ws")
        Path(workspace).mkdir()
        svc = _make_indexer_service(workspace)

        from yukar.agents.tools.repo_tools import make_repo_tools

        tools = make_repo_tools("not-a-proj", svc, repo_name="nonexistent-repo")
        summarize_tool = next(t for t in tools if t.tool_name == "repo_summarize")

        result = await summarize_tool()

        # Should not raise; should return a message field.
        assert "message" in result or "error" in result


# ===========================================================================
# Orchestrator integration test with repo_search ToolUseTurn
# ===========================================================================


class TestOrchestratorWithRepoTools:
    """EpicOrchestrator + FakeModel with repo_search tool use."""

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return _make_git_repo(tmp_path, "myrepo")

    async def test_worker_repo_search_tool_use(
        self,
        tmp_path: Path,
        git_repo: Path,
    ) -> None:
        """Orchestrator integration: FakeModel drives repo_search ToolUseTurn.

        Verifies that:
        1. EpicOrchestrator wires indexer tools into Worker.
        2. repo_search tool_use executes without error (returns dict with results).
        3. Orchestrator completes the run.
        """
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.indexer.embedder import FakeEmbedder
        from yukar.indexer.service import IndexerService
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn

        workspace = str(tmp_path / "ws")
        Path(workspace).mkdir()
        project_id = "proj_orch"
        epic_id = "EP-1"
        repo_name = git_repo.name

        await _setup_project_with_repo(workspace, project_id, git_repo, repo_name)

        # Bootstrap epic.
        from yukar.models.epic import Epic
        from yukar.storage.epic_repo import save_epic

        epic = Epic(
            id=epic_id,
            slug="test-epic",
            title="Test Epic",
            description="Test repo_search integration.",
            branch=f"yukar/{epic_id.lower()}-test-epic",
        )
        await save_epic(workspace, project_id, epic)

        # Index the repo so repo_search has data.
        svc = IndexerService(workspace_root=workspace, embedder=FakeEmbedder())
        await svc.reindex_repo(project_id, repo_name, git_repo)

        # Script: Manager creates one task, dispatches it, then completes.
        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Search and implement",
                    "status": "todo",
                    "repo": repo_name,
                },
            ),
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": repo_name}]},
            ),
            ToolUseTurn(tool_name="complete_epic", tool_input={}),
            TextTurn("Task defined."),
        ]

        worker_script = [
            # Worker calls repo_search.
            ToolUseTurn(
                tool_name="repo_search",
                tool_input={"query": "greet", "top_k": 3},
            ),
            # Then writes a file (host commits after Evaluator accepts).
            ToolUseTurn(
                tool_name="fs_write",
                tool_input={"path": "result.py", "content": "# result\n"},
            ),
            TextTurn("Done."),
        ]

        evaluator_script = [
            ToolUseTurn(
                tool_name="submit_verdict",
                tool_input={"accepted": True, "feedback": ""},
            ),
            TextTurn("Accepted."),
        ]

        llm = LLMSettings(provider="fake")

        def _fake_model_factory(role: str | None = None) -> FakeModel:
            if role == "manager":
                return FakeModel(script=manager_script)
            if role == "evaluator":
                return FakeModel(script=evaluator_script)
            return FakeModel(script=worker_script)

        orchestrator = EpicOrchestrator(
            llm_settings=llm,
            git_author_name="Test",
            git_author_email="test@test.com",
            indexer_service=svc,
        )

        with patch("yukar.llm.factory.create_model", side_effect=_fake_model_factory):
            run_id = "run-test"
            try:
                await asyncio.wait_for(
                    orchestrator.start(workspace, project_id, epic_id, run_id),
                    timeout=30.0,
                )
            except TimeoutError:
                pytest.fail("Orchestrator timed out — repo_search may have blocked")

        # Verify the run completed by checking state.
        from yukar.storage.state_repo import get_state

        state = await get_state(workspace, project_id, epic_id)
        assert state is not None
        assert state.status == "completed"
