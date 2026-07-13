"""dev_server launch config on Repo — model validation, storage, REST endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from yukar.models.project import (
    DevServerConfig,
    DevService,
    Project,
    Repo,
    ServiceReadiness,
)
from yukar.storage.project_repo import (
    get_repo,
    save_project,
    save_repo,
    update_repo_dev_server,
)


def _service(**overrides: Any) -> DevService:
    base: dict[str, Any] = {
        "name": "web",
        "command": ["pnpm", "dev", "--port", "{port}"],
        "base_port": 3000,
    }
    base.update(overrides)
    return DevService(**base)


def _config(*services: DevService) -> DevServerConfig:
    return DevServerConfig(services=list(services) if services else [_service()])


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


class TestDevServerModel:
    def test_defaults(self) -> None:
        cfg = _config()
        assert cfg.browser.allowed_origins == []
        assert cfg.browser.allow_common_cdns is True
        svc = cfg.services[0]
        assert svc.cwd == "."
        assert svc.env == {}
        assert svc.readiness.path is None
        assert svc.readiness.timeout_seconds == 60.0

    def test_empty_services_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DevServerConfig(services=[])

    def test_empty_command_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _service(command=[])

    def test_duplicate_service_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unique"):
            DevServerConfig(services=[_service(), _service(base_port=3001)])

    @pytest.mark.parametrize("bad_name", ["", "has space", "-leading", "日本語", "a/b"])
    def test_invalid_service_name_rejected(self, bad_name: str) -> None:
        with pytest.raises(ValidationError):
            _service(name=bad_name)

    @pytest.mark.parametrize("good_name", ["web", "api-2", "Api_Server", "0front"])
    def test_valid_service_name_accepted(self, good_name: str) -> None:
        assert _service(name=good_name).name == good_name

    @pytest.mark.parametrize("bad_port", [0, -1, 65536])
    def test_base_port_range_enforced(self, bad_port: int) -> None:
        with pytest.raises(ValidationError):
            _service(base_port=bad_port)

    @pytest.mark.parametrize("bad_timeout", [0, -5, 601])
    def test_readiness_timeout_bounds(self, bad_timeout: float) -> None:
        with pytest.raises(ValidationError):
            ServiceReadiness(timeout_seconds=bad_timeout)

    def test_repo_without_dev_server_loads_as_none(self) -> None:
        # Backward compat: pre-existing repo YAMLs have no dev_server key.
        repo = Repo.model_validate({"name": "api", "path": "/tmp/api"})
        assert repo.dev_server is None


# ---------------------------------------------------------------------------
# Storage roundtrip
# ---------------------------------------------------------------------------


class TestDevServerStorage:
    @pytest.mark.asyncio
    async def test_yaml_roundtrip(self, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p", repos=["app"]))
        cfg = DevServerConfig(
            services=[
                _service(
                    name="api",
                    command=["uv", "run", "uvicorn", "app:app", "--port", "{port}"],
                    cwd="apps/api",
                    base_port=8000,
                    readiness=ServiceReadiness(path="/health", timeout_seconds=30),
                ),
                _service(
                    name="web",
                    cwd="apps/web",
                    env={"API_URL": "http://127.0.0.1:{port:api}"},
                ),
            ]
        )
        await save_repo(root, "p", Repo(name="app", path="/tmp/app", dev_server=cfg))

        loaded = await get_repo(root, "p", "app")
        assert loaded is not None
        assert loaded.dev_server == cfg

    @pytest.mark.asyncio
    async def test_update_sets_and_clears(self, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p", repos=["app"]))
        await save_repo(root, "p", Repo(name="app", path="/tmp/app"))

        updated = await update_repo_dev_server(root, "p", "app", _config())
        assert updated is not None
        assert updated.dev_server is not None

        cleared = await update_repo_dev_server(root, "p", "app", None)
        assert cleared is not None
        assert cleared.dev_server is None
        reloaded = await get_repo(root, "p", "app")
        assert reloaded is not None
        assert reloaded.dev_server is None

    @pytest.mark.asyncio
    async def test_update_missing_repo_returns_none(self, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p"))
        assert await update_repo_dev_server(root, "p", "ghost", _config()) is None


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

_VALID_BODY = {
    "services": [
        {
            "name": "web",
            "command": ["pnpm", "dev", "--port", "{port}"],
            "cwd": "apps/web",
            "base_port": 3000,
            "readiness": {"path": "/", "timeout_seconds": 120},
            "env": {},
        }
    ],
    "browser": {"allowed_origins": ["https://cdn.example.com"], "allow_common_cdns": True},
}


class TestDevServerAPI:
    async def _seed_repo(self, tmp_workspace: Path) -> str:
        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p", repos=["app"]))
        await save_repo(root, "p", Repo(name="app", path="/tmp/app"))
        return root

    @pytest.mark.asyncio
    async def test_put_and_persist(self, app_client: Any, tmp_workspace: Path) -> None:
        root = await self._seed_repo(tmp_workspace)
        resp = await app_client.put("/api/projects/p/repos/app/dev-server", json=_VALID_BODY)
        assert resp.status_code == 200
        data = resp.json()
        assert data["dev_server"]["services"][0]["name"] == "web"
        assert data["dev_server"]["browser"]["allowed_origins"] == ["https://cdn.example.com"]

        loaded = await get_repo(root, "p", "app")
        assert loaded is not None
        assert loaded.dev_server is not None
        assert loaded.dev_server.services[0].base_port == 3000

    @pytest.mark.asyncio
    async def test_put_invalid_config_422(self, app_client: Any, tmp_workspace: Path) -> None:
        await self._seed_repo(tmp_workspace)
        dup = {
            "services": [
                {"name": "web", "command": ["pnpm", "dev"], "base_port": 3000},
                {"name": "web", "command": ["pnpm", "dev"], "base_port": 3001},
            ]
        }
        resp = await app_client.put("/api/projects/p/repos/app/dev-server", json=dup)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_put_missing_repo_404(self, app_client: Any, tmp_workspace: Path) -> None:
        await save_project(str(tmp_workspace), Project(id="p", name="p"))
        resp = await app_client.put("/api/projects/p/repos/ghost/dev-server", json=_VALID_BODY)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_put_missing_project_404(self, app_client: Any) -> None:
        resp = await app_client.put("/api/projects/noexist/repos/app/dev-server", json=_VALID_BODY)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_clears(self, app_client: Any, tmp_workspace: Path) -> None:
        root = await self._seed_repo(tmp_workspace)
        put = await app_client.put("/api/projects/p/repos/app/dev-server", json=_VALID_BODY)
        assert put.status_code == 200

        resp = await app_client.delete("/api/projects/p/repos/app/dev-server")
        assert resp.status_code == 200
        assert resp.json()["dev_server"] is None
        loaded = await get_repo(root, "p", "app")
        assert loaded is not None
        assert loaded.dev_server is None

    @pytest.mark.asyncio
    async def test_delete_missing_repo_404(self, app_client: Any, tmp_workspace: Path) -> None:
        await save_project(str(tmp_workspace), Project(id="p", name="p"))
        resp = await app_client.delete("/api/projects/p/repos/ghost/dev-server")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_repos_includes_dev_server(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        await self._seed_repo(tmp_workspace)
        await app_client.put("/api/projects/p/repos/app/dev-server", json=_VALID_BODY)
        resp = await app_client.get("/api/projects/p/repos")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["dev_server"]["services"][0]["base_port"] == 3000
