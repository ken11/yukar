"""Agent-facing visibility of command permissions and index state.

Three UX failures are pinned here:

1. Allowlist discovery by trial-and-error: the permitted-command set must be
   visible UP FRONT — in the ``run_command`` / ``run_tests`` tool descriptions
   — not only in the rejection message of a failed call.
2. ``repo_search`` silent empty: a missing index must return an explicit
   ``message`` (semantic search unavailable), never a bare empty result that
   teaches agents the tool is useless.
3. An indexed-but-no-hits search must also say so, steering the agent instead
   of leaving it to grind grep patterns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from yukar.agents.context import AgentContext
from yukar.agents.tools.command import describe_command_permissions, make_command_tools
from yukar.agents.tools.evaluator_tools import make_evaluator_tools
from yukar.agents.tools.overview_tools import make_overview_ro_tools
from yukar.agents.tools.repo_tools import make_repo_tools


async def _make_ctx(worktree: Path, allow: list[str], deny: list[str] | None = None) -> Any:
    return await AgentContext.create(
        project_id="proj",
        epic_id="EP-vis",
        repo_name="repo",
        worktree_path=worktree,
        workspace_root=str(worktree.parent),
        allow=allow,
        deny=deny or [],
    )


class TestDescribeCommandPermissions:
    def test_empty_allow_says_nothing_is_permitted(self) -> None:
        text = describe_command_permissions((), ())
        assert "NO shell commands are permitted" in text
        assert "Do not attempt any command" in text

    def test_lists_allow_entries(self) -> None:
        text = describe_command_permissions(("pytest", "pnpm test"), ())
        assert "  - pytest" in text
        assert "  - pnpm test" in text
        assert "do NOT trial-and-error" in text

    def test_lists_deny_entries_when_present(self) -> None:
        text = describe_command_permissions(("pnpm",), ("pnpm publish",))
        assert "Explicitly denied:" in text
        assert "  - pnpm publish" in text

    def test_omits_deny_section_when_empty(self) -> None:
        text = describe_command_permissions(("pytest",), ())
        assert "Explicitly denied:" not in text


class TestRunCommandDescriptionCarriesAllowlist:
    async def test_allow_entries_visible_in_tool_spec(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await _make_ctx(wt, allow=["pytest", "uv run ruff check"])
        (run_command,) = make_command_tools(ctx)

        desc = run_command.tool_spec["description"]
        assert "  - pytest" in desc
        assert "  - uv run ruff check" in desc

    async def test_empty_allowlist_visible_in_tool_spec(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await _make_ctx(wt, allow=[])
        (run_command,) = make_command_tools(ctx)

        desc = run_command.tool_spec["description"]
        assert "NO shell commands are permitted" in desc

    async def test_evaluator_run_tests_description_carries_allowlist(
        self, tmp_path: Path
    ) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        ctx = await _make_ctx(wt, allow=["pytest"])
        tools = make_evaluator_tools(ctx)
        run_tests = next(t for t in tools if t.tool_name == "run_tests")

        desc = run_tests.tool_spec["description"]
        assert "  - pytest" in desc

    async def test_overview_run_tests_description_carries_per_repo_notes(self) -> None:
        async def _resolve(_repo: str) -> Any:
            return None

        tools = make_overview_ro_tools(
            ["api", "web"],
            _resolve,
            include_run_tests=True,
            command_notes={
                "api": describe_command_permissions(("pytest",), ()),
                "web": describe_command_permissions((), ()),
            },
        )
        run_tests = next(t for t in tools if t.tool_name == "run_tests")

        desc = run_tests.tool_spec["description"]
        assert "### api" in desc
        assert "  - pytest" in desc
        assert "### web" in desc
        assert "NO shell commands are permitted" in desc


class _StubIndexerService:
    """Minimal indexer stand-in: the not-indexed check must short-circuit
    BEFORE any embedding/search work happens."""

    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = workspace_root

    async def search(self, *args: Any, **kwargs: Any) -> list[Any]:
        raise AssertionError("search() must not be called when the index is missing")


class TestRepoSearchNotIndexedIsExplicit:
    async def test_worker_mode_returns_message_not_bare_empty(self, tmp_path: Path) -> None:
        svc = _StubIndexerService(str(tmp_path))
        tools = make_repo_tools("proj", svc, repo_name="myrepo")
        repo_search = next(t for t in tools if t.tool_name == "repo_search")

        result = await repo_search(query="anything")
        assert result["results"] == []
        assert "has not been indexed yet" in result["message"]
        assert "repo_grep" in result["message"]

    async def test_manager_mode_all_unindexed_returns_message(self, tmp_path: Path) -> None:
        from yukar.models.project import Project, Repo
        from yukar.storage.project_repo import save_project, save_repo

        workspace = str(tmp_path / "ws")
        Path(workspace).mkdir()
        repo_dir = tmp_path / "r1"
        repo_dir.mkdir()
        await save_project(
            workspace, Project(id="proj", name="proj", status="active", repos=["r1"])
        )
        await save_repo(workspace, "proj", Repo(name="r1", path=str(repo_dir)))

        svc = _StubIndexerService(workspace)
        tools = make_repo_tools("proj", svc, repo_name=None)
        repo_search = next(t for t in tools if t.tool_name == "repo_search")

        result = await repo_search(query="anything")
        assert result["results"] == []
        assert "has been indexed yet" in result["message"]

    async def test_indexed_but_no_hits_returns_steering_message(self, tmp_path: Path) -> None:
        from yukar.config import paths as config_paths

        class _EmptyResultService(_StubIndexerService):
            async def search(self, *args: Any, **kwargs: Any) -> list[Any]:
                return []

        # Fake an existing index (index_exists checks these two files).
        idx_dir = config_paths.index_dir(str(tmp_path), "proj", "myrepo")
        idx_dir.mkdir(parents=True)
        (idx_dir / "faiss.index").write_bytes(b"")
        (idx_dir / "chunks.jsonl").write_text("")

        svc = _EmptyResultService(str(tmp_path))
        tools = make_repo_tools("proj", svc, repo_name="myrepo")
        repo_search = next(t for t in tools if t.tool_name == "repo_search")

        result = await repo_search(query="anything")
        assert result["results"] == []
        assert "No semantically similar code found" in result["message"]
