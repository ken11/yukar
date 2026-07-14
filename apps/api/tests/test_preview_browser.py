"""BrowserSession egress gate, snapshot, and console capture (real Chromium).

Static fixture pages are served by in-process ThreadingHTTPServer instances so
the tests exercise the real network path (page → route gate → 127.0.0.1)
without subprocesses.
"""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
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

# A password field pre-filled with a secret (the pattern a settings page uses,
# relying on the browser to mask it).  The snapshot must NOT leak the value.
_PW_HTML = (
    "<!DOCTYPE html><html><head><title>Settings</title></head><body>"
    '<input type="password" value="topsecret-APIKEY-42">'
    '<input type="text" value="visible-username">'
    "</body></html>"
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
        if self.path.startswith("/chain/"):
            # "/chain/<n>" 302s to "/chain/<n+1>" forever (same origin) — an
            # endless redirect chain used to prove the gate fails CLOSED when the
            # hop cap is hit (aborts) rather than fulfilling an un-gated 3xx.
            n = int(self.path.rsplit("/", 1)[1])
            self.send_response(302)
            self.send_header("Location", f"/chain/{n + 1}")
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
    (tmp_path / "pw.html").write_text(_PW_HTML)
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


# --- Minimal RFC 6455 WebSocket echo server (no external dep) ---------------

_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _WS_MAGIC).encode()).digest()).decode()


def _ws_read_frame(conn: socket.socket) -> bytes | None:
    hdr = conn.recv(2)
    if len(hdr) < 2:
        return None
    length = hdr[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", conn.recv(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", conn.recv(8))[0]
    mask = conn.recv(4) if hdr[1] & 0x80 else b"\x00\x00\x00\x00"
    data = bytearray(conn.recv(length))
    for i in range(len(data)):
        data[i] ^= mask[i % 4]
    return bytes(data)


def _ws_write_frame(conn: socket.socket, payload: bytes) -> None:
    frame = bytearray([0x81])  # FIN + text opcode
    n = len(payload)
    if n < 126:
        frame.append(n)
    elif n < 65536:
        frame.append(126)
        frame += struct.pack(">H", n)
    else:
        frame.append(127)
        frame += struct.pack(">Q", n)
    conn.sendall(bytes(frame) + payload)


def _ws_serve() -> Iterator[str]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    stop = threading.Event()

    def _accept_loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=_handle, args=(conn,), daemon=True).start()

    def _handle(conn: socket.socket) -> None:
        try:
            request = conn.recv(4096).decode("latin-1")
            key = ""
            for line in request.split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                + f"Sec-WebSocket-Accept: {_ws_accept(key)}\r\n\r\n".encode()
            )
            while not stop.is_set():
                msg = _ws_read_frame(conn)
                if msg is None:
                    break
                _ws_write_frame(conn, b"echo:" + msg)
        except OSError:
            pass
        finally:
            conn.close()

    thread = threading.Thread(target=_accept_loop, daemon=True)
    thread.start()
    try:
        # Origin form (http) for the egress allow-set; the page dials ws://.
        yield f"http://127.0.0.1:{srv.getsockname()[1]}"
    finally:
        stop.set()
        srv.close()
        thread.join(timeout=5)


@pytest.fixture
def ws_url() -> Iterator[str]:
    yield from _ws_serve()


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
        session.set_allowed(["http://127.0.0.1:3000"], cdn_preset=True)
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
        session.set_allowed(["http://127.0.0.1:3000"], cdn_preset=False)
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
        session.set_allowed([allowed_url], cdn_preset=False)
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
        session.set_allowed([allowed_url], cdn_preset=False)
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
        session.set_allowed([allowed_url], cdn_preset=False)
        with pytest.raises(Exception, match="ERR_BLOCKED_BY_CLIENT|blocked"):
            await session.page.goto(blocked_url)

    async def test_subresource_redirect_to_disallowed_origin_blocked(
        self, sessions: BrowserSessionManager, allowed_url: str, blocked_url: str
    ) -> None:
        # A SUBRESOURCE (img = non-document, manual fetch+fulfill path) whose src
        # 302s from the allowed origin to a blocked one: the gate follows the
        # chain itself, re-gates the hop, and aborts.  (Documents take continue_
        # and are the accepted redirect residual — see the residual test below.)
        from urllib.parse import quote

        session = await sessions.session(KEY, repo_name="app")
        session.set_allowed([allowed_url], cdn_preset=False)
        await session.page.goto(allowed_url)

        redirect_url = f"{allowed_url}/redirect?to={quote(blocked_url + '/index.html')}"
        await session.page.evaluate(
            """(u) => new Promise((res) => {
                const im = new Image();
                im.onload = () => res('loaded');
                im.onerror = () => res('error');
                im.src = u;
                setTimeout(() => res('timeout'), 4000);
            })""",
            redirect_url,
        )

        # §13: the blocked redirect TARGET (not the allowed hop) is recorded on
        # the per-repo aggregate — this is the path the "IdP refresh origin you
        # only discover by redirecting" use case depends on.  Its presence also
        # proves the gate aborted the hop rather than following it.
        assert normalize_origin(blocked_url) in session.blocked
        assert any(
            repo == "app" and stat.origin == normalize_origin(blocked_url)
            for repo, stat in sessions.blocked_origins("p")
        )

    async def test_document_redirect_to_disallowed_is_followed_residual(
        self, sessions: BrowserSessionManager, allowed_url: str, blocked_url: str
    ) -> None:
        # DOCUMENTED RESIDUAL (design §5): a DOCUMENT (top-level nav) redirect
        # from an allowed origin to a disallowed one IS followed un-gated.
        # Documents must load via continue_() so the page keeps a loopback IP
        # address space (a fulfilled document → Unknown space → Local Network
        # Access blocks every cross-service fetch/WebSocket).  A DIRECT
        # navigation to a disallowed origin is still blocked
        # (test_goto_disallowed_origin_blocked); this residual is bounded by the
        # dev-server subprocess already having ungated egress.
        from urllib.parse import quote

        session = await sessions.session(KEY)
        session.set_allowed([allowed_url], cdn_preset=False)
        await session.page.goto(allowed_url)

        redirect_url = f"{allowed_url}/redirect?to={quote(blocked_url + '/page2.html')}"
        await session.page.goto(redirect_url)
        assert await session.page.title() == "Page Two"  # followed to blocked origin

    async def test_navigation_redirect_within_allowed_origin_followed(
        self, sessions: BrowserSessionManager, allowed_url: str
    ) -> None:
        from urllib.parse import quote

        session = await sessions.session(KEY)
        session.set_allowed([allowed_url], cdn_preset=False)
        await session.page.goto(allowed_url)

        redirect_url = f"{allowed_url}/redirect?to={quote(allowed_url + '/page2.html')}"
        await session.page.goto(redirect_url)
        assert await session.page.title() == "Page Two"

    async def test_subresource_redirect_chain_over_cap_fails_closed(
        self, sessions: BrowserSessionManager, allowed_url: str
    ) -> None:
        # An endless same-origin redirect chain on a SUBRESOURCE (img) exceeds
        # the manual-follow hop cap.  The gate must ABORT (fail closed) rather
        # than fulfil a still-3xx response whose next Location was never gated.
        session = await sessions.session(KEY)
        session.set_allowed([allowed_url], cdn_preset=False)
        await session.page.goto(allowed_url)

        result = await session.page.evaluate(
            """(u) => new Promise((res) => {
                const im = new Image();
                im.onload = () => res('loaded');
                im.onerror = () => res('error');
                im.src = u;
                setTimeout(() => res('timeout'), 6000);
            })""",
            f"{allowed_url}/chain/0",
        )
        assert result == "error"  # gate aborted at the cap (no hang, no leak)

    async def test_snapshot_masks_password_value(
        self, sessions: BrowserSessionManager, allowed_url: str
    ) -> None:
        # The snapshot walker must not surface a password field's value (the
        # real a11y tree masks it) while still showing non-secret input values.
        session = await sessions.session(KEY)
        session.set_allowed([allowed_url], cdn_preset=False)
        await session.page.goto(f"{allowed_url}/pw.html")

        snapshot = await session.snapshot()
        assert "topsecret-APIKEY-42" not in snapshot
        assert "visible-username" in snapshot

    async def test_fetch_streaming_does_not_hang(
        self, sessions: BrowserSessionManager, allowed_url: str
    ) -> None:
        # A fetch-based endless stream must not be buffered by the gate: the
        # first chunk should arrive promptly (regression for the route.fetch
        # buffering hang).
        session = await sessions.session(KEY)
        session.set_allowed([allowed_url], cdn_preset=False)
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

    async def test_websocket_allowed_origin_is_forwarded(
        self, sessions: BrowserSessionManager, allowed_url: str, ws_url: str
    ) -> None:
        # An allowed WS origin → connect_to_server forwards to the real server,
        # so an app's WebSocket (e.g. Vite/Next HMR) actually works.
        session = await sessions.session(KEY)
        session.set_allowed([allowed_url, ws_url], cdn_preset=False)
        await session.page.goto(allowed_url)

        ws_origin = ws_url.replace("http://", "ws://")
        result = await session.page.evaluate(
            """(url) => new Promise((resolve) => {
                const ws = new WebSocket(url);
                ws.onopen = () => ws.send('ping');
                ws.onmessage = (e) => resolve(e.data);
                ws.onerror = () => resolve('error');
                ws.onclose = () => resolve('closed-before-message');
                setTimeout(() => resolve('timeout'), 5000);
            })""",
            f"{ws_origin}/",
        )
        assert result == "echo:ping"

    async def test_websocket_disallowed_origin_is_closed(
        self, sessions: BrowserSessionManager, allowed_url: str, ws_url: str
    ) -> None:
        # A WS to an origin NOT in the allow-set is closed by the gate (fail
        # closed) and recorded under the "websocket" resource type (§13).
        session = await sessions.session(KEY, repo_name="app")
        session.set_allowed([allowed_url], cdn_preset=False)  # ws_url NOT allowed
        await session.page.goto(allowed_url)

        ws_origin = ws_url.replace("http://", "ws://")
        result = await session.page.evaluate(
            """(url) => new Promise((resolve) => {
                const ws = new WebSocket(url);
                ws.onmessage = (e) => resolve('message:' + e.data);
                ws.onerror = () => resolve('blocked');
                ws.onclose = () => resolve('blocked');
                setTimeout(() => resolve('timeout'), 5000);
            })""",
            f"{ws_origin}/",
        )
        assert result == "blocked"
        assert normalize_origin(ws_url) in session.blocked
        assert "websocket" in session.blocked[normalize_origin(ws_url)].resource_types

    async def test_webrtc_and_webtransport_are_blocked(
        self, sessions: BrowserSessionManager, allowed_url: str
    ) -> None:
        # Non-HTTP egress channels are neutered before page scripts run, so an
        # agent-authored page cannot open them to reach an external host.
        session = await sessions.session(KEY)
        session.set_allowed([allowed_url], cdn_preset=False)
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
        session.set_allowed([allowed_url], cdn_preset=False)
        await session.page.goto(allowed_url)
        assert "hello-from-page" in session.console_tail()

    async def test_close_for_epic_closes_page(
        self, sessions: BrowserSessionManager, allowed_url: str
    ) -> None:
        session = await sessions.session(KEY)
        session.set_allowed([allowed_url], cdn_preset=False)
        await session.page.goto(allowed_url)

        await sessions.close_for_epic("p", "e1")
        assert session.page.is_closed()

        # A fresh session is created on next use.
        session2 = await sessions.session(KEY)
        assert session2 is not session
