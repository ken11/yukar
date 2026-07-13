"""Trial-scoped headless browser sessions with fail-closed egress.

Every network request the page makes — navigation, subresource, fetch, and
WebSocket — passes an origin gate (docs/browser-verification-design.md §5):

- the trial's own dev-server origins are allowed,
- ``allowed_origins`` from the repo's ``dev_server.browser`` config are allowed,
- the built-in well-known CDN preset is allowed for GET only (§5.1),
- everything else is aborted.

Method filtering exists because a GET to an attacker-controlled origin can
exfiltrate data in the query string; the CDN preset is safe only because its
operators are fixed, well-known parties whose access logs an attacker cannot
read.  WebSockets never match the CDN preset.

Sessions are keyed by (project, epic, trial, owner) — one page per agent so
parallel Workers never interleave navigations — and share one headless
Chromium process.  Cleanup mirrors the dev-server hooks: owner close after an
attempt, epic close on run end, trial close on archive, close_all on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Page,
        Playwright,
        Route,
        WebSocketRoute,
    )

# Fixed, well-known CDN operators (design §5.1).  GET only, no credentials.
# Deliberately a code constant — not user-editable config — so the safety
# argument (operator identity) cannot drift silently.
COMMON_CDN_ORIGINS: frozenset[str] = frozenset(
    {
        "https://fonts.googleapis.com",
        "https://fonts.gstatic.com",
        "https://cdn.jsdelivr.net",
        "https://unpkg.com",
        "https://cdnjs.cloudflare.com",
        "https://ajax.googleapis.com",
        "https://code.jquery.com",
        "https://esm.sh",
        "https://cdn.tailwindcss.com",
    }
)

_CONSOLE_CAP = 200
_SNAPSHOT_CAP_CHARS = 40_000
_REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 20


def normalize_origin(url: str) -> str:
    """Canonical ``scheme://host[:port]`` of *url* (ws→http for comparison).

    Default ports are dropped so ``https://x:443`` and ``https://x`` compare
    equal; unknown/relative URLs normalize to "" (never matches an allow-set).
    """
    parsed = urlparse(url)
    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
    host = (parsed.hostname or "").lower()
    if not scheme or not host:
        return ""
    try:
        port = parsed.port
    except ValueError:
        # Malformed port (user-typed allowed_origins entry) — never matches.
        return ""
    if port is None or (scheme, port) in (("http", 80), ("https", 443)):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


# The snapshot walker runs in the page: it tags salient elements with
# data-yukar-ref attributes and returns a playwright-mcp-style YAML outline.
# Refs are reassigned on every run — a stale ref after a re-render simply
# stops matching, and the tools tell the agent to re-read.
_SNAPSHOT_JS = """
() => {
  let counter = 0;
  const lines = [];
  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : null;
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'hidden') return null;
      return ({checkbox: 'checkbox', radio: 'radio', range: 'slider',
               submit: 'button', reset: 'button', button: 'button'})[t] || 'textbox';
    }
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'img') return 'img';
    if (tag === 'nav') return 'navigation';
    if (tag === 'main') return 'main';
    if (tag === 'form') return 'form';
    if (tag === 'table') return 'table';
    if (tag === 'li') return 'listitem';
    if (tag === 'label') return 'label';
    return null;
  };
  const nameOf = (el) => {
    const own = el.getAttribute('aria-label') || el.getAttribute('alt')
      || el.getAttribute('placeholder') || el.getAttribute('title') || '';
    let text = own;
    if (!text && ('value' in el) && typeof el.value === 'string'
        && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) text = el.value;
    if (!text) text = el.textContent || '';
    return text.trim().replace(/\\s+/g, ' ').slice(0, 80);
  };
  const isVisible = (el) => {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0;
  };
  const walk = (el, depth) => {
    if (el.nodeType !== 1 || !isVisible(el)) return;
    const role = roleOf(el);
    if (role) {
      const ref = 'e' + (++counter);
      el.setAttribute('data-yukar-ref', ref);
      let line = '  '.repeat(depth) + '- ' + role;
      const name = nameOf(el);
      if (name) line += ' "' + name.replace(/"/g, "'") + '"';
      if ((el.type === 'checkbox' || el.type === 'radio') && el.checked) line += ' [checked]';
      if (el.disabled) line += ' [disabled]';
      line += ' [ref=' + ref + ']';
      lines.push(line);
      depth += 1;
    } else if (el.childElementCount === 0) {
      const text = (el.textContent || '').trim().replace(/\\s+/g, ' ');
      if (text) lines.push('  '.repeat(depth) + '- text: ' + text.slice(0, 120));
      return;
    }
    for (const child of el.children) walk(child, depth);
  };
  if (document.body) walk(document.body, 0);
  return lines.join('\\n');
}
"""


@dataclass(frozen=True, slots=True)
class SessionKey:
    project_id: str
    epic_id: str
    trial_id: str
    owner_id: str  # worker/evaluator thread id — one page per agent


@dataclass
class BrowserSession:
    """One agent's page plus its egress allow-set and console capture."""

    key: SessionKey
    context: BrowserContext
    page: Page
    allowed_origins: set[str] = field(default_factory=set)
    cdn_preset_enabled: bool = False
    console: deque[str] = field(default_factory=lambda: deque(maxlen=_CONSOLE_CAP))

    def allow(self, origins: list[str], *, cdn_preset: bool) -> None:
        self.allowed_origins.update(normalize_origin(o) for o in origins)
        self.allowed_origins.discard("")
        if cdn_preset:
            self.cdn_preset_enabled = True

    def is_allowed(self, url: str, method: str) -> bool:
        origin = normalize_origin(url)
        if origin in self.allowed_origins:
            return True
        return (
            self.cdn_preset_enabled
            and origin in COMMON_CDN_ORIGINS
            and method.upper() == "GET"
        )

    async def snapshot(self) -> str:
        text: str = await self.page.evaluate(_SNAPSHOT_JS)
        if len(text) > _SNAPSHOT_CAP_CHARS:
            text = text[:_SNAPSHOT_CAP_CHARS] + "\n… (snapshot truncated)"
        return text

    def console_tail(self, max_lines: int = 50) -> str:
        return "\n".join(list(self.console)[-max_lines:])


# Resource types whose responses may stream without end (SSE-over-fetch,
# ReadableStream loops, media).  route.fetch() buffers the whole body, so it
# hangs forever on these — they take the non-buffering continue_() path.  The
# trade-off: a SERVER redirect from an allowed origin to a disallowed one is
# not re-gated for these types (see _gate).  This does not widen exfiltration
# in this architecture — a DIRECT request to a disallowed origin is still
# blocked by the is_allowed() pre-check for every type, and the dev-server
# subprocess already has ungated egress, so nothing an allowed-origin
# open-redirect could launder is secret from it.
_STREAMING_RESOURCE_TYPES: frozenset[str] = frozenset(
    {"eventsource", "fetch", "xhr", "media", "websocket"}
)

# Neuter non-HTTP egress channels that route interception cannot see
# (WebRTC data channels, WebTransport over HTTP/3).  Injected before any page
# script runs, in every frame.  Chromium feature flags are added at launch as
# defence-in-depth, but the init script is the reliable cross-version gate.
_NEUTER_NON_HTTP_EGRESS_JS = """
(() => {
  const block = (name) => {
    try {
      Object.defineProperty(window, name, {
        configurable: false, enumerable: false,
        get() { throw new Error(name + ' is disabled in yukar verification'); },
      });
    } catch (_e) { /* already locked */ }
  };
  ['RTCPeerConnection', 'webkitRTCPeerConnection', 'RTCDataChannel',
   'WebTransport'].forEach(block);
})();
"""

_LAUNCH_ARGS: list[str] = [
    "--disable-features=WebRtcHideLocalIpsWithMdns,WebTransport",
]


class BrowserSessionManager:
    """Lazy shared Chromium + per-agent sessions (singleton via init/get)."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._sessions: dict[SessionKey, BrowserSession] = {}
        # Serialises browser launch + session creation so parallel agents
        # (the scheduler runs Workers concurrently) can't each start a separate
        # Chromium and orphan all but the last.
        self._lock = asyncio.Lock()

    async def _ensure_browser(self) -> Browser:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        from playwright.async_api import async_playwright

        if self._playwright is None:
            self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True, args=_LAUNCH_ARGS
        )
        return self._browser

    def get_open_session(self, key: SessionKey) -> BrowserSession | None:
        """Return the owner's session iff it exists with a live page, else None.

        Used by every tool except browser_open: those must not lazily create a
        session with an empty egress allow-set (which would block even the
        trial's own origin) — the allow-set is populated only by open_app.
        """
        existing = self._sessions.get(key)
        if existing is not None and not existing.page.is_closed():
            return existing
        return None

    async def session(self, key: SessionKey) -> BrowserSession:
        """Return the owner's session, creating page + egress gate on first use.

        Called by browser_open (which then populates the allow-set).  A stale
        session whose page has closed is torn down (context closed) and rebuilt
        so a crashed renderer never leaks its context.
        """
        async with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                if not existing.page.is_closed():
                    return existing
                # Page died (renderer crash / agent close) — drop the old
                # context before rebuilding so it does not leak.
                self._sessions.pop(key, None)
                await self._close_session(existing)

            browser = await self._ensure_browser()
            # service_workers="block": SW-initiated requests bypass route
            # interception entirely — with agent-authored page code that would
            # be an egress hole, so service workers are disabled outright.
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                service_workers="block",
            )
            await context.add_init_script(_NEUTER_NON_HTTP_EGRESS_JS)
            page = await context.new_page()
            session = BrowserSession(key=key, context=context, page=page)

            async def _gate(route: Route) -> None:
                request = route.request
                if not session.is_allowed(request.url, request.method):
                    logger.info("Browser egress blocked: %s %s", request.method, request.url)
                    await route.abort("blockedbyclient")
                    return
                # Streaming-prone types take continue_(): route.fetch() buffers
                # the whole body and would hang on an endless stream.  The
                # trade-off is that a SERVER redirect from an allowed origin to
                # a disallowed one is followed by the browser un-gated for these
                # types (neither continue_ NOR fulfill re-gates a redirect hop —
                # verified empirically).  Accepted residual: a DIRECT request to
                # a disallowed origin is still blocked above for every type, and
                # the dev-server subprocess already has ungated egress, so an
                # allowed-origin open-redirect launders nothing secret from it.
                if request.resource_type in _STREAMING_RESOURCE_TYPES:
                    await route.continue_()
                    return
                # Non-streaming types: follow the redirect chain ourselves,
                # gating every hop, and hand the browser only the final response
                # — so a redirect to a disallowed origin is aborted rather than
                # followed blindly.
                try:
                    response = await route.fetch(max_redirects=0)
                    hops = 0
                    while response.status in _REDIRECT_STATUSES and hops < _MAX_REDIRECTS:
                        location = response.headers.get("location")
                        if not location:
                            break
                        next_url = urljoin(response.url, location)
                        if not session.is_allowed(next_url, request.method):
                            logger.info("Browser egress blocked (redirect): %s", next_url)
                            await route.abort("blockedbyclient")
                            return
                        response = await session.context.request.get(
                            next_url, max_redirects=0
                        )
                        hops += 1
                    await route.fulfill(response=response)
                except Exception:
                    # Page teardown mid-flight etc. — fail closed.
                    with contextlib.suppress(Exception):
                        await route.abort("failed")

            async def _ws_gate(ws: WebSocketRoute) -> None:
                # CDN preset never applies to WebSockets — explicit origins only.
                if normalize_origin(ws.url) in session.allowed_origins:
                    # Forwarding is automatic once connected (no message handlers).
                    ws.connect_to_server()
                else:
                    logger.info("Browser egress blocked (ws): %s", ws.url)
                    await ws.close()

            await context.route("**/*", _gate)
            await context.route_web_socket("**/*", _ws_gate)

            page.on(
                "console",
                lambda msg: session.console.append(f"[{msg.type}] {msg.text}"),
            )
            page.on(
                "pageerror",
                lambda err: session.console.append(f"[pageerror] {err}"),
            )

            self._sessions[key] = session
            return session

    # ------------------------------------------------------------------
    # Close paths
    # ------------------------------------------------------------------

    async def _close_session(self, session: BrowserSession) -> None:
        try:
            await session.context.close()
        except Exception:
            logger.debug("Browser session close failed", exc_info=True)

    async def close(self, key: SessionKey) -> None:
        session = self._sessions.pop(key, None)
        if session is not None:
            await self._close_session(session)

    async def close_for_trial(self, project_id: str, epic_id: str, trial_id: str) -> None:
        keys = [
            k
            for k in self._sessions
            if k.project_id == project_id and k.epic_id == epic_id and k.trial_id == trial_id
        ]
        for k in keys:
            await self.close(k)

    async def close_for_epic(self, project_id: str, epic_id: str) -> None:
        keys = [
            k for k in self._sessions if k.project_id == project_id and k.epic_id == epic_id
        ]
        for k in keys:
            await self.close(k)

    async def close_all(self) -> None:
        for k in list(self._sessions):
            await self.close(k)
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                logger.debug("Browser close failed", exc_info=True)
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                logger.debug("Playwright stop failed", exc_info=True)
            self._playwright = None


# ---------------------------------------------------------------------------
# Module-level singleton (init in app lifespan — mirrors preview.manager)
# ---------------------------------------------------------------------------

_session_manager: BrowserSessionManager | None = None


def init_browser_session_manager(manager: BrowserSessionManager | None) -> None:
    """Install (or clear, with None) the process-wide BrowserSessionManager."""
    global _session_manager  # noqa: PLW0603
    _session_manager = manager


def get_browser_session_manager() -> BrowserSessionManager | None:
    """Return the process-wide session manager, or None outside a running app."""
    return _session_manager
