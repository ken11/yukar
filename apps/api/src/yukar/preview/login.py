"""User-interactive login capture — headed browser + storage_state (design §12).

The host opens a HEADED Chromium on the machine running yukar serve, the USER
logs in themselves (form login, email OTP, an external IdP — anything a human
can complete), and on finish the context's ``storage_state`` (cookies +
localStorage) is saved next to the repo's YAML.  Agent browser contexts for
that repo are then created from this state, so agents start logged in without
ever seeing a credential.

This browser is deliberately UNGATED: it is driven by the user, not an agent,
and a login flow legitimately needs the IdP.  It is a separate Chromium
process from the agents' egress-gated one (preview/browser.py).

Dev servers for the capture run in the repo's BASE checkout under the
synthetic key ``(project, "__login__", "__base__", repo)`` — no epic or run is
involved — and are stopped when the capture ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from yukar.config import paths
from yukar.models.project import Repo
from yukar.preview.browser import get_browser_session_manager
from yukar.preview.manager import DevServerError, TrialKey, get_dev_server_manager

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)

LOGIN_EPIC_ID = "__login__"
LOGIN_TRIAL_ID = "__base__"

# Test/CI hook: a headed browser cannot open without a display, so the test
# suite sets this to "1" and drives the same flow headless.
_HEADLESS_ENV = "YUKAR_LOGIN_BROWSER_HEADLESS"


class LoginCaptureError(RuntimeError):
    """The capture could not start, finish, or was in the wrong state."""


@dataclass
class LoginCapture:
    """One in-flight user login session (headed browser + its dev servers)."""

    project_id: str
    repo_name: str
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    url: str
    started_at: float


class LoginCaptureManager:
    """At most one interactive login capture per (project, repo)."""

    def __init__(self) -> None:
        self._captures: dict[tuple[str, str], LoginCapture] = {}
        # Per-(project, repo) locks: start() holds its lock across dev-server
        # readiness + Chromium launch (tens of seconds), which must not block
        # cancel/finish/start for OTHER repos.
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        # Strong refs to disconnect-reaper tasks (a bare create_task can be GC'd).
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    def _lock_for(self, project_id: str, repo_name: str) -> asyncio.Lock:
        return self._locks.setdefault((project_id, repo_name), asyncio.Lock())

    def _trial_key(self, project_id: str, repo_name: str) -> TrialKey:
        return TrialKey(
            project_id=project_id,
            epic_id=LOGIN_EPIC_ID,
            trial_id=LOGIN_TRIAL_ID,
            repo_name=repo_name,
        )

    def is_active(self, project_id: str, repo_name: str) -> bool:
        capture = self._captures.get((project_id, repo_name))
        return capture is not None and capture.browser.is_connected()

    async def start(self, project_id: str, repo: Repo) -> LoginCapture:
        """Launch the repo's dev servers (base checkout) and open a headed browser.

        Raises:
            LoginCaptureError: no dev-server config, capture already active,
                or the services failed to launch.
        """
        config = repo.dev_server
        if config is None:
            raise LoginCaptureError(f"Repo {repo.name!r} has no dev-server config.")
        dev = get_dev_server_manager()
        if dev is None:
            raise LoginCaptureError("Dev server manager is not running.")

        async with self._lock_for(project_id, repo.name):
            existing = self._captures.get((project_id, repo.name))
            if existing is not None:
                if existing.browser.is_connected():
                    raise LoginCaptureError("A login capture is already in progress.")
                # Window was closed without finish/cancel — tear the husk down.
                self._captures.pop((project_id, repo.name), None)
                await self._teardown(existing)

            base = Path(repo.path)
            key = self._trial_key(project_id, repo.name)
            try:
                entry = await dev.ensure(key, config, base, repo_root=base)
            except DevServerError as exc:
                raise LoginCaptureError(f"Dev server failed to start: {exc}") from exc
            url = entry[config.services[0].name].origin

            from playwright.async_api import async_playwright

            headless = os.environ.get(_HEADLESS_ENV) == "1"
            playwright = await async_playwright().start()
            try:
                browser = await playwright.chromium.launch(headless=headless)
                context = await browser.new_context()
                page = await context.new_page()
            except Exception:
                with contextlib.suppress(Exception):
                    await playwright.stop()
                await dev.stop(key)
                raise
            # Best-effort: even if the first load fails the window stays open
            # and the user can navigate/retry themselves.
            with contextlib.suppress(Exception):
                await page.goto(url)

            capture = LoginCapture(
                project_id=project_id,
                repo_name=repo.name,
                playwright=playwright,
                browser=browser,
                context=context,
                page=page,
                url=url,
                started_at=time.time(),
            )
            self._captures[(project_id, repo.name)] = capture

            # If the user simply closes the window (no finish/cancel), reap the
            # capture so its dev servers don't run as orphans until the next
            # start.  Fires on our own browser.close() too — the reaper then
            # finds the capture gone (or replaced) and no-ops.
            def _on_disconnected(_browser: object = None) -> None:
                # Playwright dispatches listeners on the event loop, so a
                # running loop is guaranteed here.
                task = asyncio.get_running_loop().create_task(
                    self._reap_disconnected(project_id, repo.name, browser)
                )
                self._cleanup_tasks.add(task)
                task.add_done_callback(self._cleanup_tasks.discard)

            browser.on("disconnected", _on_disconnected)
            return capture

    async def _reap_disconnected(
        self, project_id: str, repo_name: str, browser: Browser
    ) -> None:
        async with self._lock_for(project_id, repo_name):
            capture = self._captures.get((project_id, repo_name))
            if capture is None or capture.browser is not browser:
                return  # finished / cancelled / replaced by a newer capture
            self._captures.pop((project_id, repo_name), None)
            logger.info(
                "Login capture window closed without saving for %s/%s — reaping",
                project_id,
                repo_name,
            )
            await self._teardown(capture)

    async def finish(self, root: str, project_id: str, repo_name: str) -> Path:
        """Save storage_state, close everything, and invalidate agent sessions.

        Raises:
            LoginCaptureError: no capture is active, or the user closed the
                window before saving.
        """
        async with self._lock_for(project_id, repo_name):
            capture = self._captures.pop((project_id, repo_name), None)
            if capture is None:
                raise LoginCaptureError("No login capture is in progress.")
            if not capture.browser.is_connected():
                await self._teardown(capture)
                raise LoginCaptureError(
                    "The login window was closed before saving — start again."
                )
            state_path = paths.browser_auth_state(root, project_id, repo_name)
            await asyncio.to_thread(state_path.parent.mkdir, parents=True, exist_ok=True)
            try:
                await capture.context.storage_state(path=str(state_path))
                # Session tokens — owner-only, not umask-dependent.
                await asyncio.to_thread(os.chmod, state_path, 0o600)
            except Exception as exc:
                # The window can close between the is_connected() check and the
                # save — a user action, not a server fault (409, not 500).
                raise LoginCaptureError(
                    "The login window closed before the session could be saved "
                    "— start again."
                ) from exc
            finally:
                await self._teardown(capture)

        # Existing agent sessions for the repo still hold the OLD state —
        # close them so the next browser_open rebuilds from the new file
        # (the "call browser_open first" recovery path agents already know).
        sessions = get_browser_session_manager()
        if sessions is not None:
            await sessions.close_for_repo(project_id, repo_name)
        return state_path

    async def cancel(self, project_id: str, repo_name: str) -> bool:
        """Close the capture without saving.  Idempotent; True when one existed."""
        async with self._lock_for(project_id, repo_name):
            capture = self._captures.pop((project_id, repo_name), None)
            if capture is None:
                return False
            await self._teardown(capture)
            return True

    async def _teardown(self, capture: LoginCapture) -> None:
        """Close browser + playwright and stop the capture's dev servers."""
        with contextlib.suppress(Exception):
            await capture.browser.close()
        with contextlib.suppress(Exception):
            await capture.playwright.stop()
        dev = get_dev_server_manager()
        if dev is not None:
            with contextlib.suppress(Exception):
                await dev.stop(self._trial_key(capture.project_id, capture.repo_name))

    async def stop_all(self) -> None:
        """Lifespan teardown — abandon any in-flight captures without saving."""
        for project_id, repo_name in list(self._captures):
            await self.cancel(project_id, repo_name)


# ---------------------------------------------------------------------------
# Module-level singleton (init in app lifespan — mirrors preview.manager)
# ---------------------------------------------------------------------------

_login_manager: LoginCaptureManager | None = None


def init_login_capture_manager(manager: LoginCaptureManager | None) -> None:
    """Install (or clear, with None) the process-wide LoginCaptureManager."""
    global _login_manager  # noqa: PLW0603
    _login_manager = manager


def get_login_capture_manager() -> LoginCaptureManager | None:
    """Return the process-wide login manager, or None outside a running app."""
    return _login_manager
