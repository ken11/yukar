"""Browser overview bundle (Manager/Reviewer) — end-to-end through the host stack.

Mirrors test_browser_tools.py, but through the repo-dispatching wrappers:
every tool takes a required ``repo`` argument and resolves its target PER CALL
— the active trial's worktree when it exists, the repo's base checkout before
any work does (keyed under the ``__base__`` sentinel so the two never share a
DevServerManager entry).
"""

from __future__ import annotations

import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from yukar.agents.tools.browser_overview_tools import make_browser_overview_tools
from yukar.config import paths
from yukar.models.epic import Epic
from yukar.models.project import (
    DevServerConfig,
    DevService,
    Project,
    Repo,
    ServiceReadiness,
)
from yukar.preview.browser import (
    BrowserSessionManager,
    init_browser_session_manager,
)
from yukar.preview.manager import (
    DevServerError,
    DevServerManager,
    TrialKey,
    init_dev_server_manager,
)
from yukar.storage.project_repo import save_project, save_repo

_BASE_HTML = """<!DOCTYPE html>
<html><head><title>Base App</title></head>
<body>
  <h1>Base checkout</h1>
  <a href="/page2.html">Go to page 2</a>
  <input type="text" placeholder="Search box">
</body></html>
"""

_PAGE2_HTML = (
    "<!DOCTYPE html><html><head><title>Page Two</title></head>"
    "<body><h1>Second</h1></body></html>"
)

_BRANCH_HTML = (
    "<!DOCTYPE html><html><head><title>Branch App</title></head>"
    "<body><h1>Branch worktree</h1></body></html>"
)

_SHOP_HTML = (
    "<!DOCTYPE html><html><head><title>Shop App</title></head>"
    "<body><h1>Second repo</h1></body></html>"
)


def _dev_server_config() -> DevServerConfig:
    return DevServerConfig(
        services=[
            DevService(
                name="web",
                command=[sys.executable, "-m", "http.server", "{port}", "--bind", "127.0.0.1"],
                base_port=43200,
                readiness=ServiceReadiness(path="/", timeout_seconds=30),
            )
        ]
    )


@pytest.fixture
async def managers() -> AsyncIterator[tuple[DevServerManager, BrowserSessionManager]]:
    """Install fresh process singletons; tear everything down after the test."""
    dev = DevServerManager()
    sessions = BrowserSessionManager()
    init_dev_server_manager(dev)
    init_browser_session_manager(sessions)
    try:
        yield dev, sessions
    finally:
        await sessions.close_all()
        await dev.stop_all()
        init_dev_server_manager(None)
        init_browser_session_manager(None)


@pytest.fixture
async def workspace(tmp_path: Path) -> tuple[str, Epic, Path]:
    """Project "p" / epic "e1" whose repo "app" has a base checkout but no worktree yet."""
    root = str(tmp_path / "workspace")
    base = tmp_path / "checkouts" / "app"
    base.mkdir(parents=True)
    (base / "index.html").write_text(_BASE_HTML)
    (base / "page2.html").write_text(_PAGE2_HTML)

    await save_project(root, Project(id="p", name="p", repos=["app"]))
    await save_repo(
        root, "p", Repo(name="app", path=str(base), dev_server=_dev_server_config())
    )
    # active_thread_id with no registered ThreadEntry resolves to itself ("t1").
    epic = Epic(id="e1", slug="e1", title="e1", active_thread_id="t1")
    return root, epic, base


async def _bundle(root: str, epic: Epic) -> dict[str, Any]:
    tools = await make_browser_overview_tools(root, "p", "e1", epic, owner_id="manager-1")
    return {t.tool_name: t for t in tools}


def _text_of(result: dict[str, Any]) -> str:
    return "\n".join(block.get("text", "") for block in result.get("content", []))


def _ref_of(snapshot_text: str, pattern: str) -> str:
    match = re.search(pattern + r".*?\[ref=(e\d+)\]", snapshot_text)
    assert match is not None, f"pattern {pattern!r} not found in:\n{snapshot_text}"
    return match.group(1)


_BASE_KEY = TrialKey(project_id="p", epic_id="e1", trial_id="__base__", repo_name="app")
_TRIAL_KEY = TrialKey(project_id="p", epic_id="e1", trial_id="t1", repo_name="app")


class TestBundleGating:
    async def test_no_dev_server_config_no_tools(self, tmp_path: Path, managers: Any) -> None:
        root = str(tmp_path / "ws2")
        base = tmp_path / "checkouts" / "bare"
        base.mkdir(parents=True)
        await save_project(root, Project(id="p", name="p", repos=["bare"]))
        await save_repo(root, "p", Repo(name="bare", path=str(base)))
        epic = Epic(id="e1", slug="e1", title="e1", active_thread_id="t1")
        assert await make_browser_overview_tools(root, "p", "e1", epic, owner_id="m") == []

    async def test_uninitialised_singletons_yield_no_tools(
        self, workspace: tuple[str, Epic, Path]
    ) -> None:
        # No `managers` fixture — make sure the singletons are absent.
        init_dev_server_manager(None)
        init_browser_session_manager(None)
        root, epic, _base = workspace
        assert await make_browser_overview_tools(root, "p", "e1", epic, owner_id="m") == []

    async def test_configured_repo_gets_bundle(
        self, workspace: tuple[str, Epic, Path], managers: Any
    ) -> None:
        root, epic, _base = workspace
        names = set(await _bundle(root, epic))
        assert {
            "browser_open",
            "browser_navigate",
            "browser_read",
            "browser_click",
            "browser_type",
            "browser_press",
            "browser_screenshot",
            "browser_console",
            "server_logs",
            "server_stop",
        } <= names


class TestRepoDispatch:
    async def test_repo_argument_required(
        self, workspace: tuple[str, Epic, Path], managers: Any
    ) -> None:
        root, epic, _base = workspace
        tools = await _bundle(root, epic)
        result = await tools["browser_open"]()
        assert result["status"] == "error"
        text = _text_of(result)
        assert "`repo` is required" in text
        assert "app" in text

    async def test_unknown_repo_rejected(
        self, workspace: tuple[str, Epic, Path], managers: Any
    ) -> None:
        root, epic, _base = workspace
        tools = await _bundle(root, epic)
        result = await tools["browser_read"](repo="ghost")
        assert result["status"] == "error"
        assert "app" in _text_of(result)


class TestTargetResolution:
    async def test_base_checkout_before_worktree(
        self, workspace: tuple[str, Epic, Path], managers: Any
    ) -> None:
        """No worktree yet → the base checkout is served, under the sentinel key."""
        root, epic, _base = workspace
        dev, _sessions = managers
        tools = await _bundle(root, epic)

        opened = await tools["browser_open"](repo="app")
        assert opened["status"] == "success", _text_of(opened)
        text = _text_of(opened)
        assert "Base App" in text

        assert dev.get_entry(_BASE_KEY) is not None
        assert dev.get_entry(_TRIAL_KEY) is None

        # Interactions dispatch through the same per-call target.
        link_ref = _ref_of(text, r'link "Go to page 2"')
        clicked = await tools["browser_click"](ref=link_ref, repo="app")
        assert clicked["status"] == "success", _text_of(clicked)
        read = await tools["browser_read"](repo="app")
        assert "Page Two" in _text_of(read)

    async def test_switches_to_worktree_when_it_appears(
        self, workspace: tuple[str, Epic, Path], managers: Any
    ) -> None:
        """A worktree created mid-run wins on the next call — no stale-tree reuse."""
        root, epic, _base = workspace
        dev, _sessions = managers
        tools = await _bundle(root, epic)

        opened = await tools["browser_open"](repo="app")
        assert opened["status"] == "success", _text_of(opened)
        assert "Base App" in _text_of(opened)

        # First dispatch creates the trial worktree while the run is going.
        worktree = paths.worktree_dir(root, "p", "e1", "t1", "app")
        worktree.mkdir(parents=True)
        (worktree / "index.html").write_text(_BRANCH_HTML)

        # The target flips to the trial key, whose session does not exist yet —
        # the agent recovers by re-calling browser_open.
        stale = await tools["browser_read"](repo="app")
        assert stale["status"] == "error"
        assert "browser_open" in _text_of(stale)

        reopened = await tools["browser_open"](repo="app")
        assert reopened["status"] == "success", _text_of(reopened)
        assert "Branch App" in _text_of(reopened)

        # Both entries exist independently: the base server was NOT reused for
        # the worktree (ensure() reuses healthy entries by key), it just
        # lingers until the run-end stop_for_epic sweep.
        assert dev.get_entry(_TRIAL_KEY) is not None
        assert dev.get_entry(_BASE_KEY) is not None

        # server_stop resolves to the worktree target but also sweeps the
        # leftover base entry — one call frees everything for the repo.
        stopped = await tools["server_stop"](repo="app")
        assert stopped["status"] == "success"
        assert dev.get_entry(_TRIAL_KEY) is None
        assert dev.get_entry(_BASE_KEY) is None

    async def test_pages_are_isolated_per_repo(
        self, workspace: tuple[str, Epic, Path], managers: Any
    ) -> None:
        """Each repo gets its own page and allow-set (SessionKey has no repo axis,
        so the bundle folds the repo into the owner id) — reading repo A must
        never return repo B's DOM."""
        root, epic, _base = workspace
        shop = _base.parent / "shop"
        shop.mkdir(parents=True)
        (shop / "index.html").write_text(_SHOP_HTML)
        await save_repo(
            root, "p", Repo(name="shop", path=str(shop), dev_server=_dev_server_config())
        )
        tools = await _bundle(root, epic)

        opened_app = await tools["browser_open"](repo="app")
        assert opened_app["status"] == "success", _text_of(opened_app)
        opened_shop = await tools["browser_open"](repo="shop")
        assert opened_shop["status"] == "success", _text_of(opened_shop)

        # Opening "shop" must not have navigated "app"'s page away.
        read_app = await tools["browser_read"](repo="app")
        assert "Base App" in _text_of(read_app)
        assert "Shop App" not in _text_of(read_app)
        read_shop = await tools["browser_read"](repo="shop")
        assert "Shop App" in _text_of(read_shop)

        # Stopping one repo leaves the other's page and servers untouched.
        stopped = await tools["server_stop"](repo="shop")
        assert stopped["status"] == "success"
        read_app_again = await tools["browser_read"](repo="app")
        assert "Base App" in _text_of(read_app_again)

    async def test_server_stop_stops_current_target(
        self, workspace: tuple[str, Epic, Path], managers: Any
    ) -> None:
        root, epic, _base = workspace
        dev, _sessions = managers
        tools = await _bundle(root, epic)

        opened = await tools["browser_open"](repo="app")
        assert opened["status"] == "success", _text_of(opened)

        stopped = await tools["server_stop"](repo="app")
        assert stopped["status"] == "success"
        assert dev.get_entry(_BASE_KEY) is None

        again = await tools["server_stop"](repo="app")
        assert again["status"] == "success"
        assert "No dev server" in _text_of(again)


class TestEagerWorktreeCreation:
    """With an ``ensure_tree`` callback wired, browser_open on a repo with no
    worktree yet CREATES the trial worktree instead of launching in the shared
    base checkout (avoids the Next.js duplicate-instance collision + build
    artifacts leaking into the user's checkout)."""

    async def test_creates_trial_worktree_instead_of_base(
        self, workspace: tuple[str, Epic, Path], managers: Any
    ) -> None:
        root, epic, _base = workspace
        dev, _sessions = managers

        async def _ensure(repo: str) -> Path:
            wt = paths.worktree_dir(root, "p", "e1", "t1", repo)
            wt.mkdir(parents=True, exist_ok=True)
            (wt / "index.html").write_text(_BRANCH_HTML)
            return wt

        tools = {
            t.tool_name: t
            for t in await make_browser_overview_tools(
                root, "p", "e1", epic, owner_id="manager-1", ensure_tree=_ensure
            )
        }

        opened = await tools["browser_open"](repo="app")
        assert opened["status"] == "success", _text_of(opened)
        # Served the freshly-created worktree, NOT the base checkout.
        assert "Branch App" in _text_of(opened)
        assert dev.get_entry(_TRIAL_KEY) is not None
        assert dev.get_entry(_BASE_KEY) is None

    async def test_ensure_tree_failure_surfaces_as_tool_error(
        self, workspace: tuple[str, Epic, Path], managers: Any
    ) -> None:
        root, epic, _base = workspace
        dev, _sessions = managers

        async def _ensure(repo: str) -> Path:
            raise DevServerError("no active trial owns a worktree")

        tools = {
            t.tool_name: t
            for t in await make_browser_overview_tools(
                root, "p", "e1", epic, owner_id="manager-1", ensure_tree=_ensure
            )
        }

        opened = await tools["browser_open"](repo="app")
        assert opened["status"] == "error"
        assert "no active trial" in _text_of(opened)
        # Nothing launched — neither the worktree nor the base fallback.
        assert dev.get_entry(_TRIAL_KEY) is None
        assert dev.get_entry(_BASE_KEY) is None
