"""Plan-approval snapshot (lifecycle redesign).

Covers:
- compute_plan_hash: deterministic, order-insensitive, execution-state-blind,
  sensitive to every plan-defining field.
- plan_approval_repo: save/get/delete round-trip on plan_approval.yaml.
- REST: GET /tasks carries plan_hash/approved_hash/plan_approved;
  POST /plan/approval records (409 on stale hash — the TOCTOU guard);
  DELETE /plan/approval revokes (204, idempotent).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import AsyncClient

from yukar.models.task import PlanApproval, Task, compute_plan_hash

# ---------------------------------------------------------------------------
# Unit: compute_plan_hash
# ---------------------------------------------------------------------------


def _task(**overrides: object) -> Task:
    base: dict[str, object] = {
        "id": "T1",
        "title": "Do the thing",
        "status": "todo",
        "repo": "myrepo",
        "depends_on": [],
        "thread": None,
        "contract": "implement X; pytest passes",
        "agent": None,
    }
    base.update(overrides)
    return Task.model_validate(base)


class TestComputePlanHash:
    def test_deterministic(self) -> None:
        tasks = [_task(), _task(id="T2", title="Other")]
        assert compute_plan_hash(tasks) == compute_plan_hash(tasks)
        # 64 lowercase hex chars = SHA-256 hexdigest.
        h = compute_plan_hash(tasks)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_task_order_does_not_matter(self) -> None:
        a = [_task(id="T1"), _task(id="T2", title="Other"), _task(id="T3", title="Third")]
        b = [a[2], a[0], a[1]]
        assert compute_plan_hash(a) == compute_plan_hash(b)

    def test_execution_state_does_not_matter(self) -> None:
        """status / thread flips (what dispatch does) never strip an approval."""
        planned = [_task(status="todo", thread=None)]
        dispatched = [_task(status="in_progress", thread="w-T1-1")]
        done = [_task(status="done", thread="w-T1-1")]
        assert compute_plan_hash(planned) == compute_plan_hash(dispatched)
        assert compute_plan_hash(planned) == compute_plan_hash(done)

    def test_every_plan_field_changes_the_hash(self) -> None:
        base = compute_plan_hash([_task()])
        assert compute_plan_hash([_task(id="T9")]) != base
        assert compute_plan_hash([_task(title="Renamed")]) != base
        assert compute_plan_hash([_task(repo="other")]) != base
        assert compute_plan_hash([_task(depends_on=["T0"])]) != base
        assert compute_plan_hash([_task(contract="different contract")]) != base
        assert compute_plan_hash([_task(agent="frontend-worker")]) != base

    def test_adding_a_task_changes_the_hash(self) -> None:
        one = compute_plan_hash([_task()])
        two = compute_plan_hash([_task(), _task(id="T2", title="Other")])
        assert one != two

    def test_empty_plan_has_a_stable_hash(self) -> None:
        assert compute_plan_hash([]) == compute_plan_hash([])


# ---------------------------------------------------------------------------
# Unit: plan_approval_repo round-trip
# ---------------------------------------------------------------------------


class TestPlanApprovalRepo:
    async def test_get_missing_returns_none(self, tmp_path: Path) -> None:
        from yukar.storage import plan_approval_repo

        got = await plan_approval_repo.get_plan_approval(str(tmp_path), "proj", "EP-1")
        assert got is None

    async def test_save_and_get_roundtrip(self, tmp_path: Path) -> None:
        from yukar.storage import plan_approval_repo

        approval = PlanApproval(tasks_hash="a" * 64, approved_at=datetime.now(UTC))
        await plan_approval_repo.save_plan_approval(str(tmp_path), "proj", "EP-1", approval)
        got = await plan_approval_repo.get_plan_approval(str(tmp_path), "proj", "EP-1")
        assert got is not None
        assert got.tasks_hash == "a" * 64
        assert got.approved_at is not None

    async def test_delete_removes_and_is_idempotent(self, tmp_path: Path) -> None:
        from yukar.config import paths
        from yukar.storage import plan_approval_repo

        approval = PlanApproval(tasks_hash="b" * 64, approved_at=datetime.now(UTC))
        await plan_approval_repo.save_plan_approval(str(tmp_path), "proj", "EP-1", approval)
        assert paths.plan_approval_yaml(str(tmp_path), "proj", "EP-1").exists()

        await plan_approval_repo.delete_plan_approval(str(tmp_path), "proj", "EP-1")
        assert not paths.plan_approval_yaml(str(tmp_path), "proj", "EP-1").exists()
        assert await plan_approval_repo.get_plan_approval(str(tmp_path), "proj", "EP-1") is None

        # Second delete is a no-op, not an error.
        await plan_approval_repo.delete_plan_approval(str(tmp_path), "proj", "EP-1")


# ---------------------------------------------------------------------------
# REST: GET /tasks approval fields + POST/DELETE /plan/approval
# ---------------------------------------------------------------------------


_TASK_DICTS: list[dict[str, str]] = [
    {"id": "T1", "title": "Task One", "status": "todo", "contract": "do A"},
    {"id": "T2", "title": "Task Two", "status": "todo", "contract": "do B"},
]

_TASKS_BODY: dict[str, object] = {
    "tasks": _TASK_DICTS,
    "progress": {"done": 0, "total": 2},
}


class TestPlanApprovalRest:
    async def _setup(self, client: AsyncClient) -> None:
        await client.post("/api/projects", json={"id": "proj", "name": "Proj", "repos": []})
        await client.post("/api/projects/proj/epics", json={"title": "Epic"})

    async def test_get_tasks_reports_unapproved_plan(self, app_client: AsyncClient) -> None:
        await self._setup(app_client)
        r = await app_client.get("/api/projects/proj/epics/EP-1/tasks")
        assert r.status_code == 200
        data = r.json()
        assert data["tasks"] == []
        assert data["plan_hash"] == compute_plan_hash([])
        assert data["approved_hash"] is None
        assert data["plan_approved"] is False

    async def test_approve_current_plan(self, app_client: AsyncClient) -> None:
        await self._setup(app_client)
        await app_client.put("/api/projects/proj/epics/EP-1/tasks", json=_TASKS_BODY)

        r = await app_client.get("/api/projects/proj/epics/EP-1/tasks")
        plan_hash = r.json()["plan_hash"]

        r2 = await app_client.post(
            "/api/projects/proj/epics/EP-1/plan/approval", json={"tasks_hash": plan_hash}
        )
        assert r2.status_code == 200
        assert r2.json()["tasks_hash"] == plan_hash

        r3 = await app_client.get("/api/projects/proj/epics/EP-1/tasks")
        data = r3.json()
        assert data["approved_hash"] == plan_hash
        assert data["plan_approved"] is True

    async def test_stale_hash_is_409(self, app_client: AsyncClient) -> None:
        """Approving a plan that changed underneath the client is refused."""
        await self._setup(app_client)
        await app_client.put("/api/projects/proj/epics/EP-1/tasks", json=_TASKS_BODY)
        stale_hash = compute_plan_hash([])  # the plan the client saw before PUT
        r = await app_client.post(
            "/api/projects/proj/epics/EP-1/plan/approval", json={"tasks_hash": stale_hash}
        )
        assert r.status_code == 409

        # And the refused approval left no record behind.
        r2 = await app_client.get("/api/projects/proj/epics/EP-1/tasks")
        assert r2.json()["plan_approved"] is False
        assert r2.json()["approved_hash"] is None

    async def test_plan_change_after_approval_unapproves(self, app_client: AsyncClient) -> None:
        await self._setup(app_client)
        await app_client.put("/api/projects/proj/epics/EP-1/tasks", json=_TASKS_BODY)
        r = await app_client.get("/api/projects/proj/epics/EP-1/tasks")
        plan_hash = r.json()["plan_hash"]
        await app_client.post(
            "/api/projects/proj/epics/EP-1/plan/approval", json={"tasks_hash": plan_hash}
        )

        # Change the plan (new task) — the recorded approval no longer matches.
        changed = {
            "tasks": [*_TASK_DICTS, {"id": "T3", "title": "New", "contract": "do C"}],
            "progress": {"done": 0, "total": 3},
        }
        await app_client.put("/api/projects/proj/epics/EP-1/tasks", json=changed)

        r2 = await app_client.get("/api/projects/proj/epics/EP-1/tasks")
        data = r2.json()
        assert data["approved_hash"] == plan_hash  # stale record kept, harmless
        assert data["plan_approved"] is False

    async def test_status_only_change_keeps_approval(self, app_client: AsyncClient) -> None:
        """Dispatch flipping task status must not strip the approval."""
        await self._setup(app_client)
        await app_client.put("/api/projects/proj/epics/EP-1/tasks", json=_TASKS_BODY)
        r = await app_client.get("/api/projects/proj/epics/EP-1/tasks")
        plan_hash = r.json()["plan_hash"]
        await app_client.post(
            "/api/projects/proj/epics/EP-1/plan/approval", json={"tasks_hash": plan_hash}
        )

        in_progress = {
            "tasks": [
                {"id": "T1", "title": "Task One", "status": "in_progress", "contract": "do A"},
                {"id": "T2", "title": "Task Two", "status": "done", "contract": "do B"},
            ],
            "progress": {"done": 1, "total": 2},
        }
        await app_client.put("/api/projects/proj/epics/EP-1/tasks", json=in_progress)

        r2 = await app_client.get("/api/projects/proj/epics/EP-1/tasks")
        data = r2.json()
        assert data["plan_hash"] == plan_hash
        assert data["plan_approved"] is True

    async def test_delete_revokes_approval(self, app_client: AsyncClient) -> None:
        await self._setup(app_client)
        await app_client.put("/api/projects/proj/epics/EP-1/tasks", json=_TASKS_BODY)
        r = await app_client.get("/api/projects/proj/epics/EP-1/tasks")
        plan_hash = r.json()["plan_hash"]
        await app_client.post(
            "/api/projects/proj/epics/EP-1/plan/approval", json={"tasks_hash": plan_hash}
        )

        r2 = await app_client.delete("/api/projects/proj/epics/EP-1/plan/approval")
        assert r2.status_code == 204

        r3 = await app_client.get("/api/projects/proj/epics/EP-1/tasks")
        assert r3.json()["plan_approved"] is False
        assert r3.json()["approved_hash"] is None

        # Idempotent.
        r4 = await app_client.delete("/api/projects/proj/epics/EP-1/plan/approval")
        assert r4.status_code == 204

    async def test_unknown_epic_is_404(self, app_client: AsyncClient) -> None:
        await self._setup(app_client)
        r = await app_client.post(
            "/api/projects/proj/epics/EP-99/plan/approval",
            json={"tasks_hash": compute_plan_hash([])},
        )
        assert r.status_code == 404
        r2 = await app_client.delete("/api/projects/proj/epics/EP-99/plan/approval")
        assert r2.status_code == 404

    async def test_empty_plan_cannot_be_approved(self, app_client: AsyncClient) -> None:
        await self._setup(app_client)
        r = await app_client.post(
            "/api/projects/proj/epics/EP-1/plan/approval",
            json={"tasks_hash": compute_plan_hash([])},
        )
        assert r.status_code == 409
        assert "empty" in r.json()["detail"].lower()

    async def test_new_trial_revokes_approval_same_branch_keeps_it(
        self, app_client: AsyncClient
    ) -> None:
        """A NEW trial (fresh branch = fresh attempt) revokes the recorded
        approval; a same-branch continuation (same trial, new conversation)
        keeps it."""
        await self._setup(app_client)
        await app_client.put("/api/projects/proj/epics/EP-1/tasks", json=_TASKS_BODY)

        # Trial 1 (first manager trial) — created before any approval exists.
        r = await app_client.post(
            "/api/projects/proj/epics/EP-1/threads",
            json={"title": "Trial 1", "role": "manager"},
        )
        assert r.status_code == 201

        async def _approve() -> None:
            hash_ = (await app_client.get("/api/projects/proj/epics/EP-1/tasks")).json()[
                "plan_hash"
            ]
            resp = await app_client.post(
                "/api/projects/proj/epics/EP-1/plan/approval", json={"tasks_hash": hash_}
            )
            assert resp.status_code == 200

        async def _approved() -> bool:
            return bool(
                (await app_client.get("/api/projects/proj/epics/EP-1/tasks")).json()[
                    "plan_approved"
                ]
            )

        # Same-branch continuation keeps the approval.
        await _approve()
        r2 = await app_client.post(
            "/api/projects/proj/epics/EP-1/threads",
            json={"title": "", "role": "manager", "same_branch": True},
        )
        assert r2.status_code == 201
        assert await _approved() is True

        # A NEW trial (archive the active one, fresh branch) revokes it.
        await _approve()  # re-assert a live approval right before
        r3 = await app_client.post(
            "/api/projects/proj/epics/EP-1/threads",
            json={"title": "", "role": "manager", "archive_active": True},
        )
        assert r3.status_code == 201
        assert await _approved() is False

    async def test_gate_disabled_reports_plan_approved(
        self, app_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """YUKAR_REQUIRE_PLAN_APPROVAL=0 disables the gate everywhere: the
        orchestrator dispatches without approval, so GET /tasks must not
        report an approval as pending (the UI would render a pointless
        approve banner)."""
        await self._setup(app_client)
        await app_client.put("/api/projects/proj/epics/EP-1/tasks", json=_TASKS_BODY)

        monkeypatch.setenv("YUKAR_REQUIRE_PLAN_APPROVAL", "0")
        r = await app_client.get("/api/projects/proj/epics/EP-1/tasks")
        data = r.json()
        assert data["plan_approved"] is True
        assert data["approved_hash"] is None  # nothing recorded — gate is just off

        # With the gate enabled the same state reads as unapproved.
        monkeypatch.setenv("YUKAR_REQUIRE_PLAN_APPROVAL", "1")
        r2 = await app_client.get("/api/projects/proj/epics/EP-1/tasks")
        assert r2.json()["plan_approved"] is False
