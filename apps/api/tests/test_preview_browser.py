"""BrowserSession egress gate, snapshot, and console capture (real Chromium).

Static fixture pages are served by in-process ThreadingHTTPServer instances so
the tests exercise the real network path (page → route gate → 127.0.0.1)
without subprocesses.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from yukar.preview.browser import (
    COMMON_CDN_ORIGINS,
    BrowserSession,
    BrowserSessionManager,
    SessionKey,
    normalize_origin,
)

KEY = SessionKey(project_id="p", epic_id="e1", trial_id="t1", owner_id="worker-1")

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


class _QuietHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # "/redirect?to=<url>" answers 302 Location: <url> — used to prove that a
    # redirect issued by an ALLOWED origin cannot smuggle the browser to a
    # blocked one via a re-gated (non-streaming) resource type.
    # "/stream" is an endless chunked body — used to prove the gate does not
    # buffer (and therefore hang) streaming responses.
    def do_GET(self) -> None:  # noqa: N802 — http.server API
        if self.path.startswith("/redirect?to="):
            from urllib.parse import unquote

            target = unquote(self.path.split("=", 1)[1])
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            import time

            try:
                for i in range(120):
                    chunk = f"data: tick {i}\n\n".encode()
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                    time.sleep(0.25)
            except Exception:
                pass
            return
        super().do_GET()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture
def site_dir(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text(_INDEX_HTML)
    (tmp_path / "page2.html").write_text(_PAGE2_HTML)
    return tmp_path


def _serve(directory: Path) -> Iterator[str]:
    handler = partial(_QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def allowed_url(site_dir: Path) -> Iterator[str]:
    yield from _serve(site_dir)


@pytest.fixture
def blocked_url(site_dir: Path) -> Iterator[str]:
    yield from _serve(site_dir)


@pytest.fixture
async def sessions() -> AsyncIterator[BrowserSessionManager]:
    manager = BrowserSessionManager()
    yield manager
    await manager.close_all()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestNormalizeOrigin:
    def test_strips_path_and_lowercases(self) -> None:
        assert normalize_origin("HTTP://Example.COM/a/b?q=1") == "http://example.com"

    def test_keeps_explicit_port(self) -> None:
        assert normalize_origin("http://127.0.0.1:3000/x") == "http://127.0.0.1:3000"

    def test_drops_default_ports(self) -> None:
        assert normalize_origin("https://example.com:443/") == "https://example.com"
        assert normalize_origin("http://example.com:80/") == "http://example.com"

    def test_ws_maps_to_http(self) -> None:
        assert normalize_origin("ws://127.0.0.1:3000/hmr") == "http://127.0.0.1:3000"
        assert normalize_origin("wss://example.com/socket") == "https://example.com"

    def test_garbage_is_empty(self) -> None:
        assert normalize_origin("not a url") == ""
        assert normalize_origin("/relative/path") == ""


class TestIsAllowed:
    def _session(self) -> BrowserSession:
        # context/page are unused by is_allowed — construct without Playwright.
        session = BrowserSession(key=KEY, context=None, page=None)  # ty: ignore[invalid-argument-type]
        session.allow(["http://127.0.0.1:3000"], cdn_preset=True)
        return session

    def test_trial_origin_any_method(self) -> None:
        session = self._session()
        assert session.is_allowed("http://127.0.0.1:3000/api", "POST")

    def test_cdn_preset_get_only(self) -> None:
        session = self._session()
        cdn = next(iter(COMMON_CDN_ORIGINS))
        assert session.is_allowed(f"{cdn}/lib.js", "GET")
        assert not session.is_allowed(f"{cdn}/lib.js", "POST")

    def test_cdn_preset_disabled(self) -> None:
        session = BrowserSession(key=KEY, context=None, page=None)  # ty: ignore[invalid-argument-type]
        session.allow(["http://127.0.0.1:3000"], cdn_preset=False)
        cdn = next(iter(COMMON_CDN_ORIGINS))
        assert not session.is_allowed(f"{cdn}/lib.js", "GET")

    def test_unknown_origin_blocked(self) -> None:
        session = self._session()
        assert not session.is_allowed("https://attacker.example/?d=secret", "GET")


# ---------------------------------------------------------------------------
# Real-Chromium behaviour
# ---------------------------------------------------------------------------


class TestSessionGate:
    async def test_allowed_navigation_and_snapshot_refs(
        self, sessions: BrowserSessionManager, allowed_url: str
    ) -> None:
        session = await sessions.session(KEY)
        session.allow([allowed_url], cdn_preset=False)
        await session.page.goto(allowed_url)

        snapshot = await session.snapshot()
        assert 'heading "Hello Yukar"' in snapshot
        assert 'link "Go to page 2"' in snapshot
        assert "[ref=e" in snapshot
        # Refs are physically attached to the DOM.
        assert await session.page.get_attribute("a", "data-yukar-ref")

    async def test_fetch_to_disallowed_origin_blocked(
        self, sessions: BrowserSessionManager, allowed_url: str, blocked_url: str
    ) -> None:
        session = await sessions.session(KEY)
        session.allow([allowed_url], cdn_preset=False)
        await session.page.goto(allowed_url)

        allowed_status = await session.page.evaluate(
            f"fetch('{allowed_url}/page2.html').then(r => r.status).catch(() => 'blocked')"
        )
        assert allowed_status == 200

        blocked_result = await session.page.evaluate(
            f"fetch('{blocked_url}/index.html').then(r => r.status).catch(() => 'blocked')"
        )
        assert blocked_result == "blocked"

    async def test_goto_disallowed_origin_blocked(
        self, sessions: BrowserSessionManager, allowed_url: str, blocked_url: str
    ) -> None:
        session = await sessions.session(KEY)
        session.allow([allowed_url], cdn_preset=False)
        with pytest.raises(Exception, match="ERR_BLOCKED_BY_CLIENT|blocked"):
            await session.page.goto(blocked_url)

    async def test_navigation_redirect_to_disallowed_origin_blocked(
        self, sessions: BrowserSessionManager, allowed_url: str, blocked_url: str
    ) -> None:
        # A navigation (document = re-gated fetch+fulfill path) from the allowed
        # origin 302s to a blocked one; the browser's follow-up request must
        # hit the gate again and be aborted.
        from urllib.parse import quote

        session = await sessions.session(KEY)
        session.allow([allowed_url], cdn_preset=False)
        await session.page.goto(allowed_url)

        redirect_url = f"{allowed_url}/redirect?to={quote(blocked_url + '/index.html')}"
        with pytest.raises(Exception, match="ERR_BLOCKED_BY_CLIENT|blocked|net::"):
            await session.page.goto(redirect_url)

    async def test_navigation_redirect_within_allowed_origin_followed(
        self, sessions: BrowserSessionManager, allowed_url: str
    ) -> None:
        from urllib.parse import quote

        session = await sessions.session(KEY)
        session.allow([allowed_url], cdn_preset=False)
        await session.page.goto(allowed_url)

        redirect_url = f"{allowed_url}/redirect?to={quote(allowed_url + '/page2.html')}"
        await session.page.goto(redirect_url)
        assert await session.page.title() == "Page Two"

    async def test_fetch_streaming_does_not_hang(
        self, sessions: BrowserSessionManager, allowed_url: str
    ) -> None:
        # A fetch-based endless stream must not be buffered by the gate: the
        # first chunk should arrive promptly (regression for the route.fetch
        # buffering hang).
        session = await sessions.session(KEY)
        session.allow([allowed_url], cdn_preset=False)
        await session.page.goto(allowed_url)

        first_chunk = await session.page.evaluate(
            f"""(async () => {{
                const r = await fetch('{allowed_url}/stream');
                const reader = r.body.getReader();
                const {{ value }} = await reader.read();
                return new TextDecoder().decode(value);
            }})()""",
        )
        assert "tick 0" in first_chunk

    async def test_webrtc_and_webtransport_are_blocked(
        self, sessions: BrowserSessionManager, allowed_url: str
    ) -> None:
        # Non-HTTP egress channels are neutered before page scripts run, so an
        # agent-authored page cannot open them to reach an external host.
        session = await sessions.session(KEY)
        session.allow([allowed_url], cdn_preset=False)
        await session.page.goto(allowed_url)

        rtc = await session.page.evaluate(
            "(() => { try { new RTCPeerConnection(); return 'created'; }"
            " catch (e) { return 'blocked'; } })()"
        )
        assert rtc == "blocked"
        wt = await session.page.evaluate(
            "(() => { try { new WebTransport('https://evil.example/x');"
            " return 'created'; } catch (e) { return 'blocked'; } })()"
        )
        assert wt == "blocked"

    async def test_console_capture(
        self, sessions: BrowserSessionManager, allowed_url: str
    ) -> None:
        session = await sessions.session(KEY)
        session.allow([allowed_url], cdn_preset=False)
        await session.page.goto(allowed_url)
        assert "hello-from-page" in session.console_tail()

    async def test_close_for_epic_closes_page(
        self, sessions: BrowserSessionManager, allowed_url: str
    ) -> None:
        session = await sessions.session(KEY)
        session.allow([allowed_url], cdn_preset=False)
        await session.page.goto(allowed_url)

        await sessions.close_for_epic("p", "e1")
        assert session.page.is_closed()

        # A fresh session is created on next use.
        session2 = await sessions.session(KEY)
        assert session2 is not session
