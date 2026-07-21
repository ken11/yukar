"""Interactive login capture + storage_state injection (design §12).

The capture flow runs the REAL stack — dev server (python http.server on a
fixture checkout) and a Chromium the test drives headless via the
YUKAR_LOGIN_BROWSER_HEADLESS hook (a headed window cannot open in CI).
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from yukar.agents.context import AgentContext
from yukar.agents.tools.browser_tools import make_browser_tools
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
    SessionKey,
    init_browser_session_manager,
)
from yukar.preview.login import LoginCaptureError, LoginCaptureManager
from yukar.preview.manager import (
    DevServerManager,
    TrialKey,
    init_dev_server_manager,
)
from yukar.storage.project_repo import save_project, save_repo

_INDEX_HTML = (
    "<!DOCTYPE html><html><head><title>Login Fixture</title></head>"
    "<body><h1>App</h1></body></html>"
)

_LOGIN_KEY = TrialKey(
    project_id="p", epic_id="__login__", trial_id="__base__", repo_name="app"
)


def _dev_server_config(base_port: int) -> DevServerConfig:
    return DevServerConfig(
        services=[
            DevService(
                name="web",
                command=[sys.executable, "-m", "http.server", "{port}", "--bind", "127.0.0.1"],
                base_port=base_port,
                readiness=ServiceReadiness(path="/", timeout_seconds=30),
            )
        ]
    )


@pytest.fixture
async def managers() -> AsyncIterator[tuple[DevServerManager, BrowserSessionManager]]:
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
async def workspace(tmp_path: Path) -> tuple[str, Repo]:
    """Project "p" with repo "app" whose base checkout serves the fixture page."""
    root = str(tmp_path / "workspace")
    base = tmp_path / "checkouts" / "app"
    base.mkdir(parents=True)
    (base / "index.html").write_text(_INDEX_HTML)
    repo = Repo(name="app", path=str(base), dev_server=_dev_server_config(43300))
    await save_project(root, Project(id="p", name="p", repos=["app"]))
    await save_repo(root, "p", repo)
    return root, repo


@pytest.fixture
def headless_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YUKAR_LOGIN_BROWSER_HEADLESS", "1")


def _write_auth_state(root: str, cookie_value: str) -> Path:
    state_path = paths.browser_auth_state(root, "p", "app")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "yukar_auth",
                        "value": cookie_value,
                        "domain": "localhost",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": False,
                        "secure": False,
                        "sameSite": "Lax",
                    }
                ],
                "origins": [],
            }
        )
    )
    return state_path


class TestStorageStateInjection:
    async def test_agent_context_starts_from_captured_state(
        self, workspace: tuple[str, Repo], managers: Any, tmp_path: Path
    ) -> None:
        root, _repo = workspace
        _write_auth_state(root, "captured-token")

        worktree = tmp_path / "worktrees" / "t1" / "app"
        worktree.mkdir(parents=True)
        (worktree / "index.html").write_text(_INDEX_HTML)
        ctx = AgentContext(
            project_id="p",
            epic_id="e1",
            repo_name="app",
            worktree_path=worktree,
            workspace_root=root,
        )
        tools = {t.tool_name: t for t in make_browser_tools(ctx, "worker-1")}
        opened = await tools["browser_open"]()
        assert opened["status"] == "success"

        _dev, sessions = managers
        session = sessions.get_open_session(
            SessionKey(project_id="p", epic_id="e1", trial_id="t1", owner_id="worker-1")
        )
        assert session is not None
        cookies = await session.context.cookies()
        assert any(c["name"] == "yukar_auth" and c["value"] == "captured-token" for c in cookies)

    async def test_corrupt_state_falls_back_to_clean_context(
        self, workspace: tuple[str, Repo], managers: Any, tmp_path: Path
    ) -> None:
        root, _repo = workspace
        state_path = paths.browser_auth_state(root, "p", "app")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{not json")

        worktree = tmp_path / "worktrees" / "t1" / "app"
        worktree.mkdir(parents=True)
        (worktree / "index.html").write_text(_INDEX_HTML)
        ctx = AgentContext(
            project_id="p",
            epic_id="e1",
            repo_name="app",
            worktree_path=worktree,
            workspace_root=root,
        )
        tools = {t.tool_name: t for t in make_browser_tools(ctx, "worker-1")}
        opened = await tools["browser_open"]()
        assert opened["status"] == "success"


class TestLoginCaptureFlow:
    async def test_start_finish_saves_state_and_stops_servers(
        self, workspace: tuple[str, Repo], managers: Any, headless_login: None
    ) -> None:
        root, repo = workspace
        dev, _sessions = managers
        login = LoginCaptureManager()

        capture = await login.start("p", repo)
        # The capture window opens on localhost — the same host browser_open
        # navigates, so the recorded cookies apply there.
        assert capture.url.startswith("http://localhost:")
        assert dev.get_entry(_LOGIN_KEY) is not None
        assert login.is_active("p", "app")

        # Simulate the user's login: the page sets a cookie.
        await capture.context.add_cookies(
            [
                {
                    "name": "session",
                    "value": "user-logged-in",
                    "url": capture.url,
                }
            ]
        )
        state_path = await login.finish(root, "p", "app")
        assert state_path.is_file()
        # Session tokens: owner-only permissions, not umask-dependent.
        assert (state_path.stat().st_mode & 0o777) == 0o600
        state = json.loads(state_path.read_text())
        assert any(c["name"] == "session" for c in state["cookies"])
        assert dev.get_entry(_LOGIN_KEY) is None
        assert not login.is_active("p", "app")

    async def test_cancel_discards_without_saving(
        self, workspace: tuple[str, Repo], managers: Any, headless_login: None
    ) -> None:
        root, repo = workspace
        dev, _sessions = managers
        login = LoginCaptureManager()

        await login.start("p", repo)
        assert await login.cancel("p", "app") is True
        assert not paths.browser_auth_state(root, "p", "app").exists()
        assert dev.get_entry(_LOGIN_KEY) is None
        # Idempotent.
        assert await login.cancel("p", "app") is False

    async def test_window_close_reaps_dev_servers(
        self, workspace: tuple[str, Repo], managers: Any, headless_login: None
    ) -> None:
        """Closing the window by hand (no finish/cancel) must not orphan the
        capture's dev servers until the next start."""
        _root, repo = workspace
        dev, _sessions = managers
        login = LoginCaptureManager()

        capture = await login.start("p", repo)
        assert dev.get_entry(_LOGIN_KEY) is not None

        await capture.browser.close()
        for _ in range(50):
            if dev.get_entry(_LOGIN_KEY) is None:
                break
            await asyncio.sleep(0.1)
        assert dev.get_entry(_LOGIN_KEY) is None
        assert not login.is_active("p", "app")

    async def test_double_start_rejected_and_finish_without_start(
        self, workspace: tuple[str, Repo], managers: Any, headless_login: None
    ) -> None:
        root, repo = workspace
        login = LoginCaptureManager()

        with pytest.raises(LoginCaptureError, match="No login capture"):
            await login.finish(root, "p", "app")

        await login.start("p", repo)
        try:
            with pytest.raises(LoginCaptureError, match="already in progress"):
                await login.start("p", repo)
        finally:
            await login.cancel("p", "app")

    async def test_repo_delete_cancels_capture_and_removes_auth(
        self, workspace: tuple[str, Repo], managers: Any, headless_login: None
    ) -> None:
        """remove_project_repo must cancel an in-flight capture (else its headed
        browser + __login__ dev servers orphan, unreachable from REST) and delete
        the saved auth file (else a re-registered same-name repo inherits it)."""
        from yukar.api.routers.project_settings import remove_project_repo
        from yukar.preview.login import init_login_capture_manager

        root, repo = workspace
        dev, _sessions = managers
        _write_auth_state(root, "stale-token")
        login = LoginCaptureManager()
        init_login_capture_manager(login)
        try:
            await login.start("p", repo)
            assert login.is_active("p", "app")

            await remove_project_repo("p", "app", root)

            assert not login.is_active("p", "app")
            assert dev.get_entry(_LOGIN_KEY) is None
            assert not paths.browser_auth_state(root, "p", "app").exists()
        finally:
            init_login_capture_manager(None)

    async def test_finish_invalidates_existing_agent_sessions(
        self, workspace: tuple[str, Repo], managers: Any, headless_login: None, tmp_path: Path
    ) -> None:
        """After a capture, agent sessions for the repo are closed so the next
        browser_open rebuilds from the new state."""
        root, repo = workspace
        _dev, sessions = managers

        worktree = tmp_path / "worktrees" / "t1" / "app"
        worktree.mkdir(parents=True)
        (worktree / "index.html").write_text(_INDEX_HTML)
        ctx = AgentContext(
            project_id="p",
            epic_id="e1",
            repo_name="app",
            worktree_path=worktree,
            workspace_root=root,
        )
        tools = {t.tool_name: t for t in make_browser_tools(ctx, "worker-1")}
        opened = await tools["browser_open"]()
        assert opened["status"] == "success"
        key = SessionKey(project_id="p", epic_id="e1", trial_id="t1", owner_id="worker-1")
        assert sessions.get_open_session(key) is not None

        login = LoginCaptureManager()
        await login.start("p", repo)
        await login.finish(root, "p", "app")

        assert sessions.get_open_session(key) is None
        # Recovery path: re-open works and now carries the captured state.
        reopened = await tools["browser_open"]()
        assert reopened["status"] == "success"
