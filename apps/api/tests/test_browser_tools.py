"""Browser tool bundle — end-to-end through the real host stack.

Each test drives the actual path an agent would take: tool call →
DevServerManager launches the user-declared service (python http.server on a
static fixture) inside a trial-shaped worktree → BrowserSession opens it in
headless Chromium behind the egress gate.
"""

from __future__ import annotations

import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from yukar.agents.context import AgentContext
from yukar.agents.tools.browser_tools import (
    make_browser_tools,
    make_browser_tools_if_configured,
)
from yukar.config import paths
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
    DevServerManager,
    TrialKey,
    init_dev_server_manager,
)
from yukar.storage.project_repo import save_project, save_repo

_INDEX_HTML = """<!DOCTYPE html>
<html><head><title>Fixture App</title></head>
<body>
  <h1>Hello Yukar</h1>
  <a href="/page2.html">Go to page 2</a>
  <form>
    <input type="text" placeholder="Search box">
    <button type="button">Do it</button>
  </form>
  <script>console.log('hello-from-page');</script>
</body></html>
"""

_PAGE2_HTML = (
    "<!DOCTYPE html><html><head><title>Page Two</title></head>"
    "<body><h1>Second</h1></body></html>"
)

_MID_HTML = (
    "<!DOCTYPE html><html><head><title>Mid</title><style>body{margin:0}</style></head>"
    '<body><div style="height:3000px;background:#00f"></div></body></html>'
)

_TALL_HTML = (
    # scroll-behavior:smooth is part of the fixture on purpose: plain
    # scrollTo would animate under it, letting the capture fire before the
    # page reaches the top — the prepare walk must use instant jumps.
    "<!DOCTYPE html><html><head><title>Tall</title>"
    "<style>body{margin:0}html{scroll-behavior:smooth}</style></head>"
    '<body><header style="position:fixed;top:0;left:0;right:0;height:100px;'
    'background:#f00"></header>'
    '<div style="height:20000px;background:#00f"></div></body></html>'
)


def _dev_server_config() -> DevServerConfig:
    return DevServerConfig(
        services=[
            DevService(
                name="web",
                command=[sys.executable, "-m", "http.server", "{port}", "--bind", "127.0.0.1"],
                base_port=43100,
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
async def ctx(tmp_path: Path) -> AgentContext:
    """Trial-shaped worktree (…/worktrees/t1/app) with fixture pages + repo YAML."""
    root = str(tmp_path / "workspace")
    worktree = tmp_path / "worktrees" / "t1" / "app"
    worktree.mkdir(parents=True)
    (worktree / "index.html").write_text(_INDEX_HTML)
    (worktree / "page2.html").write_text(_PAGE2_HTML)

    await save_project(root, Project(id="p", name="p", repos=["app"]))
    await save_repo(
        root,
        "p",
        Repo(name="app", path=str(worktree), dev_server=_dev_server_config()),
    )
    return AgentContext(
        project_id="p",
        epic_id="e1",
        repo_name="app",
        worktree_path=worktree,
        workspace_root=root,
    )


def _tools_by_name(tools: list[Any]) -> dict[str, Any]:
    return {t.tool_name: t for t in tools}


def _text_of(result: dict[str, Any]) -> str:
    return "\n".join(block.get("text", "") for block in result.get("content", []))


def _ref_of(snapshot_text: str, pattern: str) -> str:
    match = re.search(pattern + r".*?\[ref=(e\d+)\]", snapshot_text)
    assert match is not None, f"pattern {pattern!r} not found in:\n{snapshot_text}"
    return match.group(1)


class TestBundleGating:
    async def test_no_config_no_tools(self, tmp_path: Path, managers: Any) -> None:
        root = str(tmp_path / "ws2")
        worktree = tmp_path / "worktrees" / "t1" / "bare"
        worktree.mkdir(parents=True)
        await save_project(root, Project(id="p", name="p", repos=["bare"]))
        await save_repo(root, "p", Repo(name="bare", path=str(worktree)))
        ctx = AgentContext(
            project_id="p",
            epic_id="e1",
            repo_name="bare",
            worktree_path=worktree,
            workspace_root=root,
        )
        assert await make_browser_tools_if_configured(ctx, "worker-1") == []

    async def test_configured_repo_gets_bundle(self, ctx: AgentContext, managers: Any) -> None:
        tools = await make_browser_tools_if_configured(ctx, "worker-1")
        names = set(_tools_by_name(tools))
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

    def test_uninitialised_singletons_yield_no_tools(self, tmp_path: Path) -> None:
        # No `managers` fixture — singletons are absent in this process state.
        init_dev_server_manager(None)
        init_browser_session_manager(None)
        worktree = tmp_path / "worktrees" / "t1" / "app"
        worktree.mkdir(parents=True)
        ctx = AgentContext(
            project_id="p",
            epic_id="e1",
            repo_name="app",
            worktree_path=worktree,
            workspace_root=str(tmp_path),
        )
        assert make_browser_tools(ctx, "worker-1") == []


class TestBrowserFlow:
    async def test_open_read_click_type_screenshot(
        self, ctx: AgentContext, managers: Any
    ) -> None:
        tools = _tools_by_name(make_browser_tools(ctx, "worker-1"))

        opened = await tools["browser_open"]()
        assert opened["status"] == "success", _text_of(opened)
        text = _text_of(opened)
        assert "Fixture App" in text
        assert 'heading "Hello Yukar"' in text

        # Click through to page 2 via its snapshot ref.
        link_ref = _ref_of(text, r'link "Go to page 2"')
        clicked = await tools["browser_click"](ref=link_ref)
        assert clicked["status"] == "success", _text_of(clicked)

        read = await tools["browser_read"]()
        assert "Page Two" in _text_of(read)

        # Back to the form page; type into the search box.
        navigated = await tools["browser_navigate"](url="/index.html")
        assert navigated["status"] == "success", _text_of(navigated)
        box_ref = _ref_of(_text_of(navigated), r'textbox "Search box"')
        typed = await tools["browser_type"](ref=box_ref, text="hello")
        assert typed["status"] == "success", _text_of(typed)

        shot = await tools["browser_screenshot"]()
        assert shot["status"] == "success"
        image = shot["content"][1]["image"]
        assert image["format"] == "jpeg"
        assert image["source"]["bytes"][:2] == b"\xff\xd8"  # JPEG magic
        # Unsaved shots leave no file behind.
        shots_dir = paths.epic_screenshots_dir(ctx.workspace_root, ctx.project_id, ctx.epic_id)
        assert not shots_dir.exists()

        # save=True persists the same bytes under the epic docs folder.
        saved = await tools["browser_screenshot"](save=True, label="my-shot")
        assert saved["status"] == "success"
        assert "docs/screenshots/" in _text_of(saved)
        files = list(shots_dir.glob("*.jpg"))
        assert len(files) == 1
        assert "my-shot" in files[0].name
        assert files[0].read_bytes()[:2] == b"\xff\xd8"

        console = await tools["browser_console"]()
        assert "hello-from-page" in _text_of(console)

        logs = await tools["server_logs"]()
        assert logs["status"] == "success"

    async def test_stale_ref_reports_error(self, ctx: AgentContext, managers: Any) -> None:
        tools = _tools_by_name(make_browser_tools(ctx, "worker-1"))
        opened = await tools["browser_open"]()
        assert opened["status"] == "success", _text_of(opened)

        result = await tools["browser_click"](ref="e9999")
        assert result["status"] == "error"
        assert "stale" in _text_of(result)

    async def test_navigate_outside_origins_blocked(
        self, ctx: AgentContext, managers: Any
    ) -> None:
        tools = _tools_by_name(make_browser_tools(ctx, "worker-1"))
        opened = await tools["browser_open"]()
        assert opened["status"] == "success", _text_of(opened)

        result = await tools["browser_navigate"](url="https://attacker.example/?d=x")
        assert result["status"] == "error"
        assert "blocked" in _text_of(result).lower()

    async def test_tools_before_open_report_error(
        self, ctx: AgentContext, managers: Any
    ) -> None:
        tools = _tools_by_name(make_browser_tools(ctx, "worker-1"))
        result = await tools["browser_read"]()
        assert result["status"] == "error"
        assert "browser_open" in _text_of(result)

    async def test_unknown_service_rejected(self, ctx: AgentContext, managers: Any) -> None:
        tools = _tools_by_name(make_browser_tools(ctx, "worker-1"))
        result = await tools["browser_open"](service="ghost")
        assert result["status"] == "error"
        assert "Unknown service" in _text_of(result)

    async def test_server_stop_idempotent(self, ctx: AgentContext, managers: Any) -> None:
        dev, _sessions = managers
        tools = _tools_by_name(make_browser_tools(ctx, "worker-1"))
        opened = await tools["browser_open"]()
        assert opened["status"] == "success", _text_of(opened)

        stopped = await tools["server_stop"]()
        assert stopped["status"] == "success"
        assert "stopped" in _text_of(stopped)

        # Registry is empty for this trial and a second stop is a no-op.
        again = await tools["server_stop"]()
        assert again["status"] == "success"
        assert "No dev server" in _text_of(again)

        # Re-open works after an explicit stop.
        reopened = await tools["browser_open"]()
        assert reopened["status"] == "success", _text_of(reopened)


class TestFullPageScreenshot:
    """Long-page captures — the beyond-viewport render path that used to break."""

    async def test_full_page_capture_on_long_pages(
        self, ctx: AgentContext, managers: Any
    ) -> None:
        from io import BytesIO

        from PIL import Image

        from yukar.agents.tools.browser_core import _FULL_PAGE_MAX_PX
        from yukar.preview.browser import SessionKey

        _dev, sessions = managers
        (ctx.worktree_path / "mid.html").write_text(_MID_HTML)
        (ctx.worktree_path / "tall.html").write_text(_TALL_HTML)
        tools = _tools_by_name(make_browser_tools(ctx, "worker-1"))
        opened = await tools["browser_open"]()
        assert opened["status"] == "success", _text_of(opened)

        # Page under the cap: the whole 3000px document lands in one image.
        navigated = await tools["browser_navigate"](url="/mid.html")
        assert navigated["status"] == "success", _text_of(navigated)
        shot = await tools["browser_screenshot"](full_page=True)
        assert shot["status"] == "success", _text_of(shot)
        image = Image.open(BytesIO(shot["content"][1]["image"]["source"]["bytes"]))
        assert image.height == 3000
        assert "captured the top" not in _text_of(shot)

        # Page over the cap, scrolled mid-way beforehand: the capture starts
        # at the top of the document, is cut at the cap (and says so), the
        # fixed header paints at y=0 rather than at the scroll offset, and
        # the agent's scroll position survives the shot.
        navigated = await tools["browser_navigate"](url="/tall.html")
        assert navigated["status"] == "success", _text_of(navigated)
        session = sessions.get_open_session(
            SessionKey(project_id="p", epic_id="e1", trial_id="t1", owner_id="worker-1")
        )
        assert session is not None
        # instant: the fixture's smooth scrolling would otherwise still be
        # animating when the tool reads the position it must restore.
        await session.page.evaluate(
            'window.scrollTo({top: 3000, behavior: "instant"})'
        )

        shot = await tools["browser_screenshot"](full_page=True)
        assert shot["status"] == "success", _text_of(shot)
        assert f"captured the top {_FULL_PAGE_MAX_PX}px" in _text_of(shot)
        image = Image.open(BytesIO(shot["content"][1]["image"]["source"]["bytes"]))
        assert image.height == _FULL_PAGE_MAX_PX
        rgb = image.convert("RGB")
        header_pixel = rgb.getpixel((10, 10))
        assert isinstance(header_pixel, tuple)
        red_r, _g, red_b = header_pixel
        assert red_r > 150 and red_b < 100  # fixed header at the very top
        # Where the header would land if painted at the 3000px scroll offset.
        body_pixel = rgb.getpixel((10, 3050))
        assert isinstance(body_pixel, tuple)
        body_r, _g, body_b = body_pixel
        assert body_b > 150 and body_r < 100
        assert await session.page.evaluate("window.scrollY") == 3000


class TestCrossRepoFlow:
    async def test_open_launches_dependency_and_allows_its_origin(
        self, ctx: AgentContext, managers: Any, tmp_path: Path
    ) -> None:
        dev, _sessions = managers
        root = ctx.workspace_root

        backend_dir = tmp_path / "backend-repo"
        backend_dir.mkdir()
        (backend_dir / "index.html").write_text(
            "<!DOCTYPE html><html><head><title>Backend API</title></head>"
            "<body><h1>API root</h1></body></html>"
        )
        await save_repo(
            root,
            "p",
            Repo(
                name="backend",
                path=str(backend_dir),
                dev_server=DevServerConfig(
                    services=[
                        DevService(
                            name="api",
                            command=[
                                sys.executable,
                                "-m",
                                "http.server",
                                "{port}",
                                "--bind",
                                "127.0.0.1",
                            ],
                            base_port=43150,
                            readiness=ServiceReadiness(path="/", timeout_seconds=30),
                        )
                    ]
                ),
            ),
        )
        # Rewire the app's config to reference the backend repo's service —
        # this is what makes backend a launch dependency of app.
        config = _dev_server_config()
        config.services[0].env = {"BACKEND_URL": "http://127.0.0.1:{port:backend/api}"}
        await save_repo(
            root, "p", Repo(name="app", path=str(ctx.worktree_path), dev_server=config)
        )

        tools = _tools_by_name(make_browser_tools(ctx, "worker-1"))
        opened = await tools["browser_open"]()
        assert opened["status"] == "success", _text_of(opened)
        assert "Fixture App" in _text_of(opened)

        # Both repos' servers are up: app under the trial key, backend (no
        # worktree of its own) under the __base__ sentinel.
        app_key = TrialKey(project_id="p", epic_id="e1", trial_id="t1", repo_name="app")
        dep_key = TrialKey(
            project_id="p", epic_id="e1", trial_id="__base__", repo_name="backend"
        )
        assert dev.get_entry(app_key) is not None
        dep_entry = dev.get_entry(dep_key)
        assert dep_entry is not None

        # The dependency's origin joined the page's egress allow-set.
        backend_origin = dep_entry["api"].origin
        navigated = await tools["browser_navigate"](url=f"{backend_origin}/index.html")
        assert navigated["status"] == "success", _text_of(navigated)
        assert "Backend API" in _text_of(navigated)


class TestBlockedOriginVisibility:
    """Egress-gate rejections surface to the agent and the per-repo aggregate (§13)."""

    async def test_blocked_subresource_recorded_and_reported(
        self, ctx: AgentContext, managers: Any
    ) -> None:
        _dev, sessions = managers
        (ctx.worktree_path / "blocked.html").write_text(
            "<!DOCTYPE html><html><head><title>Blocked</title></head>"
            '<body><img src="https://blocked.example/x.png"></body></html>'
        )
        tools = _tools_by_name(make_browser_tools(ctx, "worker-1"))
        opened = await tools["browser_open"]()
        assert opened["status"] == "success", _text_of(opened)

        navigated = await tools["browser_navigate"](url="/blocked.html")
        assert navigated["status"] == "success", _text_of(navigated)
        text = _text_of(navigated)
        if "[egress]" not in text:
            # The aborted subresource can land just after the load event —
            # a follow-up read must carry the digest.
            text = _text_of(await tools["browser_read"]())
        assert "[egress]" in text
        assert "https://blocked.example" in text

        rows = sessions.blocked_origins("p")
        assert any(
            repo == "app" and stat.origin == "https://blocked.example" and stat.count >= 1
            for repo, stat in rows
        )
        # Unrelated project: nothing recorded.
        assert sessions.blocked_origins("other-project") == []

    def test_record_into_caps_distinct_origins(self) -> None:
        from yukar.preview.browser import _BLOCKED_ORIGIN_CAP, _record_into

        stats: dict[str, Any] = {}
        for i in range(_BLOCKED_ORIGIN_CAP + 10):
            _record_into(stats, f"https://origin-{i}.example/x", "img")
        assert len(stats) == _BLOCKED_ORIGIN_CAP
        # Existing origins keep counting even at the cap.
        _record_into(stats, "https://origin-0.example/y", "fetch")
        assert stats["https://origin-0.example"].count == 2
        assert stats["https://origin-0.example"].resource_types == {"img", "fetch"}
