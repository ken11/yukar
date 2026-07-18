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

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yukar.agents.tools.response_builder import make_error, make_success
from yukar.config import paths
from yukar.models.project import DevServerConfig
from yukar.preview.browser import (
    BrowserSession,
    SessionKey,
    get_browser_session_manager,
    normalize_origin,
)
from yukar.preview.manager import (
    DevServerError,
    TrialKey,
    ensure_with_dependencies,
    get_dev_server_manager,
)
from yukar.storage.project_repo import get_repo
from yukar.storage.screenshots_repo import save_epic_screenshot

_ACTION_TIMEOUT_MS = 10_000
_GOTO_TIMEOUT_MS = 30_000
_SCREENSHOT_JPEG_QUALITY = 70

_FULL_PAGE_MAX_PX = 6000
"""Size cap (both dimensions — in practice height is the one exceeded) for
full-page captures.  Chromium rasterises beyond-viewport shots as one surface
(corrupt past its ~16k texture limit), vision APIs reject images with any
dimension over 8000px, and a taller strip is downscaled into illegibility
anyway — past a few viewports the agent should scroll and take viewport shots
instead.  The wrapper docstrings in browser_tools/browser_overview_tools
mention this value; keep them in sync."""

_READ_SCROLL_JS = "() => ({ x: window.scrollX, y: window.scrollY })"

# behavior:"instant" everywhere — plain scrollTo obeys a page's CSS
# `scroll-behavior: smooth`, which would animate for hundreds of ms and let
# the capture fire before the page actually reaches the target position.
_RESTORE_SCROLL_JS = '({x, y}) => window.scrollTo({ top: y, left: x, behavior: "instant" })'

_PREPARE_FULL_PAGE_JS = """
async (maxPx) => {
  const doc = document.documentElement;
  const body = document.body;
  const frame = () =>
    new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  const jump = (top) => window.scrollTo({ top, left: 0, behavior: "instant" });
  const pageHeight = () =>
    Math.max(doc.scrollHeight, body ? body.scrollHeight : 0);
  // Walk down the page once so lazy-loaded content (loading="lazy",
  // IntersectionObserver mounts) actually renders in the capture.  The limit
  // is measured ONCE up front so infinite-scroll feeds cannot keep growing
  // the walk.
  const step = Math.max(window.innerHeight, 1);
  const limit = Math.min(pageHeight(), maxPx);
  for (let y = 0; y <= limit; y += step) {
    jump(y);
    await frame();
  }
  // Park at the top: captureBeyondViewport paints fixed/sticky elements at
  // the CURRENT scroll offset, so capturing while scrolled mid-page floats
  // them into the middle of the image.
  jump(0);
  await frame();
  return {
    width: Math.max(doc.scrollWidth, body ? body.scrollWidth : 0),
    height: pageHeight(),
  };
}
"""

_NOT_AVAILABLE = "Browser verification is not available in this process."
_NOT_OPEN = "No dev server is running for this trial. Call browser_open first."

# Registry/session sentinel for base-checkout targets (shared with the
# overview bundle).  Real trial ids are the legacy literal "manager" or
# generated thread ids, so this cannot collide.
BASE_TRIAL_ID = "__base__"

TreeEnsurer = Callable[[str], Awaitable[Path]]
"""repo name → the ACTIVE trial's worktree path, created (branch + worktree)
when missing.  Provided by the orchestrator, which owns the epic object and
state lock the bookkeeping (``epic.touched_repos``) must go through.  Raises
DevServerError when the worktree cannot be provided.

Dev servers must NEVER launch in a repo's base checkout when a trial is
active: the base checkout is a shared directory (the user's own dev server
may already run there, and every epic would otherwise target the same tree),
so a second launch collides — Next.js detects the duplicate instance and
refuses to start — and build artifacts would pollute the user's checkout."""


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


async def _page_result(session: BrowserSession) -> dict[str, Any]:
    """url/title + snapshot, plus the egress blocked-origin digest when any (§13)."""
    text = f"{await _page_summary(session)}\n\n{await session.snapshot()}"
    blocked = session.blocked_summary()
    if blocked:
        text += f"\n\n{blocked}"
    return make_success(text)


async def open_app(
    target: BrowserTarget,
    service: str | None = None,
    *,
    ensure_tree: TreeEnsurer | None = None,
) -> dict[str, Any]:
    """Ensure the declared services and open one of them (default: first).

    ``{port:repo/service}`` references in the config pull in OTHER repos'
    services as dependencies: they are launched first (awaiting readiness),
    their real ports substitute into this repo's command/env, and their
    origins join the page's egress allow-set so the app can call them.

    ``ensure_tree`` lets dependency repos without a worktree get one created
    for the active trial instead of falling back to the shared base checkout
    (see :data:`TreeEnsurer` for why base launches are a collision hazard).
    Without it (or when the target itself is a base-checkout sentinel) the
    base fallback remains.
    """
    manager = get_dev_server_manager()
    sessions = get_browser_session_manager()
    if manager is None or sessions is None:
        return make_error(_NOT_AVAILABLE)

    repo = await get_repo(target.workspace_root, target.project_id, target.repo_name)
    config = repo.dev_server if repo is not None else None
    if repo is None or config is None:
        return make_error(
            f"Repo {target.repo_name!r} has no dev-server config. "
            "Ask the user to configure it in the project's repo settings."
        )
    service_names = [s.name for s in config.services]
    chosen = service or service_names[0]
    if chosen not in service_names:
        return make_error(f"Unknown service {chosen!r}. Declared: {service_names}")

    async def _load(name: str) -> tuple[DevServerConfig, Path] | None:
        dep = await get_repo(target.workspace_root, target.project_id, name)
        if dep is None or dep.dev_server is None:
            return None
        return dep.dev_server, Path(dep.path)

    async def _resolve(name: str, base: Path) -> tuple[TrialKey, Path]:
        # Dependency repos follow the overview-bundle rule: the SAME trial's
        # worktree, created via ensure_tree when it does not exist yet.  The
        # base checkout (BASE_TRIAL_ID sentinel) is only a fallback when no
        # ensurer is wired or the target itself is a base sentinel — launching
        # there risks colliding with the user's own dev server (TreeEnsurer).
        if target.trial_id != BASE_TRIAL_ID:
            worktree = paths.worktree_dir(
                target.workspace_root,
                target.project_id,
                target.epic_id,
                target.trial_id,
                name,
            )
            if not await asyncio.to_thread(worktree.is_dir) and ensure_tree is not None:
                worktree = await ensure_tree(name)
            if await asyncio.to_thread(worktree.is_dir):
                return (
                    TrialKey(
                        project_id=target.project_id,
                        epic_id=target.epic_id,
                        trial_id=target.trial_id,
                        repo_name=name,
                    ),
                    worktree,
                )
        return (
            TrialKey(
                project_id=target.project_id,
                epic_id=target.epic_id,
                trial_id=BASE_TRIAL_ID,
                repo_name=name,
            ),
            base,
        )

    try:
        # repo_root anchors repo-relative env_file declarations to the BASE
        # checkout even when the services run in a trial worktree (§11).
        entries = await ensure_with_dependencies(
            manager,
            target.repo_name,
            config,
            key=target.trial_key,
            tree=target.worktree_path,
            repo_root=Path(repo.path),
            load_config=_load,
            resolve_tree=_resolve,
        )
    except DevServerError as exc:
        return make_error(f"Dev server failed to start: {exc}")
    entry = entries[target.repo_name]

    # User-captured auth state (design §12): loaded into a NEW context so the
    # agent starts logged in.  The agent never reads the file itself.
    auth_state = paths.browser_auth_state(
        target.workspace_root, target.project_id, target.repo_name
    )
    has_auth_state = await asyncio.to_thread(auth_state.is_file)
    session = await sessions.session(
        target.session_key,
        repo_name=target.repo_name,
        storage_state=auth_state if has_auth_state else None,
    )
    # REPLACE (not accumulate) the allow-set from the CURRENT live service
    # origins + config, so a removed origin / disabled CDN / relaunched port
    # takes effect here without a session teardown.  Dependency repos' service
    # origins are included — the page must be able to call the backend it was
    # wired to via {port:repo/service}.
    dep_origins = [
        handle.origin
        for name, dep_entry in entries.items()
        if name != target.repo_name
        for handle in dep_entry.values()
    ]
    session.set_allowed(
        [
            *manager.origins(target.trial_key),
            *dep_origins,
            *config.browser.allowed_origins,
        ],
        cdn_preset=config.browser.allow_common_cdns,
    )

    # ensure() relaunches on a service-set mismatch, so a healthy reused entry
    # always contains `chosen`; guard anyway to turn any residual race into a
    # clean tool error instead of a KeyError.
    served = entry.get(chosen)
    if served is None:
        return make_error(
            f"Service {chosen!r} is not running. Declared: {list(entry)}. "
            "Try browser_open again."
        )
    url = served.origin
    try:
        await session.page.goto(url, timeout=_GOTO_TIMEOUT_MS)
    except Exception as exc:
        return make_error(
            f"Navigation to {url} failed: {exc}\n"
            f"--- server log tail ---\n{manager.log_tail(target.trial_key, max_lines=40)}"
        )
    return await _page_result(session)


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
    return await _page_result(session)


async def read_page(target: BrowserTarget) -> dict[str, Any]:
    """Ref-annotated accessibility outline of the current page."""
    session, err = await _session_or_error(target)
    if err is not None or session is None:
        return err or make_error(_NOT_AVAILABLE)
    return await _page_result(session)


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


async def screenshot(
    target: BrowserTarget,
    full_page: bool = False,
    *,
    save: bool = False,
    label: str | None = None,
) -> dict[str, Any]:
    """JPEG screenshot of the current page as an image content block.

    Full-page captures are hardened against the ways Chromium's single-pass
    beyond-viewport render breaks on long pages: the page is walked once to
    trigger lazy-loaded content and parked at the top (fixed/sticky elements
    paint at the current scroll offset), animations are frozen, the capture
    is clipped at ``_FULL_PAGE_MAX_PX`` (with a note when the page was cut),
    and the original scroll position is restored afterwards.

    When ``save`` is set the same bytes are also persisted under the epic docs
    folder (``docs/screenshots/``) so the user can review them on the Docs
    page; the saved relative path is appended to the result text.  Persisting
    is opt-in on purpose — keeping every shot would waste disk, so the caller
    decides which ones are worth retaining.
    """
    session, err = await _session_or_error(target)
    if err is not None or session is None:
        return err or make_error(_NOT_AVAILABLE)

    kwargs: dict[str, Any] = {
        "type": "jpeg",
        "quality": _SCREENSHOT_JPEG_QUALITY,
        "full_page": full_page,
        "animations": "disabled",
    }
    truncation_note = ""
    restore_scroll: dict[str, Any] | None = None
    if full_page:
        # Every evaluate gets an outer wait_for: Playwright's evaluate is not
        # covered by the default timeout, and a page that starves rAF (busy
        # renderer loop) would otherwise hang the tool call forever.
        evaluate_timeout = _ACTION_TIMEOUT_MS / 1000
        # Scroll position is read SEPARATELY before the walk so a failure
        # mid-walk still leaves us able to restore where the agent was.
        with contextlib.suppress(Exception):
            restore_scroll = await asyncio.wait_for(
                session.page.evaluate(_READ_SCROLL_JS), timeout=evaluate_timeout
            )
        metrics: dict[str, Any] | None = None
        try:
            metrics = await asyncio.wait_for(
                session.page.evaluate(_PREPARE_FULL_PAGE_JS, _FULL_PAGE_MAX_PX),
                timeout=evaluate_timeout,
            )
            # Give lazy-loaded images the scroll walk just requested a moment
            # to arrive and decode before the capture.
            await asyncio.sleep(0.2)
        except Exception:
            # Best effort — a mid-navigation page can kill the evaluate; the
            # capped capture below still stands a chance.
            metrics = None
        # The clip is applied UNCONDITIONALLY (the driver trims it to the
        # document rect, so short pages are unaffected): even when the prepare
        # walk failed and the page size is unknown, an over-tall capture must
        # never reach Chromium's raster limit or the vision API's 8000px cap.
        clip_width = _FULL_PAGE_MAX_PX
        if metrics is not None:
            clip_width = min(metrics["width"], _FULL_PAGE_MAX_PX)
            if metrics["height"] > _FULL_PAGE_MAX_PX:
                truncation_note = (
                    f"\n(Page is {metrics['height']}px tall — captured the top "
                    f"{_FULL_PAGE_MAX_PX}px. Scroll down and take viewport "
                    "screenshots to inspect the rest.)"
                )
        kwargs["clip"] = {"x": 0, "y": 0, "width": clip_width, "height": _FULL_PAGE_MAX_PX}
    try:
        data = await session.page.screenshot(**kwargs)
    except Exception as exc:
        return make_error(f"Screenshot failed: {exc}")
    finally:
        if restore_scroll is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    session.page.evaluate(_RESTORE_SCROLL_JS, restore_scroll),
                    timeout=_ACTION_TIMEOUT_MS / 1000,
                )

    text = f"Screenshot of {session.page.url}{truncation_note}"
    if save:
        try:
            filename = await save_epic_screenshot(
                target.workspace_root,
                target.project_id,
                target.epic_id,
                data,
                label=label or target.repo_name,
            )
            text += f"\nSaved to epic docs: docs/screenshots/{filename}"
        except (OSError, ValueError) as exc:
            text += f"\n(Could not save to epic docs: {exc})"
    return {
        "status": "success",
        "content": [
            {"text": text},
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
