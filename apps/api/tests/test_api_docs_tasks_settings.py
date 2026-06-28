"""Tests for docs, tasks, settings GET/PUT endpoints."""

from __future__ import annotations

from httpx import AsyncClient


class TestDocsCRUD:
    async def _setup(self, client: AsyncClient) -> None:
        await client.post("/api/projects", json={"id": "proj", "name": "Proj", "repos": []})
        await client.post("/api/projects/proj/epics", json={"title": "Epic"})

    async def test_project_doc_put_and_get(self, app_client: AsyncClient) -> None:
        await self._setup(app_client)
        r = await app_client.put(
            "/api/projects/proj/docs/overview.md",
            json={"content": "# Overview\nHello world"},
        )
        assert r.status_code == 200

        r2 = await app_client.get("/api/projects/proj/docs/overview.md")
        assert r2.status_code == 200
        assert r2.json()["content"] == "# Overview\nHello world"

    async def test_project_doc_list(self, app_client: AsyncClient) -> None:
        await self._setup(app_client)
        await app_client.put("/api/projects/proj/docs/a.md", json={"content": "A"})
        await app_client.put("/api/projects/proj/docs/b.md", json={"content": "B"})
        r = await app_client.get("/api/projects/proj/docs")
        assert r.status_code == 200
        assert "a.md" in r.json()
        assert "b.md" in r.json()

    async def test_project_doc_not_found(self, app_client: AsyncClient) -> None:
        await self._setup(app_client)
        r = await app_client.get("/api/projects/proj/docs/missing.md")
        assert r.status_code == 404

    async def test_project_doc_path_traversal_rejected(self, app_client: AsyncClient) -> None:
        await self._setup(app_client)
        r = await app_client.put(
            "/api/projects/proj/docs/../../../etc/passwd",
            json={"content": "evil"},
        )
        # FastAPI will likely 422 or 404 due to path validation
        assert r.status_code in (404, 422)

    async def test_epic_doc_put_and_get(self, app_client: AsyncClient) -> None:
        await self._setup(app_client)
        r = await app_client.put(
            "/api/projects/proj/epics/EP-1/docs/plan.md",
            json={"content": "# Plan\nDo stuff"},
        )
        assert r.status_code == 200

        r2 = await app_client.get("/api/projects/proj/epics/EP-1/docs/plan.md")
        assert r2.status_code == 200
        assert "Plan" in r2.json()["content"]


class TestTasksCRUD:
    async def _setup(self, client: AsyncClient) -> None:
        await client.post("/api/projects", json={"id": "proj", "name": "Proj", "repos": []})
        await client.post("/api/projects/proj/epics", json={"title": "Epic"})

    async def test_get_tasks_empty(self, app_client: AsyncClient) -> None:
        await self._setup(app_client)
        r = await app_client.get("/api/projects/proj/epics/EP-1/tasks")
        assert r.status_code == 200
        data = r.json()
        assert data["tasks"] == []

    async def test_put_tasks(self, app_client: AsyncClient) -> None:
        await self._setup(app_client)
        tasks_body = {
            "tasks": [
                {"id": "T1", "title": "Task One", "status": "todo"},
                {"id": "T2", "title": "Task Two", "status": "in_progress"},
            ],
            "progress": {"done": 0, "total": 2},
        }
        r = await app_client.put("/api/projects/proj/epics/EP-1/tasks", json=tasks_body)
        assert r.status_code == 200

        r2 = await app_client.get("/api/projects/proj/epics/EP-1/tasks")
        assert r2.status_code == 200
        data = r2.json()
        assert len(data["tasks"]) == 2
        assert data["tasks"][0]["id"] == "T1"


class TestSettingsCRUD:
    async def test_get_settings(self, app_client: AsyncClient) -> None:
        r = await app_client.get("/api/settings")
        assert r.status_code == 200
        data = r.json()
        assert "llm" in data
        assert "git" in data
        assert data["git"]["author_name"] == "yukar"

    async def test_put_settings(self, app_client: AsyncClient) -> None:
        r = await app_client.get("/api/settings")
        current = r.json()
        current["git"]["author_name"] = "custom-author"
        r2 = await app_client.put("/api/settings", json=current)
        assert r2.status_code == 200
        assert r2.json()["git"]["author_name"] == "custom-author"

    async def test_put_settings_change_workspace_root_rejected_422(
        self, app_client: AsyncClient
    ) -> None:
        """Changing workspace_root at runtime must be rejected with 422 (it is
        wired into supervisor/indexer/watcher at startup and cannot be live-
        rewired).  Other field changes in the same request must NOT take effect."""
        r = await app_client.get("/api/settings")
        current = r.json()
        original_root = current["workspace_root"]
        current["workspace_root"] = "/some/other/root"
        current["git"]["author_name"] = "should-not-persist"

        r2 = await app_client.put("/api/settings", json=current)
        assert r2.status_code == 422, r2.text
        assert "workspace_root" in r2.json()["detail"]

        # The rejected request must be atomic: neither workspace_root nor the
        # co-submitted git.author_name change is applied.
        r3 = await app_client.get("/api/settings")
        after = r3.json()
        assert after["workspace_root"] == original_root
        assert after["git"]["author_name"] != "should-not-persist"

    async def test_put_settings_other_fields_update_when_root_unchanged(
        self, app_client: AsyncClient
    ) -> None:
        """Non-workspace_root fields must still update as long as the submitted
        workspace_root matches the current (expanded) value."""
        r = await app_client.get("/api/settings")
        current = r.json()
        # workspace_root is echoed back already-expanded, so resubmitting it
        # unchanged must pass the runtime-immutability guard.
        current["agent"]["worker_max_turns"] = 99
        r2 = await app_client.put("/api/settings", json=current)
        assert r2.status_code == 200, r2.text
        assert r2.json()["agent"]["worker_max_turns"] == 99
