"""Browser verification core — role-agnostic operations behind the tool bundles.

Every operation takes an explicit :class:`BrowserTarget` (which repo, which
trial worktree, which agent's page) instead of an ``AgentContext`` closure, so
role-specific bundles stay thin wrappers:

- Worker/Evaluator (phase 1): ``browser_tools.make_browser_tools`` fixes the
  target from the agent's AgentContext (single assigned repo).
- Manager/Reviewer (phase 2): a future bundle resolves ``repo`` from a tool
  argument plus the active trial, then calls the same cores — mirroring how
  ``overview_tools`` wraps the fs/grep cores with a ``repo`` parameter.

The cores return the ``{status, content}`` tool-result dicts directly (via
response_builder) so wrappers only add the LLM-facing docstrings.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yukar.agents.tools.response_builder import make_error, make_success
from yukar.models.project import DevServerConfig
from yukar.preview.browser import (
    BrowserSession,
    SessionKey,
    get_browser_session_manager,
    normalize_origin,
)
from yukar.preview.manager import DevServerError, TrialKey, get_dev_server_manager
from yukar.storage.project_repo import get_repo

_ACTION_TIMEOUT_MS = 10_000
_GOTO_TIMEOUT_MS = 30_000
_SCREENSHOT_JPEG_QUALITY = 70

_NOT_AVAILABLE = "Browser verification is not available in this process."
_NOT_OPEN = "No dev server is running for this trial. Call browser_open first."


@dataclass(frozen=True, slots=True)
class BrowserTarget:
    """Everything a browser operation needs to know about *where* it acts.

    ``owner_id`` keys the agent's own page (parallel agents never share
    navigation state); the remaining fields locate the trial worktree whose
    declared services are launched and gated.
    """

    workspace_root: str
    project_id: str
    epic_id: str
    trial_id: str
    repo_name: str
    worktree_path: Path
    owner_id: str

    @property
    def trial_key(self) -> TrialKey:
        return TrialKey(
            project_id=self.project_id,
            epic_id=self.epic_id,
            trial_id=self.trial_id,
            repo_name=self.repo_name,
        )

    @property
    def session_key(self) -> SessionKey:
        return SessionKey(
            project_id=self.project_id,
            epic_id=self.epic_id,
            trial_id=self.trial_id,
            owner_id=self.owner_id,
        )


async def load_dev_server_config(
    workspace_root: str, project_id: str, repo_name: str
) -> DevServerConfig | None:
    """The repo's declared dev_server config, or None when absent/unknown repo."""
    repo = await get_repo(workspace_root, project_id, repo_name)
    return repo.dev_server if repo is not None else None


async def _session_or_error(
    target: BrowserTarget,
) -> tuple[BrowserSession | None, dict[str, Any] | None]:
    """Current open session for this agent — error when browser_open never ran.

    Uses get_open_session (not session()) so a tool other than browser_open
    never lazily creates a session with an empty egress allow-set, which would
    block even the trial's own origin.  The allow-set is populated only by
    open_app; every other tool must reuse the session it created.
    """
    sessions = get_browser_session_manager()
    manager = get_dev_server_manager()
    if sessions is None or manager is None:
        return None, make_error(_NOT_AVAILABLE)
    if manager.get_entry(target.trial_key) is None:
        return None, make_error(_NOT_OPEN)
    session = sessions.get_open_session(target.session_key)
    if session is None:
        return None, make_error(
            "This agent has no open browser page for the trial. Call browser_open first."
        )
    return session, None


async def _page_summary(session: BrowserSession) -> str:
    return f"url: {session.page.url}\ntitle: {await session.page.title()}"


async def open_app(target: BrowserTarget, service: str | None = None) -> dict[str, Any]:
    """Ensure the declared services and open one of them (default: first)."""
    manager = get_dev_server_manager()
    sessions = get_browser_session_manager()
    if manager is None or sessions is None:
        return make_error(_NOT_AVAILABLE)

    config = await load_dev_server_config(
        target.workspace_root, target.project_id, target.repo_name
    )
    if config is None:
        return make_error(
            f"Repo {target.repo_name!r} has no dev-server config. "
            "Ask the user to configure it in the project's repo settings."
        )
    service_names = [s.name for s in config.services]
    chosen = service or service_names[0]
    if chosen not in service_names:
        return make_error(f"Unknown service {chosen!r}. Declared: {service_names}")

    try:
        entry = await manager.ensure(target.trial_key, config, target.worktree_path)
    except DevServerError as exc:
        return make_error(f"Dev server failed to start: {exc}")

    session = await sessions.session(target.session_key)
    session.allow(
        [*manager.origins(target.trial_key), *config.browser.allowed_origins],
        cdn_preset=config.browser.allow_common_cdns,
    )

    url = entry[chosen].origin
    try:
        await session.page.goto(url, timeout=_GOTO_TIMEOUT_MS)
    except Exception as exc:
        return make_error(
            f"Navigation to {url} failed: {exc}\n"
            f"--- server log tail ---\n{manager.log_tail(target.trial_key, max_lines=40)}"
        )
    return make_success(f"{await _page_summary(session)}\n\n{await session.snapshot()}")


async def navigate(target: BrowserTarget, url: str) -> dict[str, Any]:
    """Navigate to an absolute URL or a "/path" on the current origin."""
    session, err = await _session_or_error(target)
    if err is not None or session is None:
        return err or make_error(_NOT_AVAILABLE)

    if url.startswith("/"):
        url = normalize_origin(session.page.url) + url
    if not session.is_allowed(url, "GET"):
        return make_error(f"Navigation blocked: {url} is outside the allowed origins.")
    try:
        await session.page.goto(url, timeout=_GOTO_TIMEOUT_MS)
    except Exception as exc:
        return make_error(f"Navigation to {url} failed: {exc}")
    return make_success(f"{await _page_summary(session)}\n\n{await session.snapshot()}")


async def read_page(target: BrowserTarget) -> dict[str, Any]:
    """Ref-annotated accessibility outline of the current page."""
    session, err = await _session_or_error(target)
    if err is not None or session is None:
        return err or make_error(_NOT_AVAILABLE)
    return make_success(f"{await _page_summary(session)}\n\n{await session.snapshot()}")


async def click(target: BrowserTarget, ref: str) -> dict[str, Any]:
    """Click the element carrying the given snapshot ref."""
    session, err = await _session_or_error(target)
    if err is not None or session is None:
        return err or make_error(_NOT_AVAILABLE)
    try:
        await session.page.click(f'[data-yukar-ref="{ref}"]', timeout=_ACTION_TIMEOUT_MS)
    except Exception as exc:
        return make_error(
            f"Click on ref {ref!r} failed: {exc}\n"
            "The ref may be stale — call browser_read for fresh refs."
        )
    return make_success(f"Clicked {ref}. Current url: {session.page.url}")


async def type_text(
    target: BrowserTarget, ref: str, text: str, press_enter: bool = False
) -> dict[str, Any]:
    """Fill the input/textarea carrying the given snapshot ref."""
    session, err = await _session_or_error(target)
    if err is not None or session is None:
        return err or make_error(_NOT_AVAILABLE)
    selector = f'[data-yukar-ref="{ref}"]'
    try:
        await session.page.fill(selector, text, timeout=_ACTION_TIMEOUT_MS)
        if press_enter:
            await session.page.press(selector, "Enter", timeout=_ACTION_TIMEOUT_MS)
    except Exception as exc:
        return make_error(
            f"Typing into ref {ref!r} failed: {exc}\n"
            "The ref may be stale — call browser_read for fresh refs."
        )
    return make_success(f"Filled {ref}{' and pressed Enter' if press_enter else ''}.")


async def press_key(target: BrowserTarget, key: str) -> dict[str, Any]:
    """Press a keyboard key on the focused element."""
    session, err = await _session_or_error(target)
    if err is not None or session is None:
        return err or make_error(_NOT_AVAILABLE)
    try:
        await session.page.keyboard.press(key)
    except Exception as exc:
        return make_error(f"Key press {key!r} failed: {exc}")
    return make_success(f"Pressed {key}.")


async def screenshot(target: BrowserTarget, full_page: bool = False) -> dict[str, Any]:
    """JPEG screenshot of the current page as an image content block."""
    session, err = await _session_or_error(target)
    if err is not None or session is None:
        return err or make_error(_NOT_AVAILABLE)
    try:
        data = await session.page.screenshot(
            type="jpeg", quality=_SCREENSHOT_JPEG_QUALITY, full_page=full_page
        )
    except Exception as exc:
        return make_error(f"Screenshot failed: {exc}")
    return {
        "status": "success",
        "content": [
            {"text": f"Screenshot of {session.page.url}"},
            {"image": {"format": "jpeg", "source": {"bytes": data}}},
        ],
    }


async def console_tail(target: BrowserTarget, lines: int = 50) -> dict[str, Any]:
    """Recent console output (log/warn/error + page errors)."""
    session, err = await _session_or_error(target)
    if err is not None or session is None:
        return err or make_error(_NOT_AVAILABLE)
    tail = session.console_tail(lines)
    return make_success(tail if tail else "(console is empty)")


async def server_log_tail(
    target: BrowserTarget, service: str | None = None, lines: int = 100
) -> dict[str, Any]:
    """Host-captured stdout/stderr tail of the trial's services."""
    manager = get_dev_server_manager()
    if manager is None:
        return make_error(_NOT_AVAILABLE)
    if manager.get_entry(target.trial_key) is None:
        return make_error(_NOT_OPEN)
    tail = manager.log_tail(target.trial_key, service, max_lines=lines)
    return make_success(tail if tail else "(no output captured)")


async def stop_servers(target: BrowserTarget) -> dict[str, Any]:
    """Stop this trial's services and close the caller's page (idempotent)."""
    manager = get_dev_server_manager()
    sessions = get_browser_session_manager()
    if manager is None:
        return make_error(_NOT_AVAILABLE)
    if sessions is not None:
        await sessions.close(target.session_key)
    stopped = await manager.stop(target.trial_key)
    return make_success("Dev servers stopped." if stopped else "No dev server was running.")
