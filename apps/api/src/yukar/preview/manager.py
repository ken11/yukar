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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx

from yukar.models.project import DevServerConfig, DevService
from yukar.preview.envfile import EnvFileError, parse_env_file, resolve_env_file_path
from yukar.sandbox.env import build_subprocess_env

logger = logging.getLogger(__name__)

_LOG_CAP_BYTES = 256 * 1024  # combined stdout+stderr ring buffer per service
_LOG_TAIL_DEFAULT_LINES = 100
_PORT_SCAN_SPAN = 200  # how far above base_port the free-port search goes
_READY_POLL_SECONDS = 0.25
_TERM_GRACE_SECONDS = 5.0

# Loopback families a dev server may bind, probed in this order at readiness.
# A server that binds `localhost` resolves to IPv6 ``::1`` on many systems
# (notably macOS), so a hard-coded IPv4 ``127.0.0.1`` target would be REFUSED
# even though the server is up.  We detect which family actually accepts a
# connection and address the browser + readiness probe at THAT family, so the
# navigation target always matches where the child is really listening.
_LOOPBACK_PROBE_HOSTS: tuple[str, ...] = ("127.0.0.1", "::1")


async def _first_reachable_loopback(port: int) -> str | None:
    """Return the first loopback host accepting a TCP connection on *port*.

    Tries IPv4 then IPv6 (see :data:`_LOOPBACK_PROBE_HOSTS`).  ``None`` when
    neither accepts yet — the caller keeps polling until the deadline.
    """
    for host in _LOOPBACK_PROBE_HOSTS:
        try:
            _, writer = await asyncio.open_connection(host, port)
        except OSError:
            continue
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return host
    return None

# {port} / {port:service} / {port:repo/service} — group 1 carries the
# reference ("service" or "repo/service"); bare {port} leaves it None.
# The inside is matched LOOSELY on purpose: repo names have no charset
# constraint (dots/spaces are legal — "next.js", "example.com"), and a strict
# class here would make such references silently unmatchable — passing the
# save-time validators AND the launch-time resolver, handing the literal
# placeholder text to the child.  Anything inside {port:...} must therefore
# either resolve or fail loudly.
_PORT_PLACEHOLDER_RE = re.compile(r"\{port(?::([^{}]+))?\}")


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
    # Loopback family the child was found listening on (set at readiness).
    # Defaults to IPv4; becomes "::1" when the server bound IPv6-only.
    host: str = "127.0.0.1"
    proc: asyncio.subprocess.Process | None = None
    error: str | None = None
    _log: deque[bytes] = field(default_factory=deque, repr=False)
    _log_bytes: int = 0
    _readers: list[asyncio.Task[None]] = field(default_factory=list, repr=False)

    @property
    def origin(self) -> str:
        # Bracket IPv6 literals so the URL is well-formed (http://[::1]:PORT).
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    @property
    def browser_origin(self) -> str:
        # What the BROWSER navigates to.  Chromium resolves ``localhost``
        # itself (RFC 6761) and connects to whichever loopback family answers,
        # so this spelling works regardless of which family the child bound —
        # while cookies, captured auth state, and the absolute URLs apps bake
        # into redirects stay on the ``localhost`` host users configure their
        # apps around.  The numeric :attr:`origin` remains the form for the
        # readiness probe (httpx must target the family that is actually
        # listening) and for allow-set entries (canonicalised anyway).
        return f"http://localhost:{self.port}"

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
    """Substitute ``{port}`` / ``{port:name}`` / ``{port:repo/name}`` in a token.

    *ports* maps plain service names of the launching repo plus qualified
    ``repo/service`` keys for every dependency repo already launched.

    Raises:
        DevServerError: When a placeholder names a service that does not exist.
    """

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1) or own_service
        if name not in ports:
            raise DevServerError(f"Unknown service in port placeholder: {name!r}")
        return str(ports[name])

    return _PORT_PLACEHOLDER_RE.sub(_sub, text)


def unknown_port_references(config: DevServerConfig) -> list[str]:
    """Unqualified ``{port:name}`` references to services not declared in *config*.

    Save-time feedback: an undeclared reference would otherwise surface as a
    launch error in the middle of an agent turn.  Scans command tokens and
    literal env values — the two places ``resolve_port_placeholders`` applies
    at launch.  Qualified ``{port:repo/service}`` references resolve against
    OTHER repos and are validated separately (see
    :func:`cross_repo_port_references`).
    """
    declared = {s.name for s in config.services}
    problems: list[str] = []
    for svc in config.services:
        for text in [*svc.command, *svc.env.values()]:
            for match in _PORT_PLACEHOLDER_RE.finditer(text):
                name = match.group(1)
                if name is not None and "/" not in name and name not in declared:
                    problems.append(
                        f"Service {svc.name!r} references unknown service {name!r} in "
                        f"{match.group(0)!r} — only services declared in this repo's "
                        f"config are resolvable (declared: {sorted(declared)})"
                    )
    return problems


def cross_repo_port_references(config: DevServerConfig) -> set[tuple[str, str]]:
    """``(repo, service)`` pairs referenced as ``{port:repo/service}`` in *config*."""
    refs: set[tuple[str, str]] = set()
    for svc in config.services:
        for text in [*svc.command, *svc.env.values()]:
            for match in _PORT_PLACEHOLDER_RE.finditer(text):
                name = match.group(1)
                if name is not None and "/" in name:
                    repo, _, service = name.partition("/")
                    refs.add((repo, service))
    return refs


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
        # external_ports snapshot each entry was launched with — a dependency
        # repo relaunched on a different port must invalidate the dependents
        # whose env/argv baked in the old number.
        self._external_ports: dict[TrialKey, dict[str, int]] = {}

    # ------------------------------------------------------------------
    # Ensure (lazy, idempotent start)
    # ------------------------------------------------------------------

    async def ensure(
        self,
        key: TrialKey,
        config: DevServerConfig,
        worktree: Path,
        *,
        repo_root: Path | None = None,
        external_ports: dict[str, int] | None = None,
        launched_out: list[TrialKey] | None = None,
    ) -> dict[str, ServiceHandle]:
        """Start the declared services for *key* unless they are already ready.

        Idempotent: a healthy entry is returned as-is; a dead or failed one is
        torn down and relaunched from scratch.  On any launch/readiness
        failure the whole entry is stopped and DevServerError is raised with
        the failing service's log tail.

        Args:
            key: Registry key (one entry per trial worktree per repo).
            config: The repo's declared dev-server config.
            worktree: Tree the services run in (trial worktree or base checkout).
            repo_root: The repo's BASE checkout — anchor for repo-relative
                ``env_file`` declarations (§11).  ``None`` makes a relative
                declaration a launch error; absolute/``~`` paths still work.
            external_ports: Qualified ``repo/service`` → port entries of
                dependency repos already launched (``{port:repo/service}``
                resolution).  Callers with cross-repo references use
                :func:`ensure_with_dependencies`, which computes this.
            launched_out: When given, this key is appended iff THIS call
                actually (re)launches processes — a healthy reuse appends
                nothing.  Decided under the per-key lock, so callers get an
                exact record for failure cleanup (no get_entry TOCTOU).
        """
        if self._closed:
            raise DevServerError("Dev server manager is shut down.")
        external = dict(external_ports or {})
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._entries.get(key)
            if entry is not None:
                # Reuse only a fully-healthy entry whose service SET still matches
                # the config.  An edited dev_server config (service renamed / added
                # / removed) must relaunch — otherwise open_app would index the
                # reused entry with a name the running process never bound, raising
                # KeyError instead of serving the new config.  A changed
                # external-ports snapshot must relaunch too: the old processes
                # baked a dependency's stale port into their env/argv.
                healthy = all(h.state == "ready" and h.is_alive for h in entry.values())
                same_services = set(entry) == {s.name for s in config.services}
                same_external = self._external_ports.get(key, {}) == external
                if healthy and same_services and same_external:
                    return entry
                await self._stop_entry(entry)
                self._entries.pop(key, None)
                self._external_ports.pop(key, None)
                self._reserved_ports.difference_update(h.port for h in entry.values())

            # Past the reuse branch — this call WILL launch (or die trying).
            if launched_out is not None:
                launched_out.append(key)

            worktree = worktree.resolve()
            if not worktree.is_dir():
                raise DevServerError(f"Worktree does not exist: {worktree}")

            # Capacity check AND port reservation happen together under
            # _alloc_lock so a concurrent ensure() for another trial cannot both
            # pass a stale capacity snapshot (TOCTOU over the soft _max_services
            # cap) nor grab the same port.  Assign every port up-front so
            # {port:name} can reference services later in the list too.  On any
            # failure while reserving, release everything reserved so far — a
            # multi-service config whose 2nd allocation fails must not leak the
            # 1st service's reservation until process restart.
            ports: dict[str, int] = {}
            async with self._alloc_lock:
                alive_total = sum(
                    1 for e in self._entries.values() for h in e.values() if h.is_alive
                )
                if alive_total + len(config.services) > self._max_services:
                    raise DevServerError(
                        f"Dev server capacity exceeded ({alive_total} running, "
                        f"max {self._max_services})"
                    )
                try:
                    for svc in config.services:
                        port = self._allocate_port(svc.base_port, taken=set(ports.values()))
                        ports[svc.name] = port
                        self._reserved_ports.add(port)
                except BaseException:
                    self._reserved_ports.difference_update(ports.values())
                    raise

            # Resolution map for {port:...}: own plain names win over external
            # qualified keys (they cannot collide — qualified keys contain "/"),
            # and the repo's own services are also reachable via their
            # qualified alias so {port:this-repo/svc} works uniformly.
            resolution = {
                **external,
                **ports,
                **{f"{key.repo_name}/{name}": port for name, port in ports.items()},
            }
            entry = {}
            self._entries[key] = entry
            try:
                for svc in config.services:
                    handle = ServiceHandle(config=svc, port=ports[svc.name])
                    entry[svc.name] = handle
                    await self._start_service(handle, resolution, worktree, repo_root)
            except BaseException:
                await self._stop_entry(entry)
                self._entries.pop(key, None)
                self._reserved_ports.difference_update(ports.values())
                raise
            self._external_ports[key] = external
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
            self._external_ports.pop(key, None)
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
        repo_root: Path | None,
    ) -> None:
        svc = handle.config

        cwd = (worktree / svc.cwd).resolve()
        if not cwd.is_relative_to(worktree):
            raise DevServerError(f"Service cwd escapes the worktree: {svc.cwd!r}")
        if not cwd.is_dir():
            raise DevServerError(f"Service cwd does not exist: {svc.cwd!r}")

        argv = [resolve_port_placeholders(tok, ports, svc.name) for tok in svc.command]
        # Secrets by SOURCE (§11): env_file(s) then env_passthrough resolve at
        # launch time, and the values reach only this child process.  A missing
        # file or unset variable fails the launch LOUDLY — a silently absent
        # secret would surface as an inexplicable 500 during verification.
        extra_env: dict[str, str] = {}
        for decl in svc.env_file:
            try:
                path = resolve_env_file_path(decl, repo_root)
                extra_env.update(await asyncio.to_thread(parse_env_file, path))
            except EnvFileError as exc:
                raise DevServerError(f"Service {svc.name!r}: {exc}") from exc
        for var in svc.env_passthrough:
            value = os.environ.get(var)
            if value is None:
                raise DevServerError(
                    f"Service {svc.name!r}: env_passthrough variable {var!r} is not set "
                    "in the yukar server environment"
                )
            extra_env[var] = value
        # Literal env last (explicit declaration wins over file/passthrough);
        # env_file values stay VERBATIM — port placeholders apply to literals only.
        extra_env.update(
            {k: resolve_port_placeholders(v, ports, svc.name) for k, v in svc.env.items()}
        )
        # PORT is injected LAST so a user-declared env named PORT can never
        # shadow the host-assigned port — otherwise the child would bind the
        # user's value while readiness probes (and the browser) target the
        # allocated one, and per-trial port isolation would break.
        extra_env["PORT"] = str(handle.port)
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
                # Detect which loopback family the child actually bound and
                # pin the handle's host to it — so readiness, the egress
                # allow-set, and the browser navigation all target the address
                # the server is really listening on (fixes ERR_CONNECTION_REFUSED
                # when the dev server binds IPv6 ``::1`` but we assume IPv4).
                reachable = await _first_reachable_loopback(handle.port)
                if reachable is None:
                    await asyncio.sleep(_READY_POLL_SECONDS)
                    continue
                handle.host = reachable
                port_open = True

            if svc.readiness.path is None:
                handle.state = "ready"
                return

            url = f"{handle.origin}{svc.readiness.path}"
            try:
                # trust_env=False: the probe targets the child's loopback origin.
                # Honouring HTTP_PROXY/ALL_PROXY from the environment that
                # launched `yukar serve` would route 127.0.0.1 through a corporate
                # proxy that cannot reach it, failing readiness on a healthy server.
                async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
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
# Cross-repo launch orchestration
# ---------------------------------------------------------------------------

RepoConfigLoader = Callable[[str], Awaitable["tuple[DevServerConfig, Path] | None"]]
"""repo name → (dev-server config, base checkout path), or None when unknown."""

RepoTreeResolver = Callable[[str, Path], Awaitable["tuple[TrialKey, Path]"]]
"""(dep repo name, its base checkout) → (registry key, tree to launch in)."""


async def ensure_with_dependencies(
    manager: DevServerManager,
    repo_name: str,
    config: DevServerConfig,
    *,
    key: TrialKey,
    tree: Path,
    repo_root: Path,
    load_config: RepoConfigLoader,
    resolve_tree: RepoTreeResolver,
) -> dict[str, dict[str, ServiceHandle]]:
    """Launch *repo_name*'s services plus every repo it references, deps first.

    ``{port:repo/service}`` references define the cross-repo dependency order:
    a referenced repo's services are ensured (launched and awaited ready)
    BEFORE the referencing repo starts, and their real ports are handed to the
    dependent launch as ``external_ports``.  The walk is transitive and
    rejects cycles.

    Returns:
        repo name → its service entry, for every repo launched (the target
        repo and all its dependencies).

    Raises:
        DevServerError: Unknown/unconfigured dependency repo, a dependency
            cycle, or any launch failure.
    """
    configs: dict[str, tuple[DevServerConfig, Path]] = {repo_name: (config, repo_root)}
    order: list[str] = []  # dependencies first, target repo last
    done: set[str] = set()

    async def _visit(repo: str, trail: tuple[str, ...]) -> None:
        if repo in done:
            return
        if repo in trail:
            cycle = " -> ".join((*trail[trail.index(repo) :], repo))
            raise DevServerError(f"Circular dev-server dependency between repos: {cycle}")
        if repo not in configs:
            loaded = await load_config(repo)
            if loaded is None:
                raise DevServerError(
                    f"{{port:{repo}/...}} references repo {repo!r}, which is not "
                    "registered in this project or has no dev-server config"
                )
            configs[repo] = loaded
        cfg = configs[repo][0]
        for dep in sorted({r for r, _service in cross_repo_port_references(cfg)} - {repo}):
            await _visit(dep, (*trail, repo))
        done.add(repo)
        order.append(repo)

    await _visit(repo_name, ())

    qualified: dict[str, int] = {}
    entries: dict[str, dict[str, ServiceHandle]] = {}
    launched: list[TrialKey] = []  # keys THIS call brought up (exact — see ensure)
    try:
        for repo in order:
            cfg, base = configs[repo]
            if repo == repo_name:
                repo_key, repo_tree = key, tree
            else:
                repo_key, repo_tree = await resolve_tree(repo, base)
            # Hand each repo ONLY the qualified ports it actually references.
            # Passing the full accumulated map would make the same entry's
            # external_ports snapshot depend on the ENTRY POINT (opening Z
            # vs opening its dependency B directly), spuriously failing the
            # same_external reuse check and relaunching a healthy server out
            # from under its dependents.
            refs = cross_repo_port_references(cfg)
            external = {
                ref_key: qualified[ref_key]
                for ref_repo, ref_service in sorted(refs)
                if (ref_key := f"{ref_repo}/{ref_service}") in qualified
            }
            entry = await manager.ensure(
                repo_key,
                cfg,
                repo_tree,
                repo_root=base,
                external_ports=external,
                launched_out=launched,
            )
            entries[repo] = entry
            for name, handle in entry.items():
                qualified[f"{repo}/{name}"] = handle.port
    except BaseException:
        # A later launch failing must not leak what THIS call started —
        # including a dead entry it relaunched.  Reused healthy entries are
        # never in `launched`, so another agent's servers stay untouched.
        for repo_key in reversed(launched):
            with contextlib.suppress(Exception):
                await manager.stop(repo_key)
        raise
    return entries


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
