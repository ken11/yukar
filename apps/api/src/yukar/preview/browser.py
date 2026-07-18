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
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pathlib import Path

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

# Hostnames that always resolve to the loopback interface in Chromium
# (RFC 6761: localhost and *.localhost never leave the machine).  They are
# canonicalised to 127.0.0.1 so the allow-set entry the host builds from a
# ServiceHandle origin (always 127.0.0.1) also matches the localhost spelling
# dev servers print in their logs and apps bake into redirects/absolute URLs.
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})

_CONSOLE_CAP = 200
_SNAPSHOT_CAP_CHARS = 40_000
_REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 20
_BLOCKED_ORIGIN_CAP = 50  # distinct origins tracked per session / per repo
_BLOCKED_SUMMARY_MAX = 5  # origins listed in the agent-facing summary line


def normalize_origin(url: str) -> str:
    """Canonical ``scheme://host[:port]`` of *url* (ws→http for comparison).

    Default ports are dropped so ``https://x:443`` and ``https://x`` compare
    equal; loopback hostnames (``localhost`` / ``*.localhost`` / ``::1``)
    canonicalise to ``127.0.0.1`` so every spelling of the same local server
    compares equal; unknown/relative URLs normalize to "" (never matches an
    allow-set).
    """
    parsed = urlparse(url)
    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
    host = (parsed.hostname or "").lower()
    if host in _LOOPBACK_HOSTS or host.endswith(".localhost"):
        host = "127.0.0.1"
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
    // Never surface the value of a password field: the real accessibility tree
    // masks it, but this walker would otherwise hand the plaintext to the agent
    // (a logged-in agent reaching a settings page can see a pre-filled secret).
    if (!text && ('value' in el) && typeof el.value === 'string'
        && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')
        && el.type !== 'password') text = el.value;
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
class BlockedOriginStat:
    """Aggregate of egress-gate rejections for one origin (§13)."""

    origin: str
    count: int = 0
    resource_types: set[str] = field(default_factory=set)
    last_at: float = 0.0  # time.time()

    def bump(self, resource_type: str) -> None:
        self.count += 1
        self.resource_types.add(resource_type)
        self.last_at = time.time()


def _record_into(stats: dict[str, BlockedOriginStat], url: str, resource_type: str) -> None:
    """Bump the per-origin stat in *stats*, respecting the origin cap."""
    origin = normalize_origin(url) or url[:120]
    stat = stats.get(origin)
    if stat is None:
        if len(stats) >= _BLOCKED_ORIGIN_CAP:
            return  # keep the earliest origins; a runaway page can't grow this
        stat = stats[origin] = BlockedOriginStat(origin=origin)
    stat.bump(resource_type)


@dataclass
class BrowserSession:
    """One agent's page plus its egress allow-set and console capture."""

    key: SessionKey
    context: BrowserContext
    page: Page
    repo_name: str = ""  # attribution for blocked-origin stats (§13)
    allowed_origins: set[str] = field(default_factory=set)
    cdn_preset_enabled: bool = False
    console: deque[str] = field(default_factory=lambda: deque(maxlen=_CONSOLE_CAP))
    blocked: dict[str, BlockedOriginStat] = field(default_factory=dict)

    def set_allowed(self, origins: list[str], *, cdn_preset: bool) -> None:
        """REPLACE the egress allow-set with the current config's origins.

        Called by open_app on every browser_open.  Replacing (not unioning)
        means a config change takes effect on the next open without tearing the
        session down: origins the user removed stop being allowed, a disabled
        CDN preset stops matching, and a service relaunched on a new port drops
        its stale origin (the design §5 invariant "allowed = the origins this
        trial's services are CURRENTLY serving").
        """
        self.allowed_origins = {normalize_origin(o) for o in origins}
        self.allowed_origins.discard("")
        self.cdn_preset_enabled = cdn_preset

    def is_allowed(self, url: str, method: str) -> bool:
        origin = normalize_origin(url)
        if origin in self.allowed_origins:
            return True
        return (
            self.cdn_preset_enabled
            and origin in COMMON_CDN_ORIGINS
            and method.upper() == "GET"
        )

    def blocked_summary(self) -> str:
        """Short agent-facing digest of what the egress gate rejected, or ""."""
        if not self.blocked:
            return ""
        top = sorted(self.blocked.values(), key=lambda s: -s.count)[:_BLOCKED_SUMMARY_MAX]
        lines = [
            f"- {s.origin} ×{s.count} ({', '.join(sorted(s.resource_types))})" for s in top
        ]
        more = len(self.blocked) - len(top)
        if more > 0:
            lines.append(f"- … and {more} more origin(s)")
        return (
            "[egress] The gate blocked requests to origins outside the allow-set:\n"
            + "\n".join(lines)
            + "\nIf the app needs one of these, report it to the user — they can add "
            "it to the repo's dev-server allowed_origins."
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
        # Blocked-origin aggregate per (project, repo) — decoupled from session
        # lifetime so the UI can still answer "what should I allow?" after the
        # run parked or the session closed (§13).  Process memory only.
        self._blocked_by_repo: dict[tuple[str, str], dict[str, BlockedOriginStat]] = {}
        # Serialises browser launch + session creation so parallel agents
        # (the scheduler runs Workers concurrently) can't each start a separate
        # Chromium and orphan all but the last.
        self._lock = asyncio.Lock()

    def record_blocked(self, session: BrowserSession, url: str, resource_type: str) -> None:
        """Record one egress-gate rejection on the session AND the repo aggregate."""
        _record_into(session.blocked, url, resource_type)
        repo_key = (session.key.project_id, session.repo_name)
        _record_into(self._blocked_by_repo.setdefault(repo_key, {}), url, resource_type)

    def blocked_origins(self, project_id: str) -> list[tuple[str, BlockedOriginStat]]:
        """(repo_name, stat) pairs for the project, most recent first."""
        rows = [
            (repo, stat)
            for (pid, repo), stats in self._blocked_by_repo.items()
            if pid == project_id
            for stat in stats.values()
        ]
        rows.sort(key=lambda r: -r[1].last_at)
        return rows

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

    async def rendering_context(self, *, width: int, height: int) -> BrowserContext:
        """Fresh context on the shared Chromium for HOST-side HTML rendering.

        Used by the slide-preview renderer (slides/preview.py): the host feeds
        it fully self-contained content via ``page.set_content``.  Unlike agent
        sessions there is no allow-set to consult, so the gate is absolute —
        every network request is aborted (data: URIs never hit the router).
        The caller owns the context and must close it.
        """
        async with self._lock:
            browser = await self._ensure_browser()
        context = await browser.new_context(
            viewport={"width": width, "height": height}, service_workers="block"
        )
        # Setup awaits can be interrupted (run-stop cancellation, browser
        # crash); close the just-created context on ANY exit so it never
        # lingers unreferenced in the shared Chromium until close_all.
        try:
            # Same non-HTTP egress neutering as agent sessions: route
            # interception cannot see WebRTC data channels, so the init script
            # closes that hole even though rendered content is host-authored.
            await context.add_init_script(_NEUTER_NON_HTTP_EGRESS_JS)

            async def _deny(route: Route) -> None:
                await route.abort("blockedbyclient")

            async def _deny_ws(ws: WebSocketRoute) -> None:
                await ws.close()

            await context.route("**/*", _deny)
            await context.route_web_socket("**/*", _deny_ws)
        except BaseException:
            with contextlib.suppress(Exception):
                await context.close()
            raise
        return context

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

    async def session(
        self,
        key: SessionKey,
        *,
        repo_name: str = "",
        storage_state: Path | None = None,
    ) -> BrowserSession:
        """Return the owner's session, creating page + egress gate on first use.

        Called by browser_open (which then populates the allow-set).  A stale
        session whose page has closed is torn down (context closed) and rebuilt
        so a crashed renderer never leaks its context.

        Args:
            key: The owner's session key.
            repo_name: Attributes blocked-origin stats to the repo (§13); only
                browser_open knows it, every other tool reuses the session.
            storage_state: User-captured auth state (cookies + localStorage,
                design §12) loaded into the NEW context so the agent starts
                logged in.  Applied only at context creation; an existing live
                session keeps its state.  A corrupt file logs a warning and
                falls back to a clean context (verification still works — the
                agent just hits the login wall and reports it).
        """
        async with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                if not existing.page.is_closed():
                    if repo_name:
                        existing.repo_name = repo_name
                    return existing
                # Page died (renderer crash / agent close) — drop the old
                # context before rebuilding so it does not leak.
                self._sessions.pop(key, None)
                await self._close_session(existing)

            browser = await self._ensure_browser()
            # service_workers="block": SW-initiated requests bypass route
            # interception entirely — with agent-authored page code that would
            # be an egress hole, so service workers are disabled outright.
            context_kwargs: dict[str, object] = {
                "viewport": {"width": 1280, "height": 800},
                "service_workers": "block",
            }
            if storage_state is not None:
                context_kwargs["storage_state"] = str(storage_state)
            try:
                context = await browser.new_context(**context_kwargs)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            except Exception:
                if storage_state is None:
                    raise
                # Corrupt/unreadable auth state — fall back to a clean context.
                logger.warning(
                    "Browser auth state %s could not be loaded — starting unauthenticated",
                    storage_state,
                    exc_info=True,
                )
                context_kwargs.pop("storage_state", None)
                context = await browser.new_context(**context_kwargs)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            await context.add_init_script(_NEUTER_NON_HTTP_EGRESS_JS)
            page = await context.new_page()
            session = BrowserSession(key=key, context=context, page=page, repo_name=repo_name)

            async def _gate(route: Route) -> None:
                request = route.request
                if not session.is_allowed(request.url, request.method):
                    logger.info("Browser egress blocked: %s %s", request.method, request.url)
                    self.record_blocked(session, request.url, request.resource_type)
                    await route.abort("blockedbyclient")
                    return
                # Documents (top-level navigations) and streaming types take
                # continue_() rather than the manual fetch+fulfill path:
                #  - document: a FULFILLED response carries no real remote IP, so
                #    Chromium classifies the PAGE's address space as Unknown
                #    (treated as public).  Every later request from that page to a
                #    127.0.0.1 sibling service — fetch / xhr / WebSocket, the
                #    design's multi-service topology (frontend → backend, HMR) —
                #    is then a public→loopback request that Local Network Access
                #    blocks (ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS).
                #    continue_() lets the real loopback connection set the address
                #    space, so cross-service verification works.
                #  - streaming: route.fetch() buffers the whole body and would
                #    hang on an endless stream.
                # Redirect residual for BOTH (design §5): a SERVER redirect from
                # an allowed origin to a disallowed one is followed un-gated.
                # Accepted because (a) a DIRECT request to a disallowed origin is
                # still blocked above for every type, and (b) the redirect
                # Location is authored by the allowed dev-server subprocess, which
                # already has ungated egress — so it launders nothing the
                # subprocess could not exfiltrate directly.  Non-document
                # subresources (img / script / css / font / …) still take the
                # manual re-gating path below.
                if (
                    request.resource_type == "document"
                    or request.resource_type in _STREAMING_RESOURCE_TYPES
                ):
                    await route.continue_()
                    return
                # Non-streaming types: follow the redirect chain ourselves,
                # gating every hop, and hand the browser only the final response
                # — so a redirect to a disallowed origin is aborted rather than
                # followed blindly.
                try:
                    response = await route.fetch(max_redirects=0)
                    hops = 0
                    method = request.method
                    while response.status in _REDIRECT_STATUSES and hops < _MAX_REDIRECTS:
                        location = response.headers.get("location")
                        if not location:
                            break
                        next_url = urljoin(response.url, location)
                        if not session.is_allowed(next_url, method):
                            logger.info("Browser egress blocked (redirect): %s", next_url)
                            self.record_blocked(session, next_url, request.resource_type)
                            await route.abort("blockedbyclient")
                            return
                        # 307/308 preserve method+body; 303 and the historical
                        # 301/302 non-GET behaviour downgrade to GET.  Following
                        # the chain with GET would turn a form POST into a GET and
                        # make scenario verification report a false defect.
                        if response.status in (307, 308):
                            body = request.post_data_buffer
                            if body is not None:
                                response = await session.context.request.fetch(
                                    next_url, method=method, data=body, max_redirects=0
                                )
                            else:
                                response = await session.context.request.fetch(
                                    next_url, method=method, max_redirects=0
                                )
                        else:
                            method = "GET"
                            response = await session.context.request.get(
                                next_url, max_redirects=0
                            )
                        hops += 1
                    if response.status in _REDIRECT_STATUSES:
                        # Cap hit while still redirecting: the next hop was never
                        # gated, and a fulfilled 3xx is followed by the browser
                        # un-gated (design §5).  Fail closed rather than hand the
                        # browser an un-gated redirect.
                        logger.info(
                            "Browser egress blocked: redirect chain exceeded %d hops",
                            _MAX_REDIRECTS,
                        )
                        await route.abort("blockedbyclient")
                        return
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
                    self.record_blocked(session, ws.url, "websocket")
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

    async def close_for_repo(self, project_id: str, repo_name: str) -> None:
        """Close every session attributed to *repo_name* (auth state changed §12).

        The next browser_open rebuilds the context with the new storage_state —
        the same "call browser_open first" recovery path agents already know.
        """
        keys = [
            k
            for k, s in self._sessions.items()
            if k.project_id == project_id and s.repo_name == repo_name
        ]
        for k in keys:
            await self.close(k)

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
