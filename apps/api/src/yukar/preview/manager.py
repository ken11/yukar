"""Host-managed dev servers for agent browser verification.

The host — never an agent — launches the services a user declared in
``Repo.dev_server`` (docs/browser-verification-design.md §3).  Agents only
receive the resulting facts (origins, readiness, log tails) through the
browser tool bundle; they hold no capability to start processes themselves.

Registry key: ``TrialKey(project_id, epic_id, trial_id, repo_name)`` — one
entry per trial worktree, so parallel trials of the same repo never share a
server or a port.  Services inside an entry are keyed by service name and
start in declared order, each awaiting readiness before the next launches.

Lifecycle: created once in the app lifespan (``init_dev_server_manager``),
started lazily on first browser-tool use (``ensure``), stopped on run end
(orchestrator ``finally``), trial archive / repo prune, the agent's explicit
``server_stop`` tool, and app shutdown (``stop_all``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import socket
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx

from yukar.models.project import DevServerConfig, DevService
from yukar.sandbox.env import build_subprocess_env

logger = logging.getLogger(__name__)

_LOG_CAP_BYTES = 256 * 1024  # combined stdout+stderr ring buffer per service
_LOG_TAIL_DEFAULT_LINES = 100
_PORT_SCAN_SPAN = 200  # how far above base_port the free-port search goes
_READY_POLL_SECONDS = 0.25
_TERM_GRACE_SECONDS = 5.0

_PORT_PLACEHOLDER_RE = re.compile(r"\{port(?::([A-Za-z0-9][A-Za-z0-9_-]*))?\}")


class DevServerError(RuntimeError):
    """A service failed to launch, resolve its config, or become ready."""


def _signal_process_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Send *sig* to the child's whole process group, falling back to the child.

    ``start_new_session=True`` makes the child a group leader (pgid == pid), so
    signalling the group reaps grandchildren too.  Best-effort: the process may
    already be gone, or the platform may lack ``killpg`` — never raises.
    """
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        with contextlib.suppress(ProcessLookupError):
            if sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()


@dataclass(frozen=True, slots=True)
class TrialKey:
    project_id: str
    epic_id: str
    trial_id: str
    repo_name: str


ServiceState = Literal["starting", "ready", "failed", "stopped"]


@dataclass
class ServiceHandle:
    """One running (or failed) service process plus its captured output."""

    config: DevService
    port: int
    state: ServiceState = "starting"
    proc: asyncio.subprocess.Process | None = None
    error: str | None = None
    _log: deque[bytes] = field(default_factory=deque, repr=False)
    _log_bytes: int = 0
    _readers: list[asyncio.Task[None]] = field(default_factory=list, repr=False)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def _append_log(self, chunk: bytes) -> None:
        self._log.append(chunk)
        self._log_bytes += len(chunk)
        while self._log_bytes > _LOG_CAP_BYTES and self._log:
            self._log_bytes -= len(self._log.popleft())

    def log_tail(self, max_lines: int = _LOG_TAIL_DEFAULT_LINES) -> str:
        text = b"".join(self._log).decode("utf-8", errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[-max_lines:])


def resolve_port_placeholders(text: str, ports: dict[str, int], own_service: str) -> str:
    """Substitute ``{port}`` / ``{port:name}`` in a command token or env value.

    Raises:
        DevServerError: When a placeholder names a service that does not exist.
    """

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1) or own_service
        if name not in ports:
            raise DevServerError(f"Unknown service in port placeholder: {name!r}")
        return str(ports[name])

    return _PORT_PLACEHOLDER_RE.sub(_sub, text)


class DevServerManager:
    """Keyed registry of per-trial dev server processes (RepoWatcher pattern)."""

    def __init__(self, *, max_services: int = 8) -> None:
        self._entries: dict[TrialKey, dict[str, ServiceHandle]] = {}
        self._locks: dict[TrialKey, asyncio.Lock] = {}
        self._reserved_ports: set[int] = set()
        # Guards the read-then-reserve of _reserved_ports: ensure() holds only
        # a PER-KEY lock, so two ensure()s for different trials could otherwise
        # bind-probe and pick the same free port before either reserved it.
        self._alloc_lock = asyncio.Lock()
        self._max_services = max_services
        self._closed = False

    # ------------------------------------------------------------------
    # Ensure (lazy, idempotent start)
    # ------------------------------------------------------------------

    async def ensure(
        self,
        key: TrialKey,
        config: DevServerConfig,
        worktree: Path,
    ) -> dict[str, ServiceHandle]:
        """Start the declared services for *key* unless they are already ready.

        Idempotent: a healthy entry is returned as-is; a dead or failed one is
        torn down and relaunched from scratch.  On any launch/readiness
        failure the whole entry is stopped and DevServerError is raised with
        the failing service's log tail.
        """
        if self._closed:
            raise DevServerError("Dev server manager is shut down.")
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._entries.get(key)
            if entry is not None:
                if all(h.state == "ready" and h.is_alive for h in entry.values()):
                    return entry
                await self._stop_entry(entry)
                self._entries.pop(key, None)
                self._reserved_ports.difference_update(h.port for h in entry.values())

            alive_total = sum(
                1 for e in self._entries.values() for h in e.values() if h.is_alive
            )
            if alive_total + len(config.services) > self._max_services:
                raise DevServerError(
                    f"Dev server capacity exceeded ({alive_total} running, "
                    f"max {self._max_services})"
                )

            worktree = worktree.resolve()
            if not worktree.is_dir():
                raise DevServerError(f"Worktree does not exist: {worktree}")

            # Assign every port up-front so {port:name} can reference services
            # later in the list as well as earlier ones.  Reserve each port the
            # instant it is chosen (under _alloc_lock) so a concurrent ensure()
            # for another trial cannot pick the same one.
            ports: dict[str, int] = {}
            async with self._alloc_lock:
                for svc in config.services:
                    port = self._allocate_port(svc.base_port, taken=set(ports.values()))
                    ports[svc.name] = port
                    self._reserved_ports.add(port)

            entry = {}
            self._entries[key] = entry
            try:
                for svc in config.services:
                    handle = ServiceHandle(config=svc, port=ports[svc.name])
                    entry[svc.name] = handle
                    await self._start_service(handle, ports, worktree)
            except BaseException:
                await self._stop_entry(entry)
                self._entries.pop(key, None)
                self._reserved_ports.difference_update(ports.values())
                raise
            return entry

    def get_entry(self, key: TrialKey) -> dict[str, ServiceHandle] | None:
        return self._entries.get(key)

    def origins(self, key: TrialKey) -> list[str]:
        """Origins of this trial's services — the browser egress allow-set."""
        entry = self._entries.get(key)
        if not entry:
            return []
        return [h.origin for h in entry.values()]

    def log_tail(self, key: TrialKey, service: str | None = None, max_lines: int = 100) -> str:
        """Combined log tail for one service, or all services labelled."""
        entry = self._entries.get(key)
        if not entry:
            return ""
        if service is not None:
            handle = entry.get(service)
            return handle.log_tail(max_lines) if handle else ""
        parts = [
            f"=== {name} ({h.state}) ===\n{h.log_tail(max_lines)}" for name, h in entry.items()
        ]
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Stop paths
    # ------------------------------------------------------------------

    async def stop(self, key: TrialKey) -> bool:
        """Stop and forget one entry. Returns False when nothing was running.

        Takes the same per-key lock as ensure() so a concurrent launch can
        never race the teardown and leave orphaned children (the lock itself
        is kept — it is tiny and bounded by the number of trials seen).
        """
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._entries.pop(key, None)
            if entry is None:
                return False
            self._reserved_ports.difference_update(h.port for h in entry.values())
            await self._stop_entry(entry)
            return True

    async def stop_for_trial(self, project_id: str, epic_id: str, trial_id: str) -> int:
        """Stop every repo entry belonging to one trial (archive / prune)."""
        keys = [
            k
            for k in self._entries
            if k.project_id == project_id and k.epic_id == epic_id and k.trial_id == trial_id
        ]
        for k in keys:
            await self.stop(k)
        return len(keys)

    async def stop_for_epic(self, project_id: str, epic_id: str) -> int:
        """Stop every entry belonging to one epic (run-end hook)."""
        keys = [
            k for k in self._entries if k.project_id == project_id and k.epic_id == epic_id
        ]
        for k in keys:
            await self.stop(k)
        return len(keys)

    async def stop_all(self) -> None:
        """App shutdown: stop everything and refuse later launches.

        A run task that is still winding down while the lifespan tears us
        down must not respawn services — with ``start_new_session=True`` such
        a child would outlive the host.
        """
        self._closed = True
        keys = list(self._entries)
        for k in keys:
            await self.stop(k)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _allocate_port(self, base_port: int, *, taken: set[int]) -> int:
        """Find a free port at or above *base_port* (linear scan, bind probe)."""
        for candidate in range(base_port, min(base_port + _PORT_SCAN_SPAN, 65536)):
            if candidate in taken or candidate in self._reserved_ports:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("127.0.0.1", candidate))
                except OSError:
                    continue
            return candidate
        raise DevServerError(f"No free port found in [{base_port}, {base_port + _PORT_SCAN_SPAN})")

    async def _start_service(
        self,
        handle: ServiceHandle,
        ports: dict[str, int],
        worktree: Path,
    ) -> None:
        svc = handle.config

        cwd = (worktree / svc.cwd).resolve()
        if not cwd.is_relative_to(worktree):
            raise DevServerError(f"Service cwd escapes the worktree: {svc.cwd!r}")
        if not cwd.is_dir():
            raise DevServerError(f"Service cwd does not exist: {svc.cwd!r}")

        argv = [resolve_port_placeholders(tok, ports, svc.name) for tok in svc.command]
        # PORT is injected LAST so a user-declared env named PORT can never
        # shadow the host-assigned port — otherwise the child would bind the
        # user's value while readiness probes (and the browser) target the
        # allocated one, and per-trial port isolation would break.
        extra_env = {
            **{k: resolve_port_placeholders(v, ports, svc.name) for k, v in svc.env.items()},
            "PORT": str(handle.port),
        }
        env = build_subprocess_env(cwd=cwd, extra=extra_env)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            handle.state = "failed"
            handle.error = str(exc)
            raise DevServerError(f"Failed to launch service {svc.name!r}: {exc}") from exc

        handle.proc = proc
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                task = asyncio.create_task(self._pump_stream(stream, handle))
                handle._readers.append(task)  # noqa: SLF001 — own dataclass

        await self._await_ready(handle)

    @staticmethod
    async def _pump_stream(stream: asyncio.StreamReader, handle: ServiceHandle) -> None:
        with contextlib.suppress(Exception):
            while True:
                chunk = await stream.read(8192)
                if not chunk:
                    return
                handle._append_log(chunk)  # noqa: SLF001 — own dataclass

    async def _await_ready(self, handle: ServiceHandle) -> None:
        svc = handle.config
        deadline = asyncio.get_running_loop().time() + svc.readiness.timeout_seconds
        port_open = False
        while asyncio.get_running_loop().time() < deadline:
            if not handle.is_alive:
                # Let the pump tasks drain the EOF'd pipes so the error can
                # carry the child's actual output.
                if handle._readers:  # noqa: SLF001 — own dataclass
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            asyncio.gather(*handle._readers, return_exceptions=True),  # noqa: SLF001
                            timeout=1.0,
                        )
                code = handle.proc.returncode if handle.proc else None
                handle.state = "failed"
                handle.error = f"exited with code {code} before becoming ready"
                raise DevServerError(
                    f"Service {svc.name!r} exited (code {code}) before becoming ready.\n"
                    f"--- log tail ---\n{handle.log_tail(40)}"
                )

            if not port_open:
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", handle.port)
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
                    port_open = True
                except OSError:
                    await asyncio.sleep(_READY_POLL_SECONDS)
                    continue

            if svc.readiness.path is None:
                handle.state = "ready"
                return

            url = f"{handle.origin}{svc.readiness.path}"
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(url)
                if resp.status_code < 400:
                    handle.state = "ready"
                    return
            except httpx.InvalidURL as exc:
                # A malformed readiness URL can never succeed — fail fast with a
                # clean DevServerError instead of looping to the timeout (and
                # note InvalidURL is NOT an httpx.HTTPError, so it would
                # otherwise escape uncaught).
                handle.state = "failed"
                handle.error = f"invalid readiness URL {url!r}: {exc}"
                raise DevServerError(
                    f"Service {svc.name!r} has an invalid readiness path "
                    f"{svc.readiness.path!r}: {exc}"
                ) from exc
            except httpx.HTTPError:
                pass
            await asyncio.sleep(_READY_POLL_SECONDS)

        handle.state = "failed"
        handle.error = f"not ready within {svc.readiness.timeout_seconds}s"
        raise DevServerError(
            f"Service {svc.name!r} did not become ready within "
            f"{svc.readiness.timeout_seconds}s.\n--- log tail ---\n{handle.log_tail(40)}"
        )

    async def _stop_entry(self, entry: dict[str, ServiceHandle]) -> None:
        for handle in entry.values():
            await self._stop_service(handle)

    @staticmethod
    async def _stop_service(handle: ServiceHandle) -> None:
        proc = handle.proc
        if proc is not None and proc.returncode is None:
            # SIGTERM the whole group first so dev servers shut down cleanly
            # and release their ports; escalate to SIGKILL after a grace
            # period.  start_new_session=True makes pid == pgid.
            _signal_process_group(proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_SECONDS)
            except TimeoutError:
                _signal_process_group(proc, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await proc.wait()
        for task in handle._readers:  # noqa: SLF001 — own dataclass
            task.cancel()
        if handle._readers:  # noqa: SLF001
            await asyncio.gather(*handle._readers, return_exceptions=True)  # noqa: SLF001
        handle._readers.clear()  # noqa: SLF001
        if handle.state != "failed":
            handle.state = "stopped"


# ---------------------------------------------------------------------------
# Module-level singleton (init in app lifespan — mirrors usage.tracker)
# ---------------------------------------------------------------------------

_manager: DevServerManager | None = None


def init_dev_server_manager(manager: DevServerManager | None) -> None:
    """Install (or clear, with None) the process-wide DevServerManager."""
    global _manager  # noqa: PLW0603
    _manager = manager


def get_dev_server_manager() -> DevServerManager | None:
    """Return the process-wide manager, or None outside a running app."""
    return _manager
