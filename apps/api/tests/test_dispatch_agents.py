"""Tests for the dispatch ``agents`` argument (lifecycle redesign).

Task composition is NOT a persisted field — it is a per-dispatch-item
argument.  Allowed values:

- ``["worker", "evaluator"]`` (default when omitted) — the classic full cycle.
- ``["worker"]`` — work/investigation only: no Evaluator, no host commit; the
  Worker's final report is the deliverable and the task is marked done.
- ``["evaluator"]`` — evaluate the CURRENT worktree contents against the task
  contract; acceptance triggers the usual host commit.  No hermetic reset runs
  (the uncommitted contents ARE the evaluation subject).  Rejected when no
  worktree exists yet.

Everything else (empty list, unknown entries, non-list) rejects the item.
Execution order is always worker → evaluator.

Covered here:
- ``_parse_agents`` validation (pure unit).
- ``run_dispatch`` direct tests with stubbed worker/evaluator hooks and a real
  git worktree (per-path mechanics: events, threads, commits, resets).
- One FakeModel E2E through the orchestrator's real ``dispatch`` tool
  (worker-only investigation followed by an evaluator-only confirmation).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests._helpers import make_git_repo, run_until_parked

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> tuple[str, str, str]:
    root = str(tmp_path / "ws")
    project_id = "proj"
    epic_id = "EP-1"
    return root, project_id, epic_id


async def _bootstrap(root: str, project_id: str, epic_id: str, repo_path: Path) -> None:
    """Write the minimal YAML files needed for a dispatch run."""
    from yukar.models.epic import Epic
    from yukar.models.project import Project, Repo, RepoCommands
    from yukar.storage.epic_repo import save_epic
    from yukar.storage.project_repo import save_project, save_repo

    project = Project(id=project_id, name=project_id, status="active", repos=[repo_path.name])
    await save_project(root, project)

    repo = Repo(
        name=repo_path.name,
        path=str(repo_path),
        default_branch="main",
        commands=RepoCommands(allow=["git", "pytest"], deny=[]),
    )
    await save_repo(root, project_id, repo)

    epic = Epic(
        id=epic_id,
        slug="test-epic",
        title="Test Epic",
        description="A test epic for automated testing.",
        branch="yukar/ep-1-test-epic",
    )
    await save_epic(root, project_id, epic)


def _git_log_subjects(worktree: Path) -> str:
    r = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


# ---------------------------------------------------------------------------
# _parse_agents — pure validation unit tests
# ---------------------------------------------------------------------------


class TestParseAgents:
    def test_omitted_defaults_to_both(self) -> None:
        from yukar.agents.dispatch import _parse_agents

        assert _parse_agents({"task_id": "T1"}) == (True, True)

    def test_explicit_both(self) -> None:
        from yukar.agents.dispatch import _parse_agents

        assert _parse_agents({"agents": ["worker", "evaluator"]}) == (True, True)

    def test_order_is_normalised(self) -> None:
        """The argument order is irrelevant — execution is always worker → evaluator."""
        from yukar.agents.dispatch import _parse_agents

        assert _parse_agents({"agents": ["evaluator", "worker"]}) == (True, True)

    def test_worker_only(self) -> None:
        from yukar.agents.dispatch import _parse_agents

        assert _parse_agents({"agents": ["worker"]}) == (True, False)

    def test_evaluator_only(self) -> None:
        from yukar.agents.dispatch import _parse_agents

        assert _parse_agents({"agents": ["evaluator"]}) == (False, True)

    @pytest.mark.parametrize(
        "bad",
        [
            [],
            ["banana"],
            ["worker", "banana"],
            "worker",  # not a list
            [1],
            {"worker": True},
        ],
    )
    def test_invalid_values_return_error_message(self, bad: Any) -> None:
        from yukar.agents.dispatch import _parse_agents

        out = _parse_agents({"agents": bad})
        assert isinstance(out, str), f"expected an error message for {bad!r}, got {out!r}"
        assert "agents" in out


# ---------------------------------------------------------------------------
# run_dispatch — direct tests with stubbed hooks and a real git worktree
# ---------------------------------------------------------------------------


class _Harness:
    """Direct run_dispatch harness: real storage + git, stubbed agent hooks."""

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.worker_calls: list[dict[str, Any]] = []
        self.evaluator_calls: list[dict[str, Any]] = []
        self.call_order: list[str] = []
        # Behaviour knobs.
        self.worker_writes: dict[str, str] = {}  # rel path -> content
        self.worker_report: str = "worker report"
        self.eval_verdict: dict[str, Any] = {"accepted": True, "feedback": ""}

    async def run_worker(self, **kwargs: Any) -> dict[str, Any]:
        self.worker_calls.append(kwargs)
        self.call_order.append("worker")
        wt = Path(str(kwargs["ctx"].worktree_path))
        for rel, content in self.worker_writes.items():
            (wt / rel).write_text(content)
        return {"result": self.worker_report}

    async def run_evaluator(self, **kwargs: Any) -> dict[str, Any]:
        self.evaluator_calls.append(kwargs)
        self.call_order.append("evaluator")
        return dict(self.eval_verdict)


async def _make_ctx(
    root: str,
    project_id: str,
    epic_id: str,
    tasks: list[Any],
    harness: _Harness,
) -> Any:
    """Build a DispatchContext over real storage with the harness's stub hooks."""
    from yukar.agents.dispatch import DispatchContext, OrchestratorHooks
    from yukar.models.run import RunState
    from yukar.models.task import TasksFile
    from yukar.runs.scheduler import WorkerScheduler
    from yukar.storage import tasks_repo
    from yukar.storage.epic_repo import get_epic

    tf = TasksFile(tasks=tasks)
    await tasks_repo.save_tasks(root, project_id, epic_id, tf)

    epic = await get_epic(root, project_id, epic_id)
    assert epic is not None

    async def checkpoint() -> None:
        return None

    return DispatchContext(
        root=root,
        project_id=project_id,
        epic_id=epic_id,
        run_id="run-agents",
        epic=epic,
        state=RunState(run_id="run-agents", status="running"),
        tasks_holder=[tf],
        attempt_counts={},
        state_lock=asyncio.Lock(),
        scheduler=WorkerScheduler(),
        is_stopped=lambda: False,
        run_status="running",
        pub=harness.events.append,
        max_attempts=3,
        git_author_name="yukar",
        git_author_email="yukar@localhost",
        hooks=OrchestratorHooks(
            checkpoint=checkpoint,
            drain_pending=lambda: [],
            run_worker=harness.run_worker,
            run_evaluator=harness.run_evaluator,
        ),
        manager_thread_id="manager",
        manager_trial_id="manager",
        manager_branch=epic.branch,
    )


def _task(task_id: str, title: str, repo: str) -> Any:
    from yukar.models.task import Task

    return Task(id=task_id, title=title, status="todo", repo=repo, contract="do it")


class TestRunDispatchAgents:
    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_invalid_agents_item_rejected_without_side_effects(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """An invalid agents value rejects the item: no agents run, no attempt used."""
        from yukar.agents.dispatch import run_dispatch

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _Harness()
        ctx_d = await _make_ctx(
            root, project_id, epic_id, [_task("T1", "Bad agents", git_repo.name)], h
        )

        results = await run_dispatch(
            ctx_d,
            [{"task_id": "T1", "repo": git_repo.name, "agents": ["worker", "banana"]}],
        )

        assert results[0]["accepted"] is False
        assert results[0]["status"] == "rejected"
        assert "agents" in results[0]["reason"]
        assert h.worker_calls == [] and h.evaluator_calls == []
        assert ctx_d.attempt_counts == {}, "a rejected item must not consume an attempt"
        assert ctx_d.tasks_holder[0].tasks[0].status == "todo"

    async def test_empty_agents_item_rejected(self, git_repo: Path, tmp_path: Path) -> None:
        from yukar.agents.dispatch import run_dispatch

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _Harness()
        ctx_d = await _make_ctx(
            root, project_id, epic_id, [_task("T1", "Empty agents", git_repo.name)], h
        )

        results = await run_dispatch(
            ctx_d, [{"task_id": "T1", "repo": git_repo.name, "agents": []}]
        )

        assert results[0]["status"] == "rejected"
        assert "agents" in results[0]["reason"]
        assert h.worker_calls == [] and h.evaluator_calls == []

    async def test_worker_only_no_evaluator_no_commit_task_done(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """agents=["worker"]: Evaluator never starts, nothing is committed, the
        Worker's report comes back as feedback, and the task goes done."""
        from yukar.agents.dispatch import run_dispatch
        from yukar.config import paths as p
        from yukar.storage import tasks_repo, threads_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _Harness()
        h.worker_writes = {"notes.md": "investigation notes\n"}
        h.worker_report = "INVESTIGATION REPORT: the flag is unused."
        ctx_d = await _make_ctx(
            root, project_id, epic_id, [_task("T1", "Investigate flag", git_repo.name)], h
        )

        results = await run_dispatch(
            ctx_d, [{"task_id": "T1", "repo": git_repo.name, "agents": ["worker"]}]
        )

        # Verdict: done, feedback = the Worker's final report, no eval id.
        assert results[0]["accepted"] is True
        assert results[0]["status"] == "done"
        assert results[0]["feedback"] == "INVESTIGATION REPORT: the flag is unused."
        assert results[0]["worker_id"] is not None
        assert results[0]["eval_id"] is None

        # Evaluator never ran.
        assert h.evaluator_calls == []
        event_types = [getattr(ev, "type", None) for ev in h.events]
        assert "worker_started" in event_types
        assert "worker_completed" in event_types
        assert "evaluator_started" not in event_types
        assert "eval_result" not in event_types

        # Task done and persisted.
        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        assert tf.tasks[0].status == "done"

        # No host commit — the produced file stays uncommitted in the worktree.
        worktree = p.worktree_dir(root, project_id, epic_id, "manager", git_repo.name)
        assert (worktree / "notes.md").exists()
        assert "T1:" not in _git_log_subjects(worktree)

        # Worker thread resolved; no evaluator thread was registered.
        threads = await threads_repo.get_threads(root, project_id, epic_id)
        roles = {t.id: (t.role, t.status) for t in threads.threads}
        worker_entries = [v for v in roles.values() if v[0] == "worker"]
        assert worker_entries == [("worker", "resolved")]
        assert not any(role == "evaluator" for (role, _s) in roles.values())

    async def test_worker_only_empty_report_is_rejected_not_silently_done(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """A silent Worker produced no deliverable: the report IS the product
        of a worker-only dispatch, so an empty report must not mark the task
        done with an empty feedback (the Manager decides how to proceed)."""
        from yukar.agents.dispatch import run_dispatch
        from yukar.storage import tasks_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _Harness()
        h.worker_report = ""  # the Worker says nothing
        ctx_d = await _make_ctx(
            root, project_id, epic_id, [_task("T1", "Investigate flag", git_repo.name)], h
        )

        results = await run_dispatch(
            ctx_d, [{"task_id": "T1", "repo": git_repo.name, "agents": ["worker"]}]
        )

        assert results[0]["accepted"] is False
        assert "no report" in results[0]["feedback"]
        assert h.evaluator_calls == []

        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        assert tf.tasks[0].status != "done"

    async def test_evaluator_only_accept_commits_and_done(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """agents=["evaluator"]: no Worker runs; acceptance host-commits the
        current worktree contents (the worker-only leftovers)."""
        from yukar.agents.dispatch import ensure_worktree_for_repo, run_dispatch
        from yukar.storage import tasks_repo, threads_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _Harness()
        h.eval_verdict = {"accepted": True, "feedback": "looks correct"}
        ctx_d = await _make_ctx(
            root, project_id, epic_id, [_task("T2", "Persist the change", git_repo.name)], h
        )

        # Pre-create the worktree and leave uncommitted contents in it
        # (simulating an earlier worker-only attempt).
        worktree = await ensure_worktree_for_repo(
            root,
            project_id,
            epic_id,
            "manager",
            ctx_d.manager_branch,
            git_repo.name,
            ctx_d.state_lock,
            ctx_d.epic,
        )
        (worktree / "feature.py").write_text("VALUE = 42\n")

        results = await run_dispatch(
            ctx_d, [{"task_id": "T2", "repo": git_repo.name, "agents": ["evaluator"]}]
        )

        assert results[0]["accepted"] is True
        assert results[0]["status"] == "done"
        assert results[0]["worker_id"] is None
        assert results[0]["eval_id"] is not None

        # No Worker ran.
        assert h.worker_calls == []
        event_types = [getattr(ev, "type", None) for ev in h.events]
        assert "worker_started" not in event_types
        assert "evaluator_started" in event_types
        eval_started = next(
            ev for ev in h.events if getattr(ev, "type", None) == "evaluator_started"
        )
        assert eval_started.worker_id == ""

        # Host commit landed with the uncommitted contents.
        subjects = _git_log_subjects(worktree)
        assert "T2: Persist the change" in subjects
        show = subprocess.run(
            ["git", "show", "HEAD:feature.py"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        assert show.returncode == 0
        assert show.stdout == "VALUE = 42\n"

        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        assert tf.tasks[0].status == "done"

        # Evaluator thread parented to the manager conversation (no worker).
        threads = await threads_repo.get_threads(root, project_id, epic_id)
        eval_threads = [t for t in threads.threads if t.role == "evaluator"]
        assert len(eval_threads) == 1
        assert eval_threads[0].parent_thread_id == "manager"
        assert eval_threads[0].status == "resolved"

    async def test_evaluator_only_reject_keeps_worktree_and_returns_feedback(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """A rejected evaluator-only attempt returns feedback, leaves the task
        todo, commits nothing — and does NOT reset the worktree contents."""
        from yukar.agents.dispatch import ensure_worktree_for_repo, run_dispatch
        from yukar.storage import tasks_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _Harness()
        h.eval_verdict = {"accepted": False, "feedback": "missing tests"}
        ctx_d = await _make_ctx(
            root, project_id, epic_id, [_task("T2", "Persist the change", git_repo.name)], h
        )

        worktree = await ensure_worktree_for_repo(
            root,
            project_id,
            epic_id,
            "manager",
            ctx_d.manager_branch,
            git_repo.name,
            ctx_d.state_lock,
            ctx_d.epic,
        )
        (worktree / "feature.py").write_text("VALUE = 42\n")

        results = await run_dispatch(
            ctx_d, [{"task_id": "T2", "repo": git_repo.name, "agents": ["evaluator"]}]
        )

        assert results[0]["accepted"] is False
        assert results[0]["status"] == "needs_fix"
        assert results[0]["feedback"] == "missing tests"

        # No commit; the evaluation subject is still there (no hermetic reset).
        assert "T2:" not in _git_log_subjects(worktree)
        assert (worktree / "feature.py").exists(), (
            "evaluator-only must NOT run the hermetic reset — the uncommitted "
            "contents are the evaluation subject"
        )

        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        assert tf.tasks[0].status == "todo"

    async def test_evaluator_only_without_worktree_rejected(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """agents=["evaluator"] with no worktree yet: rejected (nothing to
        evaluate), no attempt consumed, no evaluator started."""
        from yukar.agents.dispatch import run_dispatch
        from yukar.storage import tasks_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _Harness()
        ctx_d = await _make_ctx(
            root, project_id, epic_id, [_task("T1", "Evaluate nothing", git_repo.name)], h
        )

        results = await run_dispatch(
            ctx_d, [{"task_id": "T1", "repo": git_repo.name, "agents": ["evaluator"]}]
        )

        assert results[0]["accepted"] is False
        assert results[0]["status"] == "rejected"
        assert "worktree" in results[0]["reason"]
        assert h.worker_calls == [] and h.evaluator_calls == []
        assert ctx_d.attempt_counts == {}, "a rejected item must not consume an attempt"
        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        assert tf.tasks[0].status == "todo"

    async def test_default_agents_runs_worker_then_evaluator_and_commits(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """Omitting agents keeps the classic behaviour: worker → evaluator,
        host commit on acceptance."""
        from yukar.agents.dispatch import run_dispatch
        from yukar.config import paths as p

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _Harness()
        h.worker_writes = {"impl.py": "x = 1\n"}
        h.eval_verdict = {"accepted": True, "feedback": ""}
        ctx_d = await _make_ctx(
            root, project_id, epic_id, [_task("T1", "Implement x", git_repo.name)], h
        )

        results = await run_dispatch(ctx_d, [{"task_id": "T1", "repo": git_repo.name}])

        assert results[0]["accepted"] is True
        assert results[0]["status"] == "done"
        assert h.call_order == ["worker", "evaluator"], (
            "execution order must be worker → evaluator"
        )
        worktree = p.worktree_dir(root, project_id, epic_id, "manager", git_repo.name)
        assert "T1: Implement x" in _git_log_subjects(worktree)

    async def test_agents_order_in_argument_is_irrelevant(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """["evaluator", "worker"] behaves exactly like the default pair."""
        from yukar.agents.dispatch import run_dispatch

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _Harness()
        h.worker_writes = {"impl.py": "x = 1\n"}
        ctx_d = await _make_ctx(
            root, project_id, epic_id, [_task("T1", "Implement x", git_repo.name)], h
        )

        results = await run_dispatch(
            ctx_d,
            [{"task_id": "T1", "repo": git_repo.name, "agents": ["evaluator", "worker"]}],
        )

        assert results[0]["status"] == "done"
        assert h.call_order == ["worker", "evaluator"]

    async def test_worker_bearing_attempt_resets_worker_only_leftovers(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """Documented semantics: files from a worker-only attempt (never
        committed) are wiped by the hermetic reset of the next worker-bearing
        attempt — only evaluator-accepted work persists."""
        from yukar.agents.dispatch import run_dispatch
        from yukar.config import paths as p

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _Harness()
        ctx_d = await _make_ctx(
            root,
            project_id,
            epic_id,
            [
                _task("T1", "Investigate", git_repo.name),
                _task("T2", "Implement", git_repo.name),
            ],
            h,
        )

        # 1) worker-only leaves an uncommitted file.
        h.worker_writes = {"leftover.txt": "scratch\n"}
        await run_dispatch(
            ctx_d, [{"task_id": "T1", "repo": git_repo.name, "agents": ["worker"]}]
        )
        worktree = p.worktree_dir(root, project_id, epic_id, "manager", git_repo.name)
        assert (worktree / "leftover.txt").exists()

        # 2) a default (worker-bearing) attempt resets the tree first.
        h.worker_writes = {"impl.py": "x = 1\n"}
        results = await run_dispatch(ctx_d, [{"task_id": "T2", "repo": git_repo.name}])

        assert results[0]["status"] == "done"
        assert not (worktree / "leftover.txt").exists(), (
            "worker-only leftovers must be wiped by the next worker-bearing attempt"
        )
        # The T2 commit must not contain the leftover either.
        show = subprocess.run(
            ["git", "show", "--stat", "HEAD"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        assert "leftover.txt" not in show.stdout


# ---------------------------------------------------------------------------
# E2E through the orchestrator's real dispatch tool (FakeModel)
# ---------------------------------------------------------------------------


class TestDispatchAgentsE2E:
    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_worker_only_then_evaluator_only(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """Scripted Manager: T1 via agents=["worker"] (investigation — no
        Evaluator, no commit), then T2 via agents=["evaluator"] (confirm the
        leftover change — host commit on acceptance)."""
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config import paths as p
        from yukar.config.settings import LLMSettings
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import tasks_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script: list[Any] = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Investigate",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={
                    "items": [
                        {"task_id": "T1", "repo": git_repo.name, "agents": ["worker"]}
                    ]
                },
            ),
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T2",
                    "title": "Confirm change",
                    "status": "todo",
                    "repo": git_repo.name,
                },
            ),
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={
                    "items": [
                        {"task_id": "T2", "repo": git_repo.name, "agents": ["evaluator"]}
                    ]
                },
            ),
            TextTurn("Investigated and confirmed."),
        ]
        # The (single) worker writes a file and reports; the (single)
        # evaluator accepts the leftover contents.
        worker_script = [
            ToolUseTurn(
                tool_name="fs_write",
                tool_input={"path": "impl.py", "content": "x = 1\n"},
            ),
            TextTurn("Wrote impl.py while investigating."),
        ]
        evaluator_script = [
            ToolUseTurn(
                tool_name="submit_verdict",
                tool_input={"accepted": True, "feedback": ""},
            ),
            TextTurn("Accepted."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            r = role or "worker"
            if r == "manager":
                return FakeModel(script=list(manager_script))
            if r == "worker":
                return FakeModel(script=list(worker_script))
            return FakeModel(script=list(evaluator_script))

        events_received: list[Any] = []

        async def _collect() -> None:
            async for ev in event_bus.event_stream(project_id, epic_id):
                events_received.append(ev)

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            orch = EpicOrchestrator(
                llm_settings=LLMSettings(provider="fake"),
                git_author_name="yukar",
                git_author_email="yukar@localhost",
                require_plan_approval=False,
            )
            await run_until_parked(orch, root, project_id, epic_id, "run-agents-e2e")

        await asyncio.wait_for(collector, timeout=5.0)

        # Exactly one worker (for T1) and one evaluator (for T2) started.
        worker_started = [
            ev for ev in events_received if getattr(ev, "type", None) == "worker_started"
        ]
        eval_started = [
            ev for ev in events_received if getattr(ev, "type", None) == "evaluator_started"
        ]
        assert len(worker_started) == 1
        assert worker_started[0].task_id == "T1"
        assert len(eval_started) == 1
        assert eval_started[0].task_id == "T2"
        assert eval_started[0].worker_id == ""  # no Worker ran for T2

        # Both tasks are done.
        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        statuses = {t.id: t.status for t in tf.tasks}
        assert statuses == {"T1": "done", "T2": "done"}

        # Only the evaluator-accepted attempt committed — as T2, not T1.
        worktree = p.worktree_dir(root, project_id, epic_id, "manager", git_repo.name)
        subjects = _git_log_subjects(worktree)
        assert "T2: Confirm change" in subjects
        assert "T1:" not in subjects
        show = subprocess.run(
            ["git", "show", "HEAD:impl.py"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        assert show.returncode == 0
        assert show.stdout == "x = 1\n"
