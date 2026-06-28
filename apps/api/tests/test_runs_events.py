"""Tests for DummyRunner, event bus, and SSE endpoint."""

from __future__ import annotations

import asyncio
from pathlib import Path

from httpx import AsyncClient


class TestEventBus:
    async def test_publish_and_receive(self) -> None:
        from yukar.events import bus as event_bus
        from yukar.events.bus import subscribe

        async with subscribe("p1", "e1") as q:
            event_bus.publish("p1", "e1", {"type": "test", "value": 42})
            event = await asyncio.wait_for(q.get(), timeout=1.0)
            assert event["value"] == 42

    async def test_no_cross_contamination(self) -> None:
        from yukar.events import bus as event_bus
        from yukar.events.bus import subscribe

        async with subscribe("p1", "e1") as q1, subscribe("p2", "e2") as q2:
            event_bus.publish("p1", "e1", {"src": "e1"})
            event_bus.publish("p2", "e2", {"src": "e2"})

            ev1 = await asyncio.wait_for(q1.get(), timeout=1.0)
            ev2 = await asyncio.wait_for(q2.get(), timeout=1.0)
            assert ev1["src"] == "e1"
            assert ev2["src"] == "e2"

    async def test_multiple_subscribers(self) -> None:
        from yukar.events import bus as event_bus
        from yukar.events.bus import subscribe

        async with subscribe("p1", "e1") as q1, subscribe("p1", "e1") as q2:
            event_bus.publish("p1", "e1", "broadcast")
            ev1 = await asyncio.wait_for(q1.get(), timeout=1.0)
            ev2 = await asyncio.wait_for(q2.get(), timeout=1.0)
            assert ev1 == "broadcast"
            assert ev2 == "broadcast"


class TestDummyRunner:
    async def test_dummy_runner_publishes_events(self, tmp_workspace: Path) -> None:
        from yukar.events.bus import subscribe
        from yukar.runs.runner import DummyRunner

        root = str(tmp_workspace)
        project_id = "proj"
        epic_id = "EP-1"
        run_id = "run-test"

        # Create project/epic dirs so state_repo can write
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        await save_project(root, Project(id=project_id, name="P"))
        await save_epic(root, project_id, Epic(id=epic_id, slug="test", title="Test"))

        received: list[object] = []

        async def collect() -> None:
            async with subscribe(project_id, epic_id) as q:
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=10.0)
                        received.append(event)
                        # Stop after run_completed
                        if hasattr(event, "type") and event.type == "run_completed":  # type: ignore[union-attr]
                            break
                    except TimeoutError:
                        break

        runner = DummyRunner()
        collector = asyncio.create_task(collect())
        # Give collector time to register
        await asyncio.sleep(0.05)
        await runner.start(root, project_id, epic_id, run_id)
        await asyncio.wait_for(collector, timeout=15.0)

        event_types = [getattr(e, "type", None) for e in received]
        assert "run_started" in event_types
        assert "run_completed" in event_types
        assert "task_update" in event_types

    async def test_dummy_runner_stop(self, tmp_workspace: Path) -> None:
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.runs.runner import DummyRunner
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="P"))
        await save_epic(root, "p", Epic(id="EP-1", slug="s", title="T"))

        runner = DummyRunner()
        task = asyncio.create_task(runner.start(root, "p", "EP-1", "run-1"))
        await asyncio.sleep(0.1)
        await runner.stop()
        await asyncio.wait_for(task, timeout=3.0)


class TestSSEEndpoint:
    async def test_sse_events_stream(self, app_client: AsyncClient, tmp_workspace: Path) -> None:
        """Smoke test: start a run and collect events from bus, verify run_started fires."""
        import asyncio

        from yukar.events.bus import subscribe

        # Create project and epic
        await app_client.post("/api/projects", json={"id": "proj2", "name": "P", "repos": []})
        await app_client.post("/api/projects/proj2/epics", json={"title": "Epic"})

        # Collect events via bus directly (avoids SSE HTTP streaming complexity in tests)
        received: list[object] = []

        async def collect() -> None:
            async with subscribe("proj2", "EP-1") as q:
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=8.0)
                        received.append(event)
                        if hasattr(event, "type") and event.type == "run_completed":  # type: ignore[union-attr]
                            break
                    except TimeoutError:
                        break

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0.05)

        r = await app_client.post("/api/projects/proj2/epics/EP-1/run")
        assert r.status_code == 202

        await asyncio.wait_for(collector, timeout=12.0)

        event_types = [getattr(e, "type", None) for e in received]
        assert "run_started" in event_types
