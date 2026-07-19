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
from typing import Any

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
# Unit: plan strings must survive the YAML round-trip with an identical hash
# ---------------------------------------------------------------------------


class TestPlanStringRoundTripStability:
    """U+0085 (NEL) regression: ruamel emits NEL raw and the YAML parser folds
    it into a space on reload, so a plan containing NEL used to hash
    differently in memory than on disk — the UI said "approved" while the
    dispatch gate rejected forever.  The Task model now normalises the
    line-break foot-guns at the boundary."""

    def test_nel_crlf_cr_normalized_on_construction(self) -> None:
        t = Task(id="T1", title="a\x85b", contract="x\r\ny\rz")
        assert t.title == "a\nb"
        assert t.contract == "x\ny\nz"

    def test_normalized_on_attribute_assignment(self) -> None:
        """task_update mutates existing tasks via assignment — must normalise too."""
        t = _task()
        t.contract = "before\x85after"
        assert t.contract == "before\nafter"

    def test_depends_on_elements_normalized(self) -> None:
        t = _task(depends_on=["T\x850"])
        assert t.depends_on == ["T\n0"]

    async def test_plan_hash_survives_disk_round_trip(self, tmp_path: Path) -> None:
        """The exact reported bug: NEL in a contract made the on-disk hash
        differ from the in-memory hash, permanently."""
        from yukar.config import paths
        from yukar.models.task import TasksFile
        from yukar.storage import tasks_repo

        root = str(tmp_path)
        tf = TasksFile(
            tasks=[
                _task(contract="line one\x85line two"),
                _task(id="T2", title="ttl\x85ttl", contract="c\r\nd"),
            ]
        )
        paths.epic_yukar_dir(root, "proj", "EP-1").mkdir(parents=True, exist_ok=True)
        await tasks_repo.save_tasks(root, "proj", "EP-1", tf)
        loaded = await tasks_repo.get_tasks(root, "proj", "EP-1")
        assert compute_plan_hash(loaded.tasks) == compute_plan_hash(tf.tasks)

    async def test_plan_hash_round_trip_fuzz(self, tmp_path: Path) -> None:
        """Seeded fuzz over the character classes that break YAML emitters:
        backslash sequences (rg '\\bfoo\\b' style commands), control chars,
        YAML-1.1 line breaks, quotes, wide chars, long fold-boundary lines,
        trailing whitespace.  Every constructed Task must hash identically
        after a save/load round trip — this pins the invariant against future
        ruamel/format changes without enumerating characters by hand."""
        import random

        from yukar.config import paths
        from yukar.models.task import TasksFile
        from yukar.storage import tasks_repo

        rng = random.Random(20260719)
        atoms = [
            "\\b", "\\.", "\\\\", "\\", "rg -n '\\bfoo\\b' src/",
            "\x85", " ", " ", "\r\n", "\r", "\n", "\t",
            "\x08", "\x00", "\x1b", "\x7f", "﻿", " ",
            '"', "'", "`", "#", ": ", "- ", "|", ">", "&", "*", "%",
            "日本語テキスト", "😀", " ", "x" * 40, "字" * 30,
            "trailing space \n", " leading", "settings\\.yaml|repos/",
        ]
        root = str(tmp_path)
        paths.epic_yukar_dir(root, "proj", "EP-1").mkdir(parents=True, exist_ok=True)
        for i in range(60):
            s = "".join(rng.choice(atoms) for _ in range(rng.randint(1, 25)))
            tf = TasksFile(tasks=[_task(title=(s or "t")[:80] or "t", contract=s)])
            await tasks_repo.save_tasks(root, "proj", "EP-1", tf)
            loaded = await tasks_repo.get_tasks(root, "proj", "EP-1")
            assert compute_plan_hash(loaded.tasks) == compute_plan_hash(tf.tasks), (
                f"round-trip hash divergence at iteration {i}: {s!r}"
            )


# ---------------------------------------------------------------------------
# Unit: the dispatch gate must agree with the REST surface (disk is authority)
# ---------------------------------------------------------------------------


class TestDispatchGateReadsDisk:
    """_is_plan_approved must hash the plan from DISK — the same bytes the
    REST surface hashed when the user approved.  If it hashed the in-memory
    holder instead, any memory/disk divergence (inexact YAML round-trip, a
    failed task_update save) shows "approved" in the UI while dispatch
    rejects forever."""

    def _orchestrator(self, root: str) -> Any:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="Test",
            git_author_email="test@example.com",
        )
        orch._root = root
        orch._project_id = "proj"
        orch._epic_id = "EP-1"
        return orch

    async def _write_and_approve(self, root: str, tasks: list[Task]) -> None:
        from yukar.config import paths
        from yukar.models.task import TasksFile
        from yukar.storage import plan_approval_repo, tasks_repo

        paths.epic_yukar_dir(root, "proj", "EP-1").mkdir(parents=True, exist_ok=True)
        await tasks_repo.save_tasks(root, "proj", "EP-1", TasksFile(tasks=tasks))
        approval = PlanApproval(
            tasks_hash=compute_plan_hash(tasks), approved_at=datetime.now(UTC)
        )
        await plan_approval_repo.save_plan_approval(root, "proj", "EP-1", approval)

    async def test_gate_passes_when_disk_approved_even_if_memory_diverged(
        self, tmp_path: Path
    ) -> None:
        from yukar.models.task import TasksFile

        root = str(tmp_path)
        await self._write_and_approve(root, [_task()])

        orch = self._orchestrator(root)
        # Simulate a diverged in-memory holder (the pre-fix stuck state).
        orch._tasks_holder = [TasksFile(tasks=[_task(contract="diverged copy")])]
        assert await orch._is_plan_approved() is True

    async def test_gate_rejects_when_disk_unapproved_even_if_memory_matches(
        self, tmp_path: Path
    ) -> None:
        from yukar.models.task import TasksFile
        from yukar.storage import tasks_repo

        root = str(tmp_path)
        approved_plan = [_task()]
        await self._write_and_approve(root, approved_plan)
        # The plan on disk then changes (approval is stale now).
        await tasks_repo.save_tasks(
            root, "proj", "EP-1", TasksFile(tasks=[_task(contract="changed on disk")])
        )

        orch = self._orchestrator(root)
        # Memory still matches the recorded approval — disk must win anyway.
        orch._tasks_holder = [TasksFile(tasks=approved_plan)]
        assert await orch._is_plan_approved() is False


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
