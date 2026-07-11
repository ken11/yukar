"""Tests for projects/epics/threads CRUD API."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient


class TestProjectsCRUD:
    async def test_list_empty(self, app_client: AsyncClient) -> None:
        r = await app_client.get("/api/projects")
        assert r.status_code == 200
        assert r.json() == []

    async def test_create_and_get(self, app_client: AsyncClient, fixture_git_repo: Path) -> None:
        body = {
            "id": "test-proj",
            "name": "Test Project",
            "repos": [
                {"name": "test-repo", "path": str(fixture_git_repo), "default_branch": "main"}
            ],
        }
        r = await app_client.post("/api/projects", json=body)
        assert r.status_code == 201
        data = r.json()
        assert data["id"] == "test-proj"
        assert data["name"] == "Test Project"
        assert data["epic_counter"] == 0

        r2 = await app_client.get("/api/projects/test-proj")
        assert r2.status_code == 200
        assert r2.json()["name"] == "Test Project"

    async def test_create_invalid_repo_path(self, app_client: AsyncClient, tmp_path: Path) -> None:
        body = {
            "id": "bad-proj",
            "name": "Bad",
            "repos": [{"name": "bad", "path": str(tmp_path / "not-a-repo")}],
        }
        r = await app_client.post("/api/projects", json=body)
        assert r.status_code == 422

    async def test_duplicate_project_409(
        self, app_client: AsyncClient, fixture_git_repo: Path
    ) -> None:
        body = {"id": "dup", "name": "Dup", "repos": []}
        await app_client.post("/api/projects", json=body)
        r = await app_client.post("/api/projects", json=body)
        assert r.status_code == 409

    async def test_patch_project(self, app_client: AsyncClient, fixture_git_repo: Path) -> None:
        await app_client.post("/api/projects", json={"id": "p1", "name": "Old Name", "repos": []})
        r = await app_client.patch("/api/projects/p1", json={"name": "New Name"})
        assert r.status_code == 200
        assert r.json()["name"] == "New Name"

    async def test_delete_project(self, app_client: AsyncClient) -> None:
        await app_client.post("/api/projects", json={"id": "del-me", "name": "Del", "repos": []})
        r = await app_client.delete("/api/projects/del-me")
        assert r.status_code == 204
        r2 = await app_client.get("/api/projects/del-me")
        assert r2.status_code == 404

    async def test_list_after_create(self, app_client: AsyncClient) -> None:
        await app_client.post("/api/projects", json={"id": "a", "name": "A", "repos": []})
        await app_client.post("/api/projects", json={"id": "b", "name": "B", "repos": []})
        r = await app_client.get("/api/projects")
        assert r.status_code == 200
        ids = [p["id"] for p in r.json()]
        assert "a" in ids
        assert "b" in ids


class TestEpicsCRUD:
    async def _create_project(self, client: AsyncClient) -> None:
        await client.post("/api/projects", json={"id": "proj", "name": "Proj", "repos": []})

    async def test_create_epic(self, app_client: AsyncClient) -> None:
        await self._create_project(app_client)
        r = await app_client.post(
            "/api/projects/proj/epics",
            json={"title": "Refactor Auth", "description": "Big refactor"},
        )
        assert r.status_code == 201
        data = r.json()
        assert data["id"] == "EP-1"
        assert data["slug"] == "refactor-auth"
        assert data["branch"] == "yukar/ep-1-refactor-auth"
        assert data["status"] == "open"

    async def test_epic_counter_increments(self, app_client: AsyncClient) -> None:
        await self._create_project(app_client)
        r1 = await app_client.post("/api/projects/proj/epics", json={"title": "Epic One"})
        r2 = await app_client.post("/api/projects/proj/epics", json={"title": "Epic Two"})
        assert r1.json()["id"] == "EP-1"
        assert r2.json()["id"] == "EP-2"

    async def test_get_epic(self, app_client: AsyncClient) -> None:
        await self._create_project(app_client)
        await app_client.post("/api/projects/proj/epics", json={"title": "My Epic"})
        r = await app_client.get("/api/projects/proj/epics/EP-1")
        assert r.status_code == 200
        assert r.json()["title"] == "My Epic"

    async def test_patch_epic_status(self, app_client: AsyncClient) -> None:
        await self._create_project(app_client)
        await app_client.post("/api/projects/proj/epics", json={"title": "E"})
        r = await app_client.patch("/api/projects/proj/epics/EP-1", json={"status": "completed"})
        assert r.status_code == 200
        assert r.json()["status"] == "completed"

    async def test_list_epics(self, app_client: AsyncClient) -> None:
        await self._create_project(app_client)
        await app_client.post("/api/projects/proj/epics", json={"title": "A"})
        await app_client.post("/api/projects/proj/epics", json={"title": "B"})
        r = await app_client.get("/api/projects/proj/epics")
        assert r.status_code == 200
        assert len(r.json()) == 2

    async def test_list_epics_newest_first(self, app_client: AsyncClient) -> None:
        """Epics are returned in created_at descending order (newest first)."""
        await self._create_project(app_client)
        await app_client.post("/api/projects/proj/epics", json={"title": "First"})
        await app_client.post("/api/projects/proj/epics", json={"title": "Second"})
        await app_client.post("/api/projects/proj/epics", json={"title": "Third"})
        r = await app_client.get("/api/projects/proj/epics")
        assert r.status_code == 200
        ids = [e["id"] for e in r.json()]
        # EP-3 was created last and should appear first.
        assert ids[0] == "EP-3"
        assert ids[-1] == "EP-1"


class TestEpicSortOrder:
    """Unit tests for list_epics sort order (created_at desc, numeric id tie-break)."""

    @pytest.mark.asyncio
    async def test_numeric_tiebreak_ep1_ep2_ep10(self, tmp_path: Path) -> None:
        """When created_at is identical, EP-10 > EP-2 > EP-1 (numeric, not lexicographic)."""
        from datetime import UTC, datetime

        from yukar.models.epic import Epic
        from yukar.storage.epic_repo import list_epics, save_epic

        root = str(tmp_path)
        project_id = "proj"

        # Use a fixed timestamp so all three epics share the same created_at.
        ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

        for epic_id, title in [("EP-1", "First"), ("EP-2", "Second"), ("EP-10", "Tenth")]:
            epic = Epic(
                id=epic_id,
                slug=title.lower(),
                title=title,
                branch=f"yukar/{epic_id.lower()}-{title.lower()}",
                created_at=ts,
                updated_at=ts,
            )
            await save_epic(root, project_id, epic)

        result = await list_epics(root, project_id)
        ids = [e.id for e in result]
        assert ids == ["EP-10", "EP-2", "EP-1"], f"Expected numeric descending, got {ids}"

    @pytest.mark.asyncio
    async def test_newer_created_at_comes_first(self, tmp_path: Path) -> None:
        """Epic with later created_at appears before older one regardless of id."""
        from datetime import UTC, datetime

        from yukar.models.epic import Epic
        from yukar.storage.epic_repo import list_epics, save_epic

        root = str(tmp_path)
        project_id = "proj"

        old_ts = datetime(2026, 1, 1, tzinfo=UTC)
        new_ts = datetime(2026, 6, 1, tzinfo=UTC)

        # EP-1 is older; EP-2 is newer.
        for epic_id, ts, title in [("EP-1", old_ts, "Old"), ("EP-2", new_ts, "New")]:
            epic = Epic(
                id=epic_id,
                slug=title.lower(),
                title=title,
                branch=f"yukar/{epic_id.lower()}-{title.lower()}",
                created_at=ts,
                updated_at=ts,
            )
            await save_epic(root, project_id, epic)

        result = await list_epics(root, project_id)
        assert result[0].id == "EP-2"
        assert result[1].id == "EP-1"


class TestThreadsCRUD:
    async def _setup(self, client: AsyncClient) -> None:
        await client.post("/api/projects", json={"id": "proj", "name": "Proj", "repos": []})
        await client.post("/api/projects/proj/epics", json={"title": "Epic"})

    async def test_create_and_list_threads(self, app_client: AsyncClient) -> None:
        await self._setup(app_client)
        r = await app_client.post(
            "/api/projects/proj/epics/EP-1/threads",
            json={"title": "Auth Strategy", "role": "worker", "repo": "my-repo"},
        )
        assert r.status_code == 201
        data = r.json()
        assert data["title"] == "Auth Strategy"
        assert data["role"] == "worker"

        r2 = await app_client.get("/api/projects/proj/epics/EP-1/threads")
        assert r2.status_code == 200
        assert len(r2.json()) == 1

    async def test_post_message_and_get(self, app_client: AsyncClient) -> None:
        await self._setup(app_client)
        cr = await app_client.post(
            "/api/projects/proj/epics/EP-1/threads",
            json={"title": "Test Thread"},
        )
        thread_id = cr.json()["id"]

        mr = await app_client.post(
            f"/api/projects/proj/epics/EP-1/threads/{thread_id}/messages",
            json={"content": "Hello Agent!", "role": "user"},
        )
        assert mr.status_code == 201

        gr = await app_client.get(f"/api/projects/proj/epics/EP-1/threads/{thread_id}")
        assert gr.status_code == 200
        messages = gr.json()
        assert len(messages) == 1
        assert messages[0]["message"]["content"][0]["text"] == "Hello Agent!"
