"""Robustness of the dispatch loop against irregular agent failures.

Covers the four user-reported weaknesses around Worker/Evaluator failures:

- An Evaluator that ends WITHOUT calling ``submit_verdict`` (turn/token limit,
  text-only answer) must never count as an acceptance: nothing is committed
  and the Manager sees an explicit ``status=failed`` / ``no_verdict`` item.
- An Evaluator exception (max_tokens, context overflow, provider error) is
  caught like a Worker exception: the attempt fails explicitly instead of
  aborting the whole dispatch call with an opaque error string.
- A provider-auth failure (expired STS token, bad API key) suspends dispatch
  for the rest of the run — every further model call would fail identically.
- A verbatim re-dispatch after a Worker max_tokens / context_overflow failure
  is rejected without consuming an attempt (the hermetic reset discarded the
  partial work, so the identical item would hit the identical limit).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests._helpers import make_git_repo, run_until_parked
from tests.test_dispatch_agents import _bootstrap, _Harness, _make_ctx, _make_workspace, _task

# ---------------------------------------------------------------------------
# classify_agent_failure — pure unit tests
# ---------------------------------------------------------------------------


def _client_error(code: str) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": f"{code} happened"}}, "ConverseStream")


class TestClassifyAgentFailure:
    def test_max_tokens(self) -> None:
        from strands.types.exceptions import MaxTokensReachedException

        from yukar.agents.failures import classify_agent_failure

        f = classify_agent_failure(MaxTokensReachedException("cut off"))
        assert f.kind == "max_tokens"
        assert f.retry_verbatim_futile is True
        assert f.run_fatal is False

    def test_context_overflow(self) -> None:
        from strands.types.exceptions import ContextWindowOverflowException

        from yukar.agents.failures import classify_agent_failure

        f = classify_agent_failure(ContextWindowOverflowException("too big"))
        assert f.kind == "context_overflow"
        assert f.retry_verbatim_futile is True

    def test_throttled(self) -> None:
        from strands.types.exceptions import ModelThrottledException

        from yukar.agents.failures import classify_agent_failure

        f = classify_agent_failure(ModelThrottledException("slow down"))
        assert f.kind == "throttled"
        assert f.run_fatal is False

    @pytest.mark.parametrize(
        "code",
        [
            "ExpiredTokenException",
            "ExpiredToken",
            "UnrecognizedClientException",
            "AccessDeniedException",
        ],
    )
    def test_bedrock_auth_client_error(self, code: str) -> None:
        from yukar.agents.failures import classify_agent_failure

        f = classify_agent_failure(_client_error(code))
        assert f.kind == "provider_auth", code
        assert f.run_fatal is True
        assert code in f.detail

    def test_botocore_no_credentials(self) -> None:
        from botocore.exceptions import NoCredentialsError

        from yukar.agents.failures import classify_agent_failure

        assert classify_agent_failure(NoCredentialsError()).kind == "provider_auth"

    def test_anthropic_authentication_error_by_name(self) -> None:
        """Detection is name-based, so the anthropic SDK is not imported here."""
        from yukar.agents.failures import classify_agent_failure

        AuthenticationError = type("AuthenticationError", (Exception,), {})
        assert classify_agent_failure(AuthenticationError("bad key")).kind == "provider_auth"

    def test_wrapped_cause_is_recognised(self) -> None:
        """A provider error hidden behind a wrapper (e.g. strands EventLoopException)."""
        from strands.types.exceptions import EventLoopException

        from yukar.agents.failures import classify_agent_failure

        inner = _client_error("ExpiredTokenException")
        try:
            try:
                raise inner
            except Exception as e:
                raise EventLoopException(e) from e
        except EventLoopException as wrapped:
            f = classify_agent_failure(wrapped)
        assert f.kind == "provider_auth"

    def test_other_bedrock_client_error_is_provider_error(self) -> None:
        from yukar.agents.failures import classify_agent_failure

        f = classify_agent_failure(_client_error("ServiceUnavailableException"))
        assert f.kind == "provider_error"
        assert f.run_fatal is False

    def test_unknown_exception_is_other(self) -> None:
        from yukar.agents.failures import classify_agent_failure

        f = classify_agent_failure(RuntimeError("boom"))
        assert f.kind == "other"
        assert f.exc_name == "RuntimeError"

    def test_guidance_never_reads_as_success(self) -> None:
        from yukar.agents.failures import AgentFailure, failure_guidance

        for kind in ("max_tokens", "context_overflow", "throttled", "provider_auth", "no_verdict"):
            text = failure_guidance(AgentFailure(kind, "X", "d"), role="worker", task_id="T9")
            assert "ATTEMPT FAILED" in text
            assert "nothing was committed" in text
            assert "T9" in text


# ---------------------------------------------------------------------------
# run_dispatch — evaluator failures, provider fault, verbatim-retry gate
# ---------------------------------------------------------------------------


class _RaisingHarness(_Harness):
    """Harness whose evaluator / worker can raise or return a no-verdict."""

    def __init__(self) -> None:
        super().__init__()
        self.eval_exc: Exception | None = None
        self.worker_exc: Exception | None = None
        # Files the Evaluator "leaves behind" (run_tests artifacts), written
        # during its run — relative path → content.
        self.eval_artifacts: dict[str, str] = {}
        # Files the Worker writes BEFORE raising worker_exc (half-written output).
        self.worker_writes_before_exc: dict[str, str] = {}

    async def run_worker(self, **kwargs: Any) -> dict[str, Any]:
        if self.worker_exc is not None:
            self.worker_calls.append(kwargs)
            self.call_order.append("worker")
            wt = Path(str(kwargs["ctx"].worktree_path))
            for rel, content in self.worker_writes_before_exc.items():
                (wt / rel).parent.mkdir(parents=True, exist_ok=True)
                (wt / rel).write_text(content)
            raise self.worker_exc
        # Like the base harness, but nested paths ("src/a.py") are supported.
        self.worker_calls.append(kwargs)
        self.call_order.append("worker")
        wt = Path(str(kwargs["ctx"].worktree_path))
        for rel, content in self.worker_writes.items():
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(content)
        return {"result": self.worker_report}

    async def run_evaluator(self, **kwargs: Any) -> dict[str, Any]:
        wt = Path(str(kwargs["ctx"].worktree_path))
        for rel, content in self.eval_artifacts.items():
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(content)
        if self.eval_exc is not None:
            self.evaluator_calls.append(kwargs)
            self.call_order.append("evaluator")
            raise self.eval_exc
        return await super().run_evaluator(**kwargs)


def _worktree(root: str, project_id: str, epic_id: str, repo: str) -> Path:
    from yukar.config import paths as p

    return p.worktree_dir(root, project_id, epic_id, "manager", repo)


def _commit_count(wt: Path) -> int:
    import subprocess

    r = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=wt, capture_output=True, text=True
    )
    return int(r.stdout.strip())


class TestEvaluatorFailures:
    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_evaluator_without_verdict_is_not_accepted(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """submitted=False → status=failed / no_verdict, nothing committed, task back to todo."""
        from yukar.agents.dispatch import run_dispatch
        from yukar.models.events import EvalResultEvent

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _Harness()
        h.worker_writes = {"a.py": "print(1)\n"}
        h.eval_verdict = {"accepted": False, "feedback": "I looked at it.", "submitted": False}
        ctx_d = await _make_ctx(root, project_id, epic_id, [_task("T1", "A", git_repo.name)], h)

        results = await run_dispatch(ctx_d, [{"task_id": "T1", "repo": git_repo.name}])

        r = results[0]
        assert r["accepted"] is False
        assert r["status"] == "failed"
        assert r["reason"] == "evaluator failed: no_verdict"
        assert "submit_verdict" in r["feedback"]
        assert "I looked at it." in r["feedback"]
        assert ctx_d.tasks_holder[0].tasks[0].status == "todo"
        # No host commit happened (only the initial commit).
        assert _commit_count(_worktree(root, project_id, epic_id, git_repo.name)) == 1
        ev = [e for e in h.events if isinstance(e, EvalResultEvent)]
        assert ev and ev[-1].accepted is False

    async def test_evaluator_exception_becomes_failed_item(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """An Evaluator MaxTokensReachedException no longer escapes the dispatch call."""
        from strands.types.exceptions import MaxTokensReachedException

        from yukar.agents.dispatch import run_dispatch
        from yukar.models.events import EvalResultEvent
        from yukar.storage import threads_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _RaisingHarness()
        h.worker_writes = {"a.py": "print(1)\n"}
        h.eval_exc = MaxTokensReachedException("evaluator cut off")
        ctx_d = await _make_ctx(root, project_id, epic_id, [_task("T1", "A", git_repo.name)], h)

        results = await run_dispatch(ctx_d, [{"task_id": "T1", "repo": git_repo.name}])

        r = results[0]
        assert r["status"] == "failed"
        assert r["reason"] == "evaluator failed: max_tokens"
        assert "task_update" in r["feedback"]
        assert r["eval_id"] is not None
        assert ctx_d.tasks_holder[0].tasks[0].status == "todo"
        assert _commit_count(_worktree(root, project_id, epic_id, git_repo.name)) == 1
        # The evaluator thread is marked failed and an eval_result(accepted=False) was published.
        tf = await threads_repo.get_threads(root, project_id, epic_id)
        assert next(t for t in tf.threads if t.id == r["eval_id"]).status == "failed"
        # The Worker finished normally — its thread is NOT flagged as failed.
        assert next(t for t in tf.threads if t.id == r["worker_id"]).status != "failed"
        ev = [e for e in h.events if isinstance(e, EvalResultEvent)]
        assert ev and ev[-1].accepted is False and "ATTEMPT FAILED" in ev[-1].feedback
        # The staged worker changes survive for an evaluator-only re-run.
        assert (_worktree(root, project_id, epic_id, git_repo.name) / "a.py").exists()

    async def test_same_subject_reevaluation_after_no_verdict_is_rejected(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """After a no_verdict, re-evaluating the SAME files under the same contract is
        rejected without an attempt; changing the contract lifts the gate."""
        from yukar.agents.dispatch import run_dispatch

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _Harness()
        h.worker_writes = {"a.py": "print(1)\n", "b.py": "print(2)\n"}
        h.eval_verdict = {"accepted": False, "feedback": "", "submitted": False}
        ctx_d = await _make_ctx(root, project_id, epic_id, [_task("T1", "A", git_repo.name)], h)
        first = await run_dispatch(ctx_d, [{"task_id": "T1", "repo": git_repo.name}])
        assert first[0]["status"] == "failed"
        assert "a.py" in first[0]["feedback"] and "b.py" in first[0]["feedback"]

        h.eval_verdict = {"accepted": True, "feedback": "pytest passed", "submitted": True}
        whole = await run_dispatch(
            ctx_d, [{"task_id": "T1", "repo": git_repo.name, "agents": ["evaluator"]}]
        )
        assert whole[0]["status"] == "rejected"
        assert "same subject" in whole[0]["reason"]
        assert ctx_d.attempt_counts["T1"] == 1
        assert len(h.evaluator_calls) == 1

        ctx_d.tasks_holder[0].tasks[0].contract = "only a.py matters now"
        again = await run_dispatch(
            ctx_d, [{"task_id": "T1", "repo": git_repo.name, "agents": ["evaluator"]}]
        )
        assert again[0]["status"] == "done"
        assert _commit_count(_worktree(root, project_id, epic_id, git_repo.name)) == 2
        assert "T1" not in ctx_d.failure_log


class TestProviderFaultSuspension:
    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_auth_failure_suspends_further_dispatch(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        from yukar.agents.dispatch import run_dispatch

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _RaisingHarness()
        h.worker_exc = _client_error("ExpiredTokenException")
        tasks = [_task("T1", "A", git_repo.name), _task("T2", "B", git_repo.name)]
        ctx_d = await _make_ctx(root, project_id, epic_id, tasks, h)

        first = await run_dispatch(ctx_d, [{"task_id": "T1", "repo": git_repo.name}])
        assert first[0]["status"] == "failed"
        assert first[0]["reason"] == "worker failed: provider_auth"
        assert "SUSPENDED" in first[0]["feedback"]
        assert ctx_d.provider_fault and "ExpiredTokenException" in ctx_d.provider_fault[0]

        # Every later item is rejected up front: no agent runs, no attempt is consumed.
        h.worker_exc = None
        second = await run_dispatch(ctx_d, [{"task_id": "T2", "repo": git_repo.name}])
        assert second[0]["status"] == "rejected"
        assert "SUSPENDED" in second[0]["reason"]
        assert "ExpiredTokenException" in second[0]["reason"]
        assert ctx_d.attempt_counts.get("T2", 0) == 0
        assert len(h.worker_calls) == 1

    async def test_dispatch_tool_rejects_while_provider_faulted(self) -> None:
        """The orchestrator-level dispatch tool short-circuits on a provider fault."""
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="yukar",
            git_author_email="yukar@localhost",
            require_plan_approval=False,
        )
        orch._provider_fault.append("ClientError: ExpiredTokenException")
        tool = orch._make_dispatch_tool()
        out = await tool._tool_func(items=[{"task_id": "T1"}, {"task_id": "T2"}])
        assert [o["task_id"] for o in out] == ["T1", "T2"]
        assert all(o["status"] == "rejected" and "SUSPENDED" in o["reason"] for o in out)


class TestVerbatimRetryGate:
    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_identical_redispatch_after_max_tokens_is_rejected(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        from strands.types.exceptions import MaxTokensReachedException

        from yukar.agents.dispatch import run_dispatch

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _RaisingHarness()
        h.worker_exc = MaxTokensReachedException("cut off")
        ctx_d = await _make_ctx(root, project_id, epic_id, [_task("T1", "A", git_repo.name)], h)

        first = await run_dispatch(ctx_d, [{"task_id": "T1", "repo": git_repo.name}])
        assert first[0]["status"] == "failed"
        assert first[0]["reason"] == "worker failed: max_tokens"
        assert ctx_d.attempt_counts["T1"] == 1

        # Same contract, same (empty) feedback, same agents → rejected, no attempt used.
        again = await run_dispatch(ctx_d, [{"task_id": "T1", "repo": git_repo.name}])
        assert again[0]["status"] == "rejected"
        assert "identical" in again[0]["reason"]
        assert ctx_d.attempt_counts["T1"] == 1
        assert len(h.worker_calls) == 1

        # Different feedback changes the approach → allowed through (and succeeds).
        h.worker_exc = None
        h.worker_writes = {"a.py": "print(1)\n"}
        h.eval_verdict = {"accepted": True, "feedback": "ok", "submitted": True}
        changed = await run_dispatch(
            ctx_d,
            [{"task_id": "T1", "repo": git_repo.name, "feedback": "write in smaller pieces"}],
        )
        assert changed[0]["status"] == "done"
        assert ctx_d.attempt_counts["T1"] == 2

    async def test_contract_change_lifts_the_gate(self, git_repo: Path, tmp_path: Path) -> None:
        from strands.types.exceptions import ContextWindowOverflowException

        from yukar.agents.dispatch import run_dispatch

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _RaisingHarness()
        h.worker_exc = ContextWindowOverflowException("overflow")
        ctx_d = await _make_ctx(root, project_id, epic_id, [_task("T1", "A", git_repo.name)], h)
        assert (await run_dispatch(ctx_d, [{"task_id": "T1", "repo": git_repo.name}]))[0][
            "status"
        ] == "failed"

        ctx_d.tasks_holder[0].tasks[0].contract = "do only the first half"
        h.worker_exc = None
        h.worker_writes = {"half.py": "x = 1\n"}
        h.eval_verdict = {"accepted": True, "feedback": "ok", "submitted": True}
        out = await run_dispatch(ctx_d, [{"task_id": "T1", "repo": git_repo.name}])
        assert out[0]["status"] == "done"

    async def test_throttled_failure_does_not_gate_retry(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        from strands.types.exceptions import ModelThrottledException

        from yukar.agents.dispatch import run_dispatch

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _RaisingHarness()
        h.worker_exc = ModelThrottledException("slow down")
        ctx_d = await _make_ctx(root, project_id, epic_id, [_task("T1", "A", git_repo.name)], h)
        first = await run_dispatch(ctx_d, [{"task_id": "T1", "repo": git_repo.name}])
        assert first[0]["status"] == "failed"
        assert first[0]["reason"] == "worker failed: throttled"
        assert not ctx_d.provider_fault

        h.worker_exc = None
        h.worker_writes = {"a.py": "print(1)\n"}
        h.eval_verdict = {"accepted": True, "feedback": "ok", "submitted": True}
        second = await run_dispatch(ctx_d, [{"task_id": "T1", "repo": git_repo.name}])
        assert second[0]["status"] == "done"


# ---------------------------------------------------------------------------
# FakeModel end-to-end: an Evaluator script that never submits a verdict
# ---------------------------------------------------------------------------


class TestEvaluatorNoVerdictThroughOrchestrator:
    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def test_text_only_evaluator_does_not_commit(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """Real run_evaluator: a text-only Evaluator yields no verdict → not done."""
        from unittest.mock import patch

        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn, ToolUseTurn
        from yukar.storage import tasks_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)

        manager_script = [
            ToolUseTurn(
                tool_name="task_update",
                tool_input={
                    "task_id": "T1",
                    "title": "Write hello.py",
                    "status": "todo",
                    "repo": git_repo.name,
                    "contract": "Create hello.py.",
                },
            ),
            ToolUseTurn(
                tool_name="dispatch",
                tool_input={"items": [{"task_id": "T1", "repo": git_repo.name}]},
            ),
            TextTurn("Reporting."),
        ]
        worker_script = [
            ToolUseTurn(
                tool_name="fs_write",
                tool_input={"path": "hello.py", "content": "print('hello')\n"},
            ),
            TextTurn("Implemented hello.py."),
        ]
        # Evaluator reads the diff and then just talks — never submit_verdict.
        evaluator_script = [
            ToolUseTurn(tool_name="read_diff", tool_input={"staged": True}),
            TextTurn("Everything looks green to me."),
        ]

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            if role == "manager":
                return FakeModel(script=list(manager_script))
            if role == "worker":
                return FakeModel(script=list(worker_script))
            return FakeModel(script=list(evaluator_script))

        with patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model):
            orch = EpicOrchestrator(
                llm_settings=LLMSettings(provider="fake"),
                git_author_name="yukar",
                git_author_email="yukar@localhost",
                require_plan_approval=False,
            )
            await run_until_parked(orch, root, project_id, epic_id, "run-noverdict")

        tf = await tasks_repo.get_tasks(root, project_id, epic_id)
        assert tf.tasks[0].status == "todo", "a verdict-less evaluation must not mark the task done"
        assert _commit_count(_worktree(root, project_id, epic_id, git_repo.name)) == 1
        assert "T1" in orch._dispatch_failure_log
        assert orch._dispatch_failure_log["T1"].failure.kind == "no_verdict"


# Keep asyncio imported for harness type parity with test_dispatch_agents.
_ = asyncio


# ---------------------------------------------------------------------------
# Scoped evaluation — splitting an oversized evaluation into parts
# ---------------------------------------------------------------------------


class TestReadDiffScoping:
    async def _ctx(self, wt: Path) -> Any:
        from yukar.agents.context import AgentContext

        return await AgentContext.create(
            project_id="p",
            epic_id="e",
            repo_name=wt.name,
            worktree_path=wt,
            workspace_root=str(wt.parent),
            allow=[],
            deny=[],
        )

    @staticmethod
    def _stage(wt: Path, files: dict[str, str]) -> None:
        import subprocess

        for rel, content in files.items():
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(content)
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)

    async def test_paths_limits_the_diff(self, tmp_path: Path) -> None:
        from yukar.agents.tools.evaluator_tools import make_evaluator_tools

        wt = make_git_repo(tmp_path, "r1")
        self._stage(wt, {"a.py": "a = 1\n", "b.py": "b = 2\n"})
        read_diff, _ = make_evaluator_tools(await self._ctx(wt))
        out = await read_diff(paths=["a.py"])
        assert out["status"] == "success"
        assert "a = 1" in out["diff"] and "b = 2" not in out["diff"]
        assert out["files"] == ["a.py"]
        assert out["truncated"] is False

    async def test_bad_paths_are_rejected(self, tmp_path: Path) -> None:
        from yukar.agents.tools.evaluator_tools import make_evaluator_tools

        wt = make_git_repo(tmp_path, "r3")
        read_diff, _ = make_evaluator_tools(await self._ctx(wt))
        for bad in ["--output=x", "../secret", "/etc/passwd"]:
            out = await read_diff(paths=[bad])
            assert out["status"] == "error", bad

    async def test_oversized_diff_is_truncated_with_stat(self, tmp_path: Path) -> None:
        from yukar.agents.tools import evaluator_tools
        from yukar.agents.tools.evaluator_tools import make_evaluator_tools

        wt = make_git_repo(tmp_path, "r4")
        self._stage(wt, {f"f{i}.py": ("x = 1\n" * 200) for i in range(5)})
        read_diff, _ = make_evaluator_tools(await self._ctx(wt))
        from unittest.mock import patch

        with patch.object(evaluator_tools, "_DIFF_CHAR_LIMIT", 2000):
            out = await read_diff()
        assert out["truncated"] is True
        assert out["total_chars"] > 2000
        assert "DIFF TOO LARGE" in out["diff"]
        assert len(out["files"]) == 5
        assert "`paths`" in out["diff"]


class TestAdversarialRegressions:
    """Regressions from the third (adversarial) review pass."""

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def _draft(
        self, tmp_path: Path, git_repo: Path, files: dict[str, str]
    ) -> tuple[Any, Any]:
        from yukar.agents.dispatch import run_dispatch

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _RaisingHarness()
        h.worker_writes = files
        ctx_d = await _make_ctx(root, project_id, epic_id, [_task("T1", "A", git_repo.name)], h)
        out = await run_dispatch(
            ctx_d, [{"task_id": "T1", "repo": git_repo.name, "agents": ["worker"]}]
        )
        assert out[0]["status"] == "done"
        ctx_d.tasks_holder[0].tasks[0].status = "todo"
        h.eval_verdict = {"accepted": True, "feedback": "ok", "submitted": True}
        return ctx_d, h

    async def test_worker_thread_resolved_after_evaluator_failure(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        from strands.types.exceptions import MaxTokensReachedException

        from yukar.agents.dispatch import run_dispatch
        from yukar.storage import threads_repo

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _RaisingHarness()
        h.worker_writes = {"a.py": "a\n"}
        h.eval_exc = MaxTokensReachedException("cut")
        ctx_d = await _make_ctx(root, project_id, epic_id, [_task("T1", "A", git_repo.name)], h)
        r = (await run_dispatch(ctx_d, [{"task_id": "T1", "repo": git_repo.name}]))[0]
        th = await threads_repo.get_threads(root, project_id, epic_id)
        statuses = {t.id: t.status for t in th.threads}
        assert statuses[r["worker_id"]] == "resolved"
        assert statuses[r["eval_id"]] == "failed"

    async def test_worker_failure_discards_half_written_files(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        from strands.types.exceptions import MaxTokensReachedException

        from yukar.agents.dispatch import run_dispatch

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _RaisingHarness()
        h.worker_writes_before_exc = {"partial.py": "def half(:\n"}
        h.worker_exc = MaxTokensReachedException("cut")
        ctx_d = await _make_ctx(root, project_id, epic_id, [_task("T1", "A", git_repo.name)], h)
        first = await run_dispatch(ctx_d, [{"task_id": "T1", "repo": git_repo.name}])
        assert first[0]["status"] == "failed"
        wt = _worktree(root, project_id, epic_id, git_repo.name)
        assert not (wt / "partial.py").exists(), "truncated output must be discarded"
        # Nothing to certify afterwards: an evaluator-only item finds no changes.
        h.worker_exc = None
        h.eval_verdict = {"accepted": True, "feedback": "ok", "submitted": True}
        out = await run_dispatch(
            ctx_d, [{"task_id": "T1", "repo": git_repo.name, "agents": ["evaluator"]}]
        )
        assert out[0]["status"] == "done" and _commit_count(wt) == 1

    async def test_worker_result_none_is_handled(self, git_repo: Path, tmp_path: Path) -> None:
        from yukar.agents.dispatch import run_dispatch

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _RaisingHarness()

        async def run_worker(**kwargs: Any) -> dict[str, Any]:
            return {"result": None}

        ctx_d = await _make_ctx(root, project_id, epic_id, [_task("T1", "A", git_repo.name)], h)
        ctx_d.hooks.run_worker = run_worker
        out = await run_dispatch(
            ctx_d, [{"task_id": "T1", "repo": git_repo.name, "agents": ["worker"]}]
        )
        assert out[0]["status"] == "needs_fix"
        assert ctx_d.tasks_holder[0].tasks[0].status == "todo"

    async def test_host_crash_inside_attempt_becomes_failed_item(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        from unittest.mock import patch

        from yukar.agents.dispatch import run_dispatch

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _RaisingHarness()
        ctx_d = await _make_ctx(root, project_id, epic_id, [_task("T1", "A", git_repo.name)], h)
        with patch("yukar.agents.dispatch.run_one_attempt", side_effect=ValueError("boom")):
            out = await run_dispatch(ctx_d, [{"task_id": "T1", "repo": git_repo.name}])
        assert out[0]["status"] == "failed"
        assert out[0]["reason"] == "host failed: other"
        assert ctx_d.tasks_holder[0].tasks[0].status == "todo"

    def test_implicit_exception_context_is_not_classified(self) -> None:
        """An unrelated AccessDenied merely in __context__ must not suspend the run."""
        from yukar.agents.failures import classify_agent_failure

        try:
            try:
                raise _client_error("AccessDeniedException")
            except Exception:
                raise RuntimeError("cleanup failed")  # noqa: B904 — implicit context on purpose
        except RuntimeError as exc:
            f = classify_agent_failure(exc)
        assert f.kind == "other"

    async def test_read_diff_rejects_oversized_path_lists(self, tmp_path: Path) -> None:
        from yukar.agents.context import AgentContext
        from yukar.agents.tools.evaluator_tools import make_evaluator_tools

        wt = make_git_repo(tmp_path, "r9")
        ctx = await AgentContext.create(
            project_id="p",
            epic_id="e",
            repo_name=wt.name,
            worktree_path=wt,
            workspace_root=str(wt.parent),
            allow=[],
            deny=[],
        )
        read_diff, _ = make_evaluator_tools(ctx)
        out = await read_diff(paths=[f"f{i}.py" for i in range(501)])
        assert out["status"] == "error" and "too many paths" in out["error"]
        out2 = await read_diff(paths=["a.py\n--stat"])
        assert out2["status"] == "error"


class TestAdversarialRegressionsRound2:
    """Regressions from the verification pass on the third-review fixes."""

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        return make_git_repo(tmp_path, "myrepo")

    async def _draft(
        self, tmp_path: Path, git_repo: Path, files: dict[str, str], tasks: list[Any] | None = None
    ) -> tuple[Any, Any]:
        from yukar.agents.dispatch import run_dispatch

        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, git_repo)
        h = _RaisingHarness()
        h.worker_writes = files
        ctx_d = await _make_ctx(
            root, project_id, epic_id, tasks or [_task("T1", "A", git_repo.name)], h
        )
        out = await run_dispatch(
            ctx_d, [{"task_id": "T1", "repo": git_repo.name, "agents": ["worker"]}]
        )
        assert out[0]["status"] == "done"
        ctx_d.tasks_holder[0].tasks[0].status = "todo"
        h.eval_verdict = {"accepted": True, "feedback": "ok", "submitted": True}
        return ctx_d, h

    def test_cause_behind_event_loop_exception_is_followed(self) -> None:
        from strands.types.exceptions import EventLoopException

        from yukar.agents.failures import classify_agent_failure

        try:
            raise RuntimeError("mid") from _client_error("ExpiredTokenException")
        except RuntimeError as mid:
            wrapped = EventLoopException(mid)
        assert classify_agent_failure(wrapped).kind == "provider_auth"

    async def test_tracked_file_modified_by_evaluator_is_restored(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        import subprocess

        from yukar.agents.dispatch import run_dispatch

        ctx_d, h = await self._draft(tmp_path, git_repo, {"a.py": "a\n"})
        wt = _worktree(ctx_d.root, ctx_d.project_id, ctx_d.epic_id, git_repo.name)
        orig = ctx_d.hooks.run_evaluator

        async def formatter(**kwargs: Any) -> dict[str, Any]:
            (wt / "README.md").write_text("# reformatted by the evaluator\n")
            (wt / ".coverage").write_text("artifact\n")
            return await orig(**kwargs)

        ctx_d.hooks.run_evaluator = formatter
        out = await run_dispatch(
            ctx_d, [{"task_id": "T1", "repo": git_repo.name, "agents": ["evaluator"]}]
        )
        assert out[0]["status"] == "done"
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True
        ).stdout
        assert "README.md" not in status and ".coverage" not in status
        shown = subprocess.run(
            ["git", "show", "--stat", "--format=%s", "HEAD"], cwd=wt, capture_output=True, text=True
        ).stdout
        assert "README.md" not in shown and ".coverage" not in shown
