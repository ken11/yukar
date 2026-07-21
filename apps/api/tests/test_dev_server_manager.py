"""DevServerManager — real-process launch, readiness, ports, logs, stop paths.

Services are tiny ``python -c`` scripts (sys.executable) so the tests are
hermetic and fast: no network beyond 127.0.0.1, no external binaries.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

import pytest

from yukar.models.project import DevServerConfig, DevService, ServiceReadiness
from yukar.preview.manager import (
    DevServerError,
    DevServerManager,
    TrialKey,
    cross_repo_port_references,
    ensure_with_dependencies,
    resolve_port_placeholders,
    unknown_port_references,
)

KEY = TrialKey(project_id="p", epic_id="e1", trial_id="t1", repo_name="app")

# Binds PORT from env, prints a marker, then idles.
_SERVE = (
    "import os,socket,time\n"
    "port=int(os.environ['PORT'])\n"
    "s=socket.create_server(('127.0.0.1',port))\n"
    "print('listening on',port,flush=True)\n"
    "time.sleep(120)\n"
)


def _service(**overrides: object) -> DevService:
    base: dict[str, object] = {
        "name": "web",
        "command": [sys.executable, "-c", _SERVE],
        "base_port": 42800,
    }
    base.update(overrides)
    return DevService.model_validate(base)


def _config(*services: DevService) -> DevServerConfig:
    return DevServerConfig(services=list(services) if services else [_service()])


@pytest.fixture
def manager() -> DevServerManager:
    return DevServerManager()


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    return tmp_path


# ---------------------------------------------------------------------------
# Placeholder resolution (pure)
# ---------------------------------------------------------------------------


class TestPortPlaceholders:
    def test_own_port(self) -> None:
        assert resolve_port_placeholders("--port={port}", {"web": 3000}, "web") == "--port=3000"

    def test_sibling_port(self) -> None:
        out = resolve_port_placeholders(
            "http://127.0.0.1:{port:api}", {"web": 3000, "api": 8000}, "web"
        )
        assert out == "http://127.0.0.1:8000"

    def test_multiple_in_one_token(self) -> None:
        out = resolve_port_placeholders("{port}:{port:api}", {"web": 1, "api": 2}, "web")
        assert out == "1:2"

    def test_unknown_service_rejected(self) -> None:
        with pytest.raises(DevServerError, match="ghost"):
            resolve_port_placeholders("{port:ghost}", {"web": 3000}, "web")

    def test_plain_text_untouched(self) -> None:
        assert resolve_port_placeholders("pnpm dev", {"web": 1}, "web") == "pnpm dev"


class TestUnknownPortReferences:
    """Save-time scan for {port:name} references outside this config."""

    def test_valid_cross_reference_ok(self) -> None:
        config = _config(
            _service(name="api", base_port=42840),
            _service(
                name="web",
                base_port=42850,
                env={"API_URL": "http://127.0.0.1:{port:api}"},
            ),
        )
        assert unknown_port_references(config) == []

    def test_unknown_reference_in_env_reported(self) -> None:
        config = _config(
            _service(name="web", base_port=42850, env={"API_URL": "http://x:{port:api}"})
        )
        problems = unknown_port_references(config)
        assert len(problems) == 1
        assert "'api'" in problems[0]
        assert "'web'" in problems[0]

    def test_unknown_reference_in_command_reported(self) -> None:
        svc = _service(
            name="web", base_port=42850, command=["serve", "--upstream", "{port:backend}"]
        )
        problems = unknown_port_references(_config(svc))
        assert len(problems) == 1
        assert "'backend'" in problems[0]

    def test_bare_port_placeholder_ok(self) -> None:
        svc = _service(name="web", base_port=42850, env={"SELF": "http://x:{port}"})
        assert unknown_port_references(_config(svc)) == []

    def test_qualified_reference_not_reported_here(self) -> None:
        # {port:repo/service} resolves against ANOTHER repo — validated at the
        # project level, not against this config's own service names.
        svc = _service(name="web", base_port=42850, env={"API": "http://x:{port:backend/api}"})
        assert unknown_port_references(_config(svc)) == []


class TestCrossRepoReferences:
    def test_qualified_refs_extracted(self) -> None:
        svc = _service(
            name="web",
            base_port=42850,
            command=["serve", "--upstream", "{port:backend/api}"],
            env={"OTHER": "http://x:{port:auth/idp}"},
        )
        assert cross_repo_port_references(_config(svc)) == {("backend", "api"), ("auth", "idp")}

    def test_bare_and_unqualified_ignored(self) -> None:
        svc = _service(name="web", base_port=42850, env={"SELF": "{port}:{port:web}"})
        assert cross_repo_port_references(_config(svc)) == set()

    def test_qualified_resolution(self) -> None:
        out = resolve_port_placeholders("{port:backend/api}", {"backend/api": 4321}, "web")
        assert out == "4321"

    def test_dotted_repo_name_matches(self) -> None:
        # Repo names carry no charset constraint ("next.js", "example.com") —
        # the placeholder must match them, not silently pass the text through.
        svc = _service(name="web", base_port=42850, env={"API": "http://x:{port:next.js/api}"})
        assert cross_repo_port_references(_config(svc)) == {("next.js", "api")}
        out = resolve_port_placeholders("{port:next.js/api}", {"next.js/api": 4321}, "web")
        assert out == "4321"

    def test_loose_reference_fails_loudly_at_resolution(self) -> None:
        # Anything inside {port:...} must resolve or raise — never pass through
        # as literal text into the child's env/argv.
        with pytest.raises(DevServerError, match="foo bar"):
            resolve_port_placeholders("{port:foo bar}", {"web": 3000}, "web")

    def test_unqualified_loose_reference_reported_at_save(self) -> None:
        svc = _service(name="web", base_port=42850, env={"X": "http://x:{port: api}"})
        problems = unknown_port_references(_config(svc))
        assert len(problems) == 1
        assert "' api'" in problems[0]


# ---------------------------------------------------------------------------
# Port allocation
# ---------------------------------------------------------------------------


class TestPortAllocation:
    def test_skips_occupied_port(self, manager: DevServerManager) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", 42900))
            blocker.listen(1)
            port = manager._allocate_port(42900, taken=set())
        assert port != 42900
        assert 42900 < port < 42900 + 200

    def test_skips_taken_and_reserved(self, manager: DevServerManager) -> None:
        manager._reserved_ports.add(42910)
        port = manager._allocate_port(42910, taken={42911})
        assert port == 42912


# ---------------------------------------------------------------------------
# ensure / readiness / logs / idempotency
# ---------------------------------------------------------------------------


class TestEnsure:
    @pytest.mark.asyncio
    async def test_launch_ready_and_idempotent(
        self, manager: DevServerManager, worktree: Path
    ) -> None:
        try:
            entry = await manager.ensure(KEY, _config(), worktree)
            handle = entry["web"]
            assert handle.state == "ready"
            assert handle.is_alive
            assert handle.origin == f"http://127.0.0.1:{handle.port}"
            # The browser-facing spelling is always localhost, whatever
            # loopback family the probe pinned.
            assert handle.browser_origin == f"http://localhost:{handle.port}"
            assert manager.origins(KEY) == [handle.origin]

            # Log pump captured the marker line.
            await asyncio.sleep(0.2)
            assert "listening on" in manager.log_tail(KEY, "web")

            # Second ensure: same process, no restart.
            entry2 = await manager.ensure(KEY, _config(), worktree)
            assert entry2["web"].proc is handle.proc
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_ipv6_only_bind_is_detected(
        self, manager: DevServerManager, worktree: Path
    ) -> None:
        """A server that binds only IPv6 ``::1`` (as `next dev` on localhost
        often does) must be reached at ``[::1]`` — not the IPv4 default that
        would yield ERR_CONNECTION_REFUSED."""
        serve_v6 = (
            "import os,socket,time\n"
            "port=int(os.environ['PORT'])\n"
            "s=socket.create_server(('::1',port),family=socket.AF_INET6)\n"
            "print('listening on',port,flush=True)\n"
            "time.sleep(120)\n"
        )
        svc = _service(command=[sys.executable, "-c", serve_v6])
        try:
            entry = await manager.ensure(KEY, _config(svc), worktree)
            handle = entry["web"]
            assert handle.state == "ready"
            assert handle.host == "::1"
            # IPv6 literal is bracketed so the URL is well-formed.
            assert handle.origin == f"http://[::1]:{handle.port}"
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_env_cross_reference_reaches_child(
        self, manager: DevServerManager, worktree: Path
    ) -> None:
        capture = (
            "import os,socket,time\n"
            "open('captured.txt','w').write(os.environ.get('API_URL',''))\n"
            "s=socket.create_server(('127.0.0.1',int(os.environ['PORT'])))\n"
            "time.sleep(120)\n"
        )
        api = _service(name="api", base_port=42820)
        web = _service(
            name="web",
            command=[sys.executable, "-c", capture],
            base_port=42830,
            env={"API_URL": "http://127.0.0.1:{port:api}"},
        )
        try:
            entry = await manager.ensure(KEY, _config(api, web), worktree)
            captured = (worktree / "captured.txt").read_text()
            assert captured == f"http://127.0.0.1:{entry['api'].port}"
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_user_port_env_does_not_override_assigned_port(
        self, manager: DevServerManager, worktree: Path
    ) -> None:
        # A user-declared env PORT must NOT shadow the host-assigned port: the
        # child binds the injected port, and readiness (which probes the
        # assigned port) succeeds.
        svc = _service(
            command=[
                sys.executable,
                "-m",
                "http.server",
                "{port}",
                "--bind",
                "127.0.0.1",
            ],
            base_port=42842,
            env={"PORT": "59999"},  # bogus override attempt
            readiness=ServiceReadiness(path="/", timeout_seconds=30),
        )
        try:
            entry = await manager.ensure(KEY, _config(svc), worktree)
            handle = entry["web"]
            assert handle.state == "ready"
            assert handle.port != 59999
            # The child listens on the assigned port (readiness proved it).
            assert handle.origin.endswith(str(handle.port))
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_readiness_path_without_leading_slash_is_normalized(
        self, manager: DevServerManager, worktree: Path
    ) -> None:
        # "index.html" (no leading slash) previously built an unparseable URL
        # ("…:{port}index.html") and crashed; the model normalizes it to
        # "/index.html", and readiness then probes a real file.
        (worktree / "index.html").write_text("<h1>ok</h1>")
        svc = _service(
            command=[
                sys.executable,
                "-m",
                "http.server",
                "{port}",
                "--bind",
                "127.0.0.1",
            ],
            base_port=42844,
            readiness=ServiceReadiness(path="index.html", timeout_seconds=30),
        )
        assert svc.readiness.path == "/index.html"
        try:
            entry = await manager.ensure(KEY, _config(svc), worktree)
            assert entry["web"].state == "ready"
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_http_readiness(self, manager: DevServerManager, worktree: Path) -> None:
        svc = _service(
            command=[
                sys.executable,
                "-m",
                "http.server",
                "{port}",
                "--bind",
                "127.0.0.1",
            ],
            base_port=42840,
            readiness=ServiceReadiness(path="/", timeout_seconds=30),
        )
        try:
            entry = await manager.ensure(KEY, _config(svc), worktree)
            assert entry["web"].state == "ready"
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_early_exit_fails_with_log_tail(
        self, manager: DevServerManager, worktree: Path
    ) -> None:
        svc = _service(
            command=[sys.executable, "-c", "print('boom-marker'); import sys; sys.exit(3)"],
            readiness=ServiceReadiness(timeout_seconds=10),
        )
        with pytest.raises(DevServerError, match="boom-marker"):
            await manager.ensure(KEY, _config(svc), worktree)
        assert manager.get_entry(KEY) is None
        assert manager._reserved_ports == set()

    @pytest.mark.asyncio
    async def test_nonexistent_binary_fails(
        self, manager: DevServerManager, worktree: Path
    ) -> None:
        svc = _service(command=["/nonexistent/definitely-not-a-binary"])
        with pytest.raises(DevServerError, match="Failed to launch"):
            await manager.ensure(KEY, _config(svc), worktree)
        assert manager.get_entry(KEY) is None

    @pytest.mark.asyncio
    async def test_readiness_timeout(self, manager: DevServerManager, worktree: Path) -> None:
        svc = _service(
            command=[sys.executable, "-c", "import time; time.sleep(30)"],
            readiness=ServiceReadiness(timeout_seconds=1),
        )
        with pytest.raises(DevServerError, match="did not become ready"):
            await manager.ensure(KEY, _config(svc), worktree)
        assert manager.get_entry(KEY) is None

    @pytest.mark.asyncio
    async def test_cwd_escape_rejected(self, manager: DevServerManager, worktree: Path) -> None:
        svc = _service(cwd="../..")
        with pytest.raises(DevServerError, match="escapes"):
            await manager.ensure(KEY, _config(svc), worktree)

    @pytest.mark.asyncio
    async def test_cwd_missing_rejected(self, manager: DevServerManager, worktree: Path) -> None:
        svc = _service(cwd="no/such/dir")
        with pytest.raises(DevServerError, match="does not exist"):
            await manager.ensure(KEY, _config(svc), worktree)

    @pytest.mark.asyncio
    async def test_capacity_limit(self, worktree: Path) -> None:
        small = DevServerManager(max_services=1)
        two = _config(
            _service(name="a", base_port=42850),
            _service(name="b", base_port=42860),
        )
        with pytest.raises(DevServerError, match="capacity"):
            await small.ensure(KEY, two, worktree)

    @pytest.mark.asyncio
    async def test_partial_alloc_failure_releases_reserved_ports(
        self, manager: DevServerManager, worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A multi-service config whose 2nd allocation fails must not leak the
        1st service's reservation (else retries burn the scan span forever)."""
        two = _config(
            _service(name="a", base_port=42700),
            _service(name="b", base_port=42710),
        )
        real_alloc = manager._allocate_port
        calls = {"n": 0}

        def flaky_alloc(base_port: int, taken: set[int]) -> int:
            calls["n"] += 1
            if calls["n"] == 2:
                raise DevServerError("no free port (simulated)")
            return real_alloc(base_port, taken=taken)

        monkeypatch.setattr(manager, "_allocate_port", flaky_alloc)
        with pytest.raises(DevServerError):
            await manager.ensure(KEY, two, worktree)
        assert manager._reserved_ports == set()
        assert manager.get_entry(KEY) is None

    @pytest.mark.asyncio
    async def test_config_service_rename_relaunches(
        self, manager: DevServerManager, worktree: Path
    ) -> None:
        """Editing the dev_server config (service renamed) must relaunch, not
        reuse the old entry — otherwise open_app indexes a name that isn't there."""
        await manager.ensure(KEY, _config(_service(name="web", base_port=42720)), worktree)
        try:
            entry = await manager.ensure(
                KEY, _config(_service(name="app", base_port=42730)), worktree
            )
            assert set(entry) == {"app"}
            registered = manager.get_entry(KEY)
            assert registered is not None
            assert set(registered) == {"app"}
        finally:
            await manager.stop_all()


# ---------------------------------------------------------------------------
# Cross-repo dependencies (ensure_with_dependencies / external_ports)
# ---------------------------------------------------------------------------

# Captures one env var to captured.txt, binds PORT, then idles.
_CAPTURE_URL = (
    "import os,socket,time\n"
    "open('captured.txt','w').write(os.environ.get('BACKEND_URL',''))\n"
    "s=socket.create_server(('127.0.0.1',int(os.environ['PORT'])))\n"
    "time.sleep(120)\n"
)

# Writes a start marker, binds PORT, then idles — proves the service launched
# even after a later cleanup removed its registry entry.
_SERVE_MARK = (
    "import os,socket,time\n"
    "open('started.txt','w').write('1')\n"
    "s=socket.create_server(('127.0.0.1',int(os.environ['PORT'])))\n"
    "time.sleep(120)\n"
)


class TestEnsureWithDependencies:
    @pytest.mark.asyncio
    async def test_dependency_starts_first_and_port_substitutes(
        self, manager: DevServerManager, tmp_path: Path
    ) -> None:
        backend_tree = tmp_path / "backend"
        backend_tree.mkdir()
        web_tree = tmp_path / "web"
        web_tree.mkdir()
        backend_cfg = _config(_service(name="api", base_port=42860))
        web_cfg = _config(
            _service(
                name="web",
                command=[sys.executable, "-c", _CAPTURE_URL],
                base_port=42870,
                env={"BACKEND_URL": "http://127.0.0.1:{port:backend/api}"},
            )
        )

        async def load(name: str) -> tuple[DevServerConfig, Path] | None:
            return (backend_cfg, backend_tree) if name == "backend" else None

        async def resolve(name: str, base: Path) -> tuple[TrialKey, Path]:
            return TrialKey("p", "e1", "t1", name), base

        try:
            entries = await ensure_with_dependencies(
                manager,
                "webrepo",
                web_cfg,
                key=TrialKey("p", "e1", "t1", "webrepo"),
                tree=web_tree,
                repo_root=web_tree,
                load_config=load,
                resolve_tree=resolve,
            )
            assert set(entries) == {"webrepo", "backend"}
            backend_port = entries["backend"]["api"].port
            captured = (web_tree / "captured.txt").read_text()
            assert captured == f"http://127.0.0.1:{backend_port}"
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_cycle_rejected(self, manager: DevServerManager, tmp_path: Path) -> None:
        cfg_a = _config(_service(name="s", base_port=42880, env={"X": "{port:b/s}"}))
        cfg_b = _config(_service(name="s", base_port=42890, env={"X": "{port:a/s}"}))

        async def load(name: str) -> tuple[DevServerConfig, Path] | None:
            return {"a": (cfg_a, tmp_path), "b": (cfg_b, tmp_path)}.get(name)

        async def resolve(name: str, base: Path) -> tuple[TrialKey, Path]:
            return TrialKey("p", "e1", "t1", name), base

        with pytest.raises(DevServerError, match="Circular"):
            await ensure_with_dependencies(
                manager,
                "a",
                cfg_a,
                key=TrialKey("p", "e1", "t1", "a"),
                tree=tmp_path,
                repo_root=tmp_path,
                load_config=load,
                resolve_tree=resolve,
            )

    @pytest.mark.asyncio
    async def test_unknown_dependency_repo_rejected(
        self, manager: DevServerManager, tmp_path: Path
    ) -> None:
        cfg = _config(_service(name="web", base_port=42880, env={"X": "{port:ghost/api}"}))

        async def load(_name: str) -> tuple[DevServerConfig, Path] | None:
            return None

        async def resolve(name: str, base: Path) -> tuple[TrialKey, Path]:
            return TrialKey("p", "e1", "t1", name), base

        with pytest.raises(DevServerError, match="ghost"):
            await ensure_with_dependencies(
                manager,
                "webrepo",
                cfg,
                key=TrialKey("p", "e1", "t1", "webrepo"),
                tree=tmp_path,
                repo_root=tmp_path,
                load_config=load,
                resolve_tree=resolve,
            )

    @pytest.mark.asyncio
    async def test_failed_target_stops_freshly_launched_deps(
        self, manager: DevServerManager, tmp_path: Path
    ) -> None:
        backend_tree = tmp_path / "backend"
        backend_tree.mkdir()
        backend_cfg = _config(
            _service(name="api", command=[sys.executable, "-c", _SERVE_MARK], base_port=42860)
        )
        # Target's command does not exist — its launch fails AFTER the dep is up.
        web_cfg = _config(
            _service(
                name="web",
                command=["/nonexistent-binary-for-test"],
                base_port=42870,
                env={"X": "{port:backend/api}"},
            )
        )
        dep_key = TrialKey("p", "e1", "t1", "backend")

        async def load(name: str) -> tuple[DevServerConfig, Path] | None:
            return (backend_cfg, backend_tree) if name == "backend" else None

        async def resolve(name: str, base: Path) -> tuple[TrialKey, Path]:
            return dep_key, base

        try:
            with pytest.raises(DevServerError, match="web"):
                await ensure_with_dependencies(
                    manager,
                    "webrepo",
                    web_cfg,
                    key=TrialKey("p", "e1", "t1", "webrepo"),
                    tree=tmp_path,
                    repo_root=tmp_path,
                    load_config=load,
                    resolve_tree=resolve,
                )
            # The dep really launched (its start marker exists) and was then
            # rolled back — "gone" must not mean "never started".
            assert (backend_tree / "started.txt").exists()
            assert manager.get_entry(dep_key) is None
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_preexisting_dep_survives_target_failure(
        self, manager: DevServerManager, tmp_path: Path
    ) -> None:
        # The other half of the rollback contract: a dependency entry that was
        # ALREADY running (another agent may be using it) must NOT be stopped
        # when the target's launch fails.
        backend_tree = tmp_path / "backend"
        backend_tree.mkdir()
        backend_cfg = _config(_service(name="api", base_port=42860))
        dep_key = TrialKey("p", "e1", "t1", "backend")
        web_cfg = _config(
            _service(
                name="web",
                command=["/nonexistent-binary-for-test"],
                base_port=42870,
                env={"X": "{port:backend/api}"},
            )
        )

        async def load(name: str) -> tuple[DevServerConfig, Path] | None:
            return (backend_cfg, backend_tree) if name == "backend" else None

        async def resolve(name: str, base: Path) -> tuple[TrialKey, Path]:
            return dep_key, base

        try:
            existing = await manager.ensure(dep_key, backend_cfg, backend_tree)
            with pytest.raises(DevServerError, match="web"):
                await ensure_with_dependencies(
                    manager,
                    "webrepo",
                    web_cfg,
                    key=TrialKey("p", "e1", "t1", "webrepo"),
                    tree=tmp_path,
                    repo_root=tmp_path,
                    load_config=load,
                    resolve_tree=resolve,
                )
            entry = manager.get_entry(dep_key)
            assert entry is not None
            assert entry["api"].proc is existing["api"].proc  # untouched, not relaunched
            assert entry["api"].is_alive
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_transitive_chain_ports_flow_through(
        self, manager: DevServerManager, tmp_path: Path
    ) -> None:
        # A → B → C: topological launch beyond depth 1, with each dependent
        # receiving its OWN referenced ports.
        trees = {name: tmp_path / name for name in ("a", "b", "c")}
        for tree in trees.values():
            tree.mkdir()
        cfg_c = _config(_service(name="svc", base_port=42910))
        cfg_b = _config(
            _service(
                name="svc",
                command=[sys.executable, "-c", _CAPTURE_URL],
                base_port=42920,
                env={"BACKEND_URL": "http://127.0.0.1:{port:c/svc}"},
            )
        )
        cfg_a = _config(
            _service(
                name="svc",
                command=[sys.executable, "-c", _CAPTURE_URL],
                base_port=42930,
                env={"BACKEND_URL": "http://127.0.0.1:{port:b/svc}"},
            )
        )
        configs = {"b": cfg_b, "c": cfg_c}

        async def load(name: str) -> tuple[DevServerConfig, Path] | None:
            cfg = configs.get(name)
            return (cfg, trees[name]) if cfg is not None else None

        async def resolve(name: str, base: Path) -> tuple[TrialKey, Path]:
            return TrialKey("p", "e1", "t1", name), base

        try:
            entries = await ensure_with_dependencies(
                manager,
                "a",
                cfg_a,
                key=TrialKey("p", "e1", "t1", "a"),
                tree=trees["a"],
                repo_root=trees["a"],
                load_config=load,
                resolve_tree=resolve,
            )
            assert set(entries) == {"a", "b", "c"}
            b_port = entries["b"]["svc"].port
            c_port = entries["c"]["svc"].port
            assert (trees["a"] / "captured.txt").read_text() == f"http://127.0.0.1:{b_port}"
            assert (trees["b"] / "captured.txt").read_text() == f"http://127.0.0.1:{c_port}"
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_dependency_reuse_is_entrypoint_stable(
        self, manager: DevServerManager, tmp_path: Path
    ) -> None:
        # Each repo's external_ports snapshot must depend only on its OWN
        # references — opening Z (which pulls in A and B) and then opening B
        # directly must NOT relaunch the healthy B.
        trees = {name: tmp_path / name for name in ("z", "a", "b")}
        for tree in trees.values():
            tree.mkdir()
        cfg_a = _config(_service(name="x", base_port=42940))
        cfg_b = _config(_service(name="y", base_port=42950))
        cfg_z = _config(
            _service(
                name="web",
                base_port=42960,
                env={
                    "A_URL": "http://127.0.0.1:{port:a/x}",
                    "B_URL": "http://127.0.0.1:{port:b/y}",
                },
            )
        )
        configs = {"a": cfg_a, "b": cfg_b}

        async def load(name: str) -> tuple[DevServerConfig, Path] | None:
            cfg = configs.get(name)
            return (cfg, trees[name]) if cfg is not None else None

        async def resolve(name: str, base: Path) -> tuple[TrialKey, Path]:
            return TrialKey("p", "e1", "t1", name), base

        try:
            first = await ensure_with_dependencies(
                manager,
                "z",
                cfg_z,
                key=TrialKey("p", "e1", "t1", "z"),
                tree=trees["z"],
                repo_root=trees["z"],
                load_config=load,
                resolve_tree=resolve,
            )
            b_proc = first["b"]["y"].proc

            second = await ensure_with_dependencies(
                manager,
                "b",
                cfg_b,
                key=TrialKey("p", "e1", "t1", "b"),
                tree=trees["b"],
                repo_root=trees["b"],
                load_config=load,
                resolve_tree=resolve,
            )
            assert second["b"]["y"].proc is b_proc  # healthy reuse, no relaunch
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_changed_external_ports_relaunches(
        self, manager: DevServerManager, worktree: Path
    ) -> None:
        key = TrialKey("p", "e1", "t1", "webrepo")
        cfg = _config(
            _service(
                name="web",
                command=[sys.executable, "-c", _CAPTURE_URL],
                base_port=42870,
                env={"BACKEND_URL": "http://127.0.0.1:{port:backend/api}"},
            )
        )
        try:
            entry1 = await manager.ensure(
                key, cfg, worktree, external_ports={"backend/api": 42901}
            )
            assert (worktree / "captured.txt").read_text().endswith(":42901")
            # Same snapshot → healthy reuse (same process).
            entry2 = await manager.ensure(
                key, cfg, worktree, external_ports={"backend/api": 42901}
            )
            assert entry2["web"].proc is entry1["web"].proc
            # A dependency relaunched on a new port → the dependent relaunches
            # so its env re-substitutes.
            entry3 = await manager.ensure(
                key, cfg, worktree, external_ports={"backend/api": 42902}
            )
            assert entry3["web"].proc is not entry1["web"].proc
            assert (worktree / "captured.txt").read_text().endswith(":42902")
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_self_qualified_alias_resolves(
        self, manager: DevServerManager, worktree: Path
    ) -> None:
        key = TrialKey("p", "e1", "t1", "app")
        cfg = _config(
            _service(
                name="web",
                command=[sys.executable, "-c", _CAPTURE_URL],
                base_port=42870,
                env={"BACKEND_URL": "http://127.0.0.1:{port:app/web}"},
            )
        )
        try:
            entry = await manager.ensure(key, cfg, worktree)
            assert (
                (worktree / "captured.txt").read_text()
                == f"http://127.0.0.1:{entry['web'].port}"
            )
        finally:
            await manager.stop_all()


# ---------------------------------------------------------------------------
# Stop paths
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_kills_process(self, manager: DevServerManager, worktree: Path) -> None:
        entry = await manager.ensure(KEY, _config(), worktree)
        proc = entry["web"].proc
        assert proc is not None
        assert await manager.stop(KEY) is True
        assert proc.returncode is not None
        assert manager.get_entry(KEY) is None
        assert await manager.stop(KEY) is False  # idempotent

    @pytest.mark.asyncio
    async def test_stop_for_trial_scopes_by_trial(
        self, manager: DevServerManager, worktree: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        other_key = TrialKey(project_id="p", epic_id="e1", trial_id="t2", repo_name="app")
        other_tree = tmp_path_factory.mktemp("wt2")
        try:
            e1 = await manager.ensure(KEY, _config(), worktree)
            e2 = await manager.ensure(other_key, _config(_service(base_port=42870)), other_tree)

            stopped = await manager.stop_for_trial("p", "e1", "t1")
            assert stopped == 1
            assert manager.get_entry(KEY) is None
            assert manager.get_entry(other_key) is not None
            assert e1["web"].proc is not None
            assert e1["web"].proc.returncode is not None
            assert e2["web"].is_alive
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_stop_for_epic_and_stop_all(
        self, manager: DevServerManager, worktree: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        other_epic = TrialKey(project_id="p", epic_id="e2", trial_id="t1", repo_name="app")
        other_tree = tmp_path_factory.mktemp("wt3")
        try:
            await manager.ensure(KEY, _config(), worktree)
            await manager.ensure(other_epic, _config(_service(base_port=42880)), other_tree)

            assert await manager.stop_for_epic("p", "e1") == 1
            assert manager.get_entry(other_epic) is not None
        finally:
            await manager.stop_all()
        assert manager.get_entry(other_epic) is None

    @pytest.mark.asyncio
    async def test_stopped_process_group_children_die(
        self, manager: DevServerManager, worktree: Path
    ) -> None:
        # Parent spawns a grandchild that also idles; killpg must reap both.
        script = (
            "import os,socket,subprocess,sys,time\n"
            "child = subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])\n"
            "open('grandchild.pid','w').write(str(child.pid))\n"
            "s=socket.create_server(('127.0.0.1',int(os.environ['PORT'])))\n"
            "time.sleep(120)\n"
        )
        svc = _service(command=[sys.executable, "-c", script], base_port=42890)
        await manager.ensure(KEY, _config(svc), worktree)
        grandchild_pid = int((worktree / "grandchild.pid").read_text())
        assert _pid_alive(grandchild_pid)

        await manager.stop(KEY)
        # SIGTERM propagates to the group; give the OS a beat to reap.
        for _ in range(20):
            if not _pid_alive(grandchild_pid):
                break
            await asyncio.sleep(0.1)
        assert not _pid_alive(grandchild_pid)


# ---------------------------------------------------------------------------
# Env sources (§11): env_file / env_passthrough / literal env / PORT
# ---------------------------------------------------------------------------

# Prints selected env vars to the log, then serves and idles.
_SERVE_PRINT_ENV = (
    "import os,socket,time\n"
    "port=int(os.environ['PORT'])\n"
    "for k in ('FROM_FILE','SHARED','PASSED','LIT','VERBATIM','ORDERED'):\n"
    "    print(k+'='+os.environ.get(k,'<unset>'),flush=True)\n"
    "s=socket.create_server(('127.0.0.1',port))\n"
    "print('listening on',port,flush=True)\n"
    "time.sleep(120)\n"
)


class TestEnvSources:
    @pytest.mark.asyncio
    async def test_sources_reach_child_with_merge_order(
        self,
        manager: DevServerManager,
        worktree: Path,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """env_file → env_passthrough → literal env; later sources win."""
        secrets = tmp_path_factory.mktemp("secrets")
        env_file = secrets / "dev.env"
        env_file.write_text("FROM_FILE=file-value\nSHARED=from-file\nLIT=from-file\n")
        monkeypatch.setenv("YUKAR_TEST_SHARED", "ignored")
        monkeypatch.setenv("SHARED", "from-host")
        monkeypatch.setenv("PASSED", "host-value")

        svc = _service(
            command=[sys.executable, "-c", _SERVE_PRINT_ENV],
            base_port=42950,
            env_file=[str(env_file)],
            env_passthrough=["SHARED", "PASSED"],
            env={"LIT": "literal-wins"},
        )
        try:
            await manager.ensure(KEY, _config(svc), worktree)
            await asyncio.sleep(0.2)
            log = manager.log_tail(KEY, "web")
            assert "FROM_FILE=file-value" in log
            assert "SHARED=from-host" in log  # passthrough overrides the file
            assert "PASSED=host-value" in log
            assert "LIT=literal-wins" in log  # literal env overrides the file
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_env_file_values_are_verbatim_not_port_substituted(
        self,
        manager: DevServerManager,
        worktree: Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """§11: env_file values are VERBATIM — {port} is NOT substituted (unlike
        the literal `env:` map). Guards against a 'symmetry' refactor breaking it."""
        secrets = tmp_path_factory.mktemp("secrets")
        env_file = secrets / "verbatim.env"
        # A literal "{port}" must reach the child unchanged.
        env_file.write_text("VERBATIM=url-{port}-tail\n")
        svc = _service(
            command=[sys.executable, "-c", _SERVE_PRINT_ENV],
            base_port=42980,
            env_file=[str(env_file)],
        )
        try:
            await manager.ensure(KEY, _config(svc), worktree)
            await asyncio.sleep(0.2)
            assert "VERBATIM=url-{port}-tail" in manager.log_tail(KEY, "web")
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_multiple_env_files_apply_in_declared_order(
        self,
        manager: DevServerManager,
        worktree: Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """§11: multiple env_file entries apply in declared order — later wins."""
        secrets = tmp_path_factory.mktemp("secrets")
        first = secrets / "a.env"
        second = secrets / "b.env"
        first.write_text("ORDERED=from-first\n")
        second.write_text("ORDERED=from-second\n")
        svc = _service(
            command=[sys.executable, "-c", _SERVE_PRINT_ENV],
            base_port=42985,
            env_file=[str(first), str(second)],
        )
        try:
            await manager.ensure(KEY, _config(svc), worktree)
            await asyncio.sleep(0.2)
            assert "ORDERED=from-second" in manager.log_tail(KEY, "web")
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_relative_env_file_resolves_against_repo_root(
        self,
        manager: DevServerManager,
        worktree: Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Repo-relative declarations read the BASE checkout, not the worktree."""
        repo_root = tmp_path_factory.mktemp("base-checkout")
        (repo_root / ".env.dev").write_text("FROM_FILE=base-checkout-value\n")

        svc = _service(
            command=[sys.executable, "-c", _SERVE_PRINT_ENV],
            base_port=42960,
            env_file=[".env.dev"],
        )
        try:
            await manager.ensure(KEY, _config(svc), worktree, repo_root=repo_root)
            await asyncio.sleep(0.2)
            assert "FROM_FILE=base-checkout-value" in manager.log_tail(KEY, "web")
        finally:
            await manager.stop_all()

    @pytest.mark.asyncio
    async def test_relative_env_file_without_repo_root_fails(
        self, manager: DevServerManager, worktree: Path
    ) -> None:
        svc = _service(base_port=42965, env_file=[".env.dev"])
        with pytest.raises(DevServerError, match="repo-relative"):
            await manager.ensure(KEY, _config(svc), worktree)
        assert manager.get_entry(KEY) is None

    @pytest.mark.asyncio
    async def test_missing_env_file_fails_launch(
        self, manager: DevServerManager, worktree: Path
    ) -> None:
        svc = _service(base_port=42970, env_file=[str(worktree / "nope.env")])
        with pytest.raises(DevServerError, match="nope.env"):
            await manager.ensure(KEY, _config(svc), worktree)
        assert manager.get_entry(KEY) is None

    @pytest.mark.asyncio
    async def test_missing_passthrough_fails_launch(
        self, manager: DevServerManager, worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("YUKAR_TEST_ABSENT", raising=False)
        svc = _service(base_port=42975, env_passthrough=["YUKAR_TEST_ABSENT"])
        with pytest.raises(DevServerError, match="YUKAR_TEST_ABSENT"):
            await manager.ensure(KEY, _config(svc), worktree)
        assert manager.get_entry(KEY) is None
