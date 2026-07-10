"""Tests for issue③④⑤⑥ — dispatch lifecycle, grep tools, and worker exceptions.

issue④: make_git_tools include_commit=False, read_diff --cached, host stage+commit.
issue③: append_message (role="user") before stream_async for worker and evaluator.
issue⑤: repo_grep — path guard, result parsing, FileNotFoundError.
issue⑥: run_worker exception → WorkerFailedEvent, worker_finalized=True; dispatch skips
         double-update; MaxTokensReachedException → reason="max_tokens".
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent_context(tmp_path: Path) -> Any:
    """Return an AgentContext synchronously (no gitignore walk, suitable for tests)."""
    from yukar.agents.context import AgentContext

    return AgentContext(
        project_id="proj",
        epic_id="epic",
        repo_name="repo",
        worktree_path=tmp_path,
        workspace_root=str(tmp_path),
    )


def _make_bare_repo(parent: Path, name: str = "repo") -> Path:
    """Create a minimal real git repo with one commit and return its path."""
    repo = parent / name
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    (repo / "init.txt").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    return repo


def _make_dispatch_context(
    tmp_path: Path,
    *,
    run_worker_fn: Any = None,
    run_evaluator_fn: Any = None,
    pub_fn: Any = None,
    project_id: str = "proj",
    epic_id: str = "epic1",
) -> Any:
    """Build a minimal DispatchContext for unit tests."""
    from yukar.agents.dispatch import DispatchContext, OrchestratorHooks
    from yukar.models.epic import Epic
    from yukar.models.run import RunState
    from yukar.models.task import TasksFile
    from yukar.runs.scheduler import WorkerScheduler

    epic = Epic(
        id=epic_id,
        slug=epic_id.replace("_", "-"),
        title="Epic",
        description="",
        branch=epic_id,
    )
    state = RunState(run_id="run1", status="running")

    async def _default_worker(**kwargs: Any) -> dict[str, Any]:
        return {"result": "done"}

    async def _default_evaluator(**kwargs: Any) -> dict[str, Any]:
        return {"accepted": True, "feedback": ""}

    return DispatchContext(
        root=str(tmp_path),
        project_id=project_id,
        epic_id=epic_id,
        run_id="run1",
        epic=epic,
        state=state,
        tasks_holder=[TasksFile(tasks=[])],
        attempt_counts={},
        state_lock=asyncio.Lock(),
        scheduler=WorkerScheduler(max_parallel_workers=4),
        is_stopped=lambda: False,
        run_status="running",
        pub=pub_fn if pub_fn is not None else (lambda _: None),
        max_attempts=3,
        git_author_name="yukar",
        git_author_email="yukar@localhost",
        hooks=OrchestratorHooks(
            checkpoint=AsyncMock(),
            drain_pending=lambda: [],
            run_worker=run_worker_fn if run_worker_fn is not None else _default_worker,
            run_evaluator=(
                run_evaluator_fn if run_evaluator_fn is not None else _default_evaluator
            ),
        ),
    )


# ===========================================================================
# issue④: make_git_tools include_commit
# ===========================================================================


class TestMakeGitToolsIncludeCommit:
    """issue④ — include_commit=False removes git_commit from the returned list."""

    def test_default_includes_commit(self, tmp_path: Path) -> None:
        from yukar.agents.tools.git_tools import make_git_tools

        ctx = _make_agent_context(tmp_path)
        tools = make_git_tools(ctx)
        names = {t.__name__ for t in tools}
        assert "git_commit" in names, "git_commit should be present by default"
        assert len(tools) == 4

    def test_include_commit_false_excludes_commit(self, tmp_path: Path) -> None:
        from yukar.agents.tools.git_tools import make_git_tools

        ctx = _make_agent_context(tmp_path)
        tools = make_git_tools(ctx, include_commit=False)
        names = {t.__name__ for t in tools}
        assert "git_commit" not in names, (
            "git_commit must not be present when include_commit=False"
        )
        assert len(tools) == 3
        assert "git_status" in names
        assert "git_diff" in names
        assert "git_add" in names

    def test_include_commit_true_explicit(self, tmp_path: Path) -> None:
        from yukar.agents.tools.git_tools import make_git_tools

        ctx = _make_agent_context(tmp_path)
        tools = make_git_tools(ctx, include_commit=True)
        names = {t.__name__ for t in tools}
        assert "git_commit" in names
        assert len(tools) == 4


# ===========================================================================
# issue④: read_diff default uses --cached
# ===========================================================================


class TestReadDiffDefaultCached:
    """issue④ — read_diff without base_branch uses --cached (staged diff)."""

    @pytest.mark.asyncio
    async def test_default_uses_cached(self, tmp_path: Path) -> None:
        from yukar.agents.tools.evaluator_tools import make_evaluator_tools

        ctx = _make_agent_context(tmp_path)
        tools = make_evaluator_tools(ctx)
        read_diff = next(t for t in tools if t.__name__ == "read_diff")

        with patch("yukar.agents.tools.evaluator_tools.run_git") as mock_run_git:
            mock_result = MagicMock()
            mock_result.stdout = "diff content"
            mock_run_git.return_value = mock_result

            await read_diff()

        mock_run_git.assert_called_once()
        args = mock_run_git.call_args[0]
        assert "--cached" in args, f"--cached must be in git args, got: {args}"
        assert "HEAD" not in args, f"HEAD should not appear in staged-diff path, got: {args}"

    @pytest.mark.asyncio
    async def test_base_branch_uses_head_range(self, tmp_path: Path) -> None:
        from yukar.agents.tools.evaluator_tools import make_evaluator_tools

        ctx = _make_agent_context(tmp_path)
        tools = make_evaluator_tools(ctx)
        read_diff = next(t for t in tools if t.__name__ == "read_diff")

        with patch("yukar.agents.tools.evaluator_tools.run_git") as mock_run_git:
            mock_result = MagicMock()
            mock_result.stdout = "diff content"
            mock_run_git.return_value = mock_result

            await read_diff(base_branch="main")

        args = mock_run_git.call_args[0]
        assert "main...HEAD" in args or any("main" in str(a) for a in args)
        assert "--cached" not in args


# ===========================================================================
# issue④: publish_diff_update uses --cached (real git)
# ===========================================================================


class TestPublishDiffUpdateCached:
    """issue④ — publish_diff_update counts staged files via --cached diff."""

    @pytest.mark.asyncio
    async def test_uses_cached_counts_staged_files(self, tmp_path: Path) -> None:
        """publish_diff_update counts staged files (--cached diff)."""
        from yukar.agents.dispatch_helpers import publish_diff_update

        repo = _make_bare_repo(tmp_path, "testrepo")
        (repo / "new_file.py").write_text("# new\n")
        subprocess.run(
            ["git", "add", "new_file.py"],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )

        events: list[Any] = []
        files_changed = await publish_diff_update(
            "proj", "epic", "run1", "repo", repo, events.append
        )

        assert files_changed >= 1, f"Expected >= 1 file changed, got {files_changed}"


# ===========================================================================
# issue④: run_one_attempt host stage and commit
# ===========================================================================


class TestRunOneAttemptHostCommit:
    """issue④ — host stages with git add -A and commits after Evaluator accepts."""

    @pytest.mark.asyncio
    async def test_host_stages_and_commits_on_accept(self, tmp_path: Path) -> None:
        """When accepted=True, host runs git add -A then git commit."""
        from yukar.agents.dispatch_attempt import run_one_attempt
        from yukar.models.task import Task, TasksFile

        repo = _make_bare_repo(tmp_path, "myrepo")
        (tmp_path / "ws").mkdir()

        task = Task(id="T1", title="Add feature", status="todo", repo="myrepo")

        run_git_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        async def fake_run_git(*args: Any, **kwargs: Any) -> MagicMock:
            run_git_calls.append((args, kwargs))
            result = MagicMock()
            # `git diff --cached --quiet` → rc 1 signals staged changes exist
            # (so the host proceeds to commit them).
            result.returncode = 1 if args[:3] == ("diff", "--cached", "--quiet") else 0
            result.stdout = ""
            result.stderr = ""
            return result

        async def fake_run_worker(**kwargs: Any) -> dict[str, Any]:
            return {"result": "Implemented the feature."}

        async def fake_run_evaluator(**kwargs: Any) -> dict[str, Any]:
            return {"accepted": True, "feedback": ""}

        async def fake_publish_diff(*args: Any, **kwargs: Any) -> int:
            return 1

        ctx_d = _make_dispatch_context(
            tmp_path / "ws",
            run_worker_fn=fake_run_worker,
            run_evaluator_fn=fake_run_evaluator,
            project_id="proj",
            epic_id="epic1",
        )
        # Override tasks_holder with the actual task.
        ctx_d.tasks_holder[0] = TasksFile(tasks=[task])

        with (
            patch("yukar.agents.dispatch_attempt.run_git", side_effect=fake_run_git),
            patch("yukar.agents.dispatch_attempt.state_repo.save_state", AsyncMock()),
            patch("yukar.agents.dispatch_attempt.threads_repo") as mock_threads_repo,
            patch("yukar.agents.dispatch_attempt.publish_diff_update", fake_publish_diff),
            patch("yukar.agents.dispatch_attempt.register_agent_thread", AsyncMock()),
            patch(
                "yukar.agents.dispatch_attempt.get_repo",
                AsyncMock(return_value=MagicMock(commands=MagicMock(allow=[], deny=[]))),
            ),
            patch(
                "yukar.agents.dispatch_attempt.AgentContext.create",
                AsyncMock(return_value=_make_agent_context(repo)),
            ),
        ):
            mock_threads_repo.update_thread_status = AsyncMock()
            result = await run_one_attempt(
                ctx_d=ctx_d,
                task=task,
                repo_name="myrepo",
                worktree_path=repo,
                feedback="",
            )

        accepted, _, _, _, _, worker_finalized = result
        assert accepted is True
        assert worker_finalized is False

        # git add -A must have been called (host staging).
        add_calls = [c for c in run_git_calls if c[0] and c[0][0] == "add" and "-A" in c[0]]
        assert add_calls, f"Expected 'git add -A' call, got: {run_git_calls}"

        # git commit must have been called with subject "T1: Add feature".
        commit_calls = [c for c in run_git_calls if c[0] and c[0][0] == "commit"]
        assert commit_calls, f"Expected git commit call, got: {run_git_calls}"
        commit_args = commit_calls[-1][0]
        assert "T1: Add feature" in commit_args, (
            f"Commit subject should be 'T1: Add feature', got args: {commit_args}"
        )

    @pytest.mark.asyncio
    async def test_commit_failure_rejects_to_retry(self, tmp_path: Path) -> None:
        """If the host commit FAILS on accept, the attempt is rejected (accepted=False
        with feedback) instead of marked done — so the staged work is retried, not
        silently discarded by the next attempt's reset."""
        from yukar.agents.dispatch_attempt import run_one_attempt
        from yukar.models.task import Task, TasksFile

        repo = _make_bare_repo(tmp_path, "myrepo_cf")
        (tmp_path / "wscf").mkdir()

        task = Task(id="T1", title="Add feature", status="todo", repo="myrepo_cf")

        async def fake_run_git(*args: Any, **kwargs: Any) -> MagicMock:
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            if args[:3] == ("diff", "--cached", "--quiet"):
                result.returncode = 1  # staged changes exist → proceed to commit
            elif args and args[0] == "commit":
                result.returncode = 1  # commit FAILS
                result.stderr = "fatal: unable to write commit object"
            else:
                result.returncode = 0
            return result

        async def fake_run_worker(**kwargs: Any) -> dict[str, Any]:
            return {"result": "done"}

        async def fake_run_evaluator(**kwargs: Any) -> dict[str, Any]:
            return {"accepted": True, "feedback": ""}

        async def fake_publish_diff(*args: Any, **kwargs: Any) -> int:
            return 1

        ctx_d = _make_dispatch_context(
            tmp_path / "wscf",
            run_worker_fn=fake_run_worker,
            run_evaluator_fn=fake_run_evaluator,
            project_id="proj",
            epic_id="epic1",
        )
        ctx_d.tasks_holder[0] = TasksFile(tasks=[task])

        with (
            patch("yukar.agents.dispatch_attempt.run_git", side_effect=fake_run_git),
            patch("yukar.agents.dispatch_attempt.state_repo.save_state", AsyncMock()),
            patch("yukar.agents.dispatch_attempt.threads_repo") as mock_threads_repo,
            patch("yukar.agents.dispatch_attempt.publish_diff_update", fake_publish_diff),
            patch("yukar.agents.dispatch_attempt.register_agent_thread", AsyncMock()),
            patch(
                "yukar.agents.dispatch_attempt.get_repo",
                AsyncMock(return_value=MagicMock(commands=MagicMock(allow=[], deny=[]))),
            ),
            patch(
                "yukar.agents.dispatch_attempt.AgentContext.create",
                AsyncMock(return_value=_make_agent_context(repo)),
            ),
        ):
            mock_threads_repo.update_thread_status = AsyncMock()
            accepted, _, _, feedback_out, _, worker_finalized = await run_one_attempt(
                ctx_d=ctx_d, task=task, repo_name="myrepo_cf", worktree_path=repo, feedback=""
            )

        assert accepted is False, "commit failure must reject the attempt (not mark done)"
        assert worker_finalized is False
        assert "commit failed" in feedback_out.lower()

    @pytest.mark.asyncio
    async def test_resets_worktree_before_worker(self, tmp_path: Path) -> None:
        """Cross-task isolation: each attempt resets the shared (epic, repo) worktree
        to HEAD (git reset --hard HEAD + git clean -fd) BEFORE the Worker runs, so a
        prior rejected/abandoned task's uncommitted residue cannot leak into this
        attempt's diff or host commit."""
        from yukar.agents.dispatch_attempt import run_one_attempt
        from yukar.models.task import Task, TasksFile

        repo = _make_bare_repo(tmp_path, "myrepo3")
        (tmp_path / "ws3").mkdir()

        task = Task(id="T1", title="Add feature", status="todo", repo="myrepo3")

        seq: list[tuple[str, Any, Any]] = []

        async def fake_run_git(*args: Any, **kwargs: Any) -> MagicMock:
            seq.append(("git", args[0] if args else None, args))
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        async def fake_run_worker(**kwargs: Any) -> dict[str, Any]:
            seq.append(("worker", None, None))
            return {"result": "done"}

        async def fake_run_evaluator(**kwargs: Any) -> dict[str, Any]:
            return {"accepted": True, "feedback": ""}

        async def fake_publish_diff(*args: Any, **kwargs: Any) -> int:
            return 1

        ctx_d = _make_dispatch_context(
            tmp_path / "ws3",
            run_worker_fn=fake_run_worker,
            run_evaluator_fn=fake_run_evaluator,
            project_id="proj",
            epic_id="epic1",
        )
        ctx_d.tasks_holder[0] = TasksFile(tasks=[task])

        with (
            patch("yukar.agents.dispatch_attempt.run_git", side_effect=fake_run_git),
            patch("yukar.agents.dispatch_attempt.state_repo.save_state", AsyncMock()),
            patch("yukar.agents.dispatch_attempt.threads_repo") as mock_threads_repo,
            patch("yukar.agents.dispatch_attempt.publish_diff_update", fake_publish_diff),
            patch("yukar.agents.dispatch_attempt.register_agent_thread", AsyncMock()),
            patch(
                "yukar.agents.dispatch_attempt.get_repo",
                AsyncMock(return_value=MagicMock(commands=MagicMock(allow=[], deny=[]))),
            ),
            patch(
                "yukar.agents.dispatch_attempt.AgentContext.create",
                AsyncMock(return_value=_make_agent_context(repo)),
            ),
        ):
            mock_threads_repo.update_thread_status = AsyncMock()
            await run_one_attempt(
                ctx_d=ctx_d, task=task, repo_name="myrepo3", worktree_path=repo, feedback=""
            )

        reset_idx = next((i for i, s in enumerate(seq) if s[0] == "git" and s[1] == "reset"), None)
        clean_idx = next((i for i, s in enumerate(seq) if s[0] == "git" and s[1] == "clean"), None)
        worker_idx = next((i for i, s in enumerate(seq) if s[0] == "worker"), None)
        assert reset_idx is not None, f"git reset must be called, got: {seq}"
        assert clean_idx is not None, f"git clean must be called, got: {seq}"
        assert worker_idx is not None, f"worker must run, got: {seq}"
        # Reset/clean must precede the Worker (isolation guarantee).
        assert reset_idx < worker_idx, f"reset must precede Worker, got: {seq}"
        assert clean_idx < worker_idx, f"clean must precede Worker, got: {seq}"
        # reset --hard HEAD ; clean -fd
        assert "--hard" in seq[reset_idx][2] and "HEAD" in seq[reset_idx][2]
        assert "-fd" in seq[clean_idx][2]

    @pytest.mark.asyncio
    async def test_no_commit_on_reject(self, tmp_path: Path) -> None:
        """When accepted=False, no commit is made."""
        from yukar.agents.dispatch_attempt import run_one_attempt
        from yukar.models.task import Task, TasksFile

        repo = _make_bare_repo(tmp_path, "myrepo2")
        (tmp_path / "ws2").mkdir()

        task = Task(id="T1", title="Feature", status="todo", repo="myrepo2")

        run_git_calls: list[tuple[Any, ...]] = []

        async def fake_run_git(*args: Any, **kwargs: Any) -> MagicMock:
            run_git_calls.append(args)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        async def fake_run_evaluator(**kwargs: Any) -> dict[str, Any]:
            return {"accepted": False, "feedback": "Not done"}

        ctx_d = _make_dispatch_context(
            tmp_path / "ws2",
            run_evaluator_fn=fake_run_evaluator,
            project_id="proj",
            epic_id="epic1",
        )
        ctx_d.tasks_holder[0] = TasksFile(tasks=[task])

        with (
            patch("yukar.agents.dispatch_attempt.run_git", side_effect=fake_run_git),
            patch("yukar.agents.dispatch_attempt.state_repo.save_state", AsyncMock()),
            patch("yukar.agents.dispatch_attempt.threads_repo") as mock_threads_repo,
            patch("yukar.agents.dispatch_attempt.publish_diff_update", AsyncMock(return_value=0)),
            patch("yukar.agents.dispatch_attempt.register_agent_thread", AsyncMock()),
            patch(
                "yukar.agents.dispatch_attempt.get_repo",
                AsyncMock(return_value=MagicMock(commands=MagicMock(allow=[], deny=[]))),
            ),
            patch(
                "yukar.agents.dispatch_attempt.AgentContext.create",
                AsyncMock(return_value=_make_agent_context(repo)),
            ),
        ):
            mock_threads_repo.update_thread_status = AsyncMock()
            result = await run_one_attempt(
                ctx_d=ctx_d,
                task=task,
                repo_name="myrepo2",
                worktree_path=repo,
                feedback="",
            )

        accepted, _, _, _, _, worker_finalized = result
        assert accepted is False
        assert worker_finalized is False

        commit_calls = [c for c in run_git_calls if c and c[0] == "commit"]
        assert not commit_calls, f"Expected no commit on reject, got: {run_git_calls}"


# ===========================================================================
# issue③: append_message before stream_async
# ===========================================================================


_CONVO: list[Any] = [
    {"role": "user", "content": [{"text": "# Task Contract\nCreate a.py"}]},
    {
        "role": "assistant",
        "content": [
            {"text": "writing the file"},
            {"toolUse": {"toolUseId": "t1", "name": "fs_write", "input": {"path": "a.py"}}},
        ],
    },
    {
        "role": "user",
        "content": [
            {"toolResult": {"toolUseId": "t1", "status": "success", "content": [{"text": "ok"}]}}
        ],
    },
    {"role": "assistant", "content": [{"text": "Done."}]},
]


class _FakeStream:
    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> None:
        raise StopAsyncIteration


class TestActivityLogPersistence:
    """issue③ + tool-log — Worker/Evaluator persist their FULL conversation
    (agent.messages: hand-off + tool-use activity + final reply) AFTER stream_async,
    so the thread retains its activity log on reload without a FileSessionManager."""

    @pytest.mark.asyncio
    async def test_worker_persists_full_conversation_after_stream(self, tmp_path: Path) -> None:
        from yukar.agents.worker import run_worker
        from yukar.models.task import Task

        ctx = _make_agent_context(tmp_path)
        task = Task(id="T1", title="Task", status="todo")

        order: list[str] = []
        captured: dict[str, Any] = {}

        async def fake_persist(
            root: str, pid: str, eid: str, tid: str, messages: Any, **kw: Any
        ) -> None:
            order.append("persist")
            captured["messages"] = messages

        class FakeAgent:
            messages = _CONVO

            def stream_async(self, prompt: str, *, limits: Any = None) -> _FakeStream:
                order.append("stream")
                return _FakeStream()

        fake_bound = MagicMock()
        fake_bound.flush = AsyncMock()

        with (
            patch(
                "yukar.agents.worker.session_store.persist_agent_messages",
                side_effect=fake_persist,
            ),
            patch("yukar.agents.worker.Agent", return_value=FakeAgent()),
            patch("yukar.agents.worker.AgentUsageRecorder") as mock_recorder,
            patch("yukar.agents.worker.make_fs_tools", return_value=[]),
            patch("yukar.agents.worker.make_fs_edit_tools", return_value=[]),
            patch("yukar.agents.worker.make_command_tools", return_value=[]),
            patch("yukar.agents.worker.make_git_tools", return_value=[]),
            patch("yukar.agents.worker.make_grep_tools", return_value=[]),
        ):
            mock_recorder.return_value.bind.return_value = fake_bound
            await run_worker(
                project_id="proj",
                epic_id="epic",
                run_id="run1",
                worker_id="worker-abc",
                task=task,
                ctx=ctx,
                feedback="",
                hitl_prefix="",
                worker_model=MagicMock(),
                conversation_manager=None,
                indexer_service=None,
                git_author_name="yukar",
                git_author_email="yukar@localhost",
            )

        assert "persist" in order and "stream" in order
        assert order.index("persist") > order.index("stream"), "persist must run after stream"
        assert captured["messages"] is _CONVO, "worker.agent.messages must be persisted verbatim"

    @pytest.mark.asyncio
    async def test_evaluator_persists_full_conversation_after_stream(self, tmp_path: Path) -> None:
        from yukar.agents.evaluator import run_evaluator
        from yukar.models.task import Task

        ctx = _make_agent_context(tmp_path)
        task = Task(id="T1", title="Task", status="todo")

        order: list[str] = []
        captured: dict[str, Any] = {}

        async def fake_persist(
            root: str, pid: str, eid: str, tid: str, messages: Any, **kw: Any
        ) -> None:
            order.append("persist")
            captured["messages"] = messages

        class FakeAgent:
            messages = _CONVO

            def stream_async(self, prompt: str, *, limits: Any = None) -> _FakeStream:
                order.append("stream")
                return _FakeStream()

        fake_bound = MagicMock()
        fake_bound.flush = AsyncMock()

        with (
            patch(
                "yukar.agents.evaluator.session_store.persist_agent_messages",
                side_effect=fake_persist,
            ),
            patch("yukar.agents.evaluator.Agent", return_value=FakeAgent()),
            patch("yukar.agents.evaluator.AgentUsageRecorder") as mock_recorder,
            patch("yukar.agents.evaluator.make_evaluator_tools", return_value=[]),
            patch("yukar.agents.evaluator.make_grep_tools", return_value=[]),
        ):
            mock_recorder.return_value.bind.return_value = fake_bound
            await run_evaluator(
                project_id="proj",
                epic_id="epic",
                run_id="run1",
                eval_id="eval-abc",
                task=task,
                ctx=ctx,
                worker_id="worker-abc",
                eval_model=MagicMock(),
                conversation_manager=None,
            )

        assert "persist" in order and "stream" in order
        assert order.index("persist") > order.index("stream"), "persist must run after stream"
        assert captured["messages"] is _CONVO, "evaluator.agent.messages must be persisted verbatim"


class TestPersistAgentMessages:
    """session_store.persist_agent_messages — writes Strands content blocks
    (text / toolUse / toolResult) round-trippable by list_messages, with truncation."""

    @pytest.mark.asyncio
    async def test_round_trip_blocks_and_truncation(self, tmp_path: Path) -> None:
        from yukar.storage import session_store

        root = str(tmp_path)
        messages = [
            {"role": "user", "content": [{"text": "hello " + "x" * 20_000}]},
            {
                "role": "assistant",
                "content": [
                    {"text": "writing"},
                    {
                        "toolUse": {
                            "toolUseId": "t1",
                            "name": "fs_write",
                            "input": {"path": "a.py", "content": "y" * 20_000},
                        }
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "t1",
                            "status": "success",
                            "content": [{"text": "z" * 20_000}],
                        }
                    }
                ],
            },
            {"role": "assistant", "content": [{"text": "done"}]},
        ]

        await session_store.persist_agent_messages(
            root, "p", "e", "worker-1", messages, max_block_chars=100
        )
        out = session_store.list_messages(root, "p", "e", "worker-1")

        assert len(out) == 4
        assert out[0].message.role == "user"
        assert "[truncated" in (out[0].message.content[0].text or "")
        # toolUse preserved, long input string truncated
        tu = out[1].message.content[1].tool_use
        assert tu is not None and tu.name == "fs_write"
        assert "[truncated" in str(tu.input.get("content"))
        # toolResult preserved + flattened text truncated
        tr = out[2].message.content[0].tool_result
        assert tr is not None and tr.tool_use_id == "t1"
        assert "[truncated" in (tr.text or "")
        assert out[3].message.content[0].text == "done"

    @pytest.mark.asyncio
    async def test_empty_messages_is_noop(self, tmp_path: Path) -> None:
        from yukar.storage import session_store

        root = str(tmp_path)
        await session_store.persist_agent_messages(root, "p", "e", "worker-2", [])
        assert session_store.list_messages(root, "p", "e", "worker-2") == []


# ===========================================================================
# issue⑤: repo_grep
# ===========================================================================


class TestRepoGrep:
    """issue⑤ — repo_grep path guard, result parsing, FileNotFoundError."""

    @pytest.mark.asyncio
    async def test_path_escape_returns_error_without_subprocess(self, tmp_path: Path) -> None:
        """Paths escaping the worktree must return error without launching rg."""
        from yukar.agents.tools.grep_tools import make_grep_tools

        ctx = _make_agent_context(tmp_path)
        tools = make_grep_tools(ctx)
        repo_grep = tools[0]

        with patch(
            "yukar.agents.tools.grep_tools.asyncio.create_subprocess_exec"
        ) as mock_exec:
            result = await repo_grep(pattern="hello", path="../outside")

        assert result["status"] == "error"
        assert result["results"] == []
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_match_parsing(self, tmp_path: Path) -> None:
        """rg output 'path<US>lineno<US>text' is parsed into structured results
        AND the matched line is rendered into the LLM-visible content text."""
        from yukar.agents.tools.grep_tools import make_grep_tools

        ctx = _make_agent_context(tmp_path)
        tools = make_grep_tools(ctx)
        repo_grep = tools[0]

        rg_stdout = b"a/b.py\x1f12\x1f    def hello():\n"
        rg_stderr = b""

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(rg_stdout, rg_stderr))

        with patch(
            "yukar.agents.tools.grep_tools.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await repo_grep(pattern="hello")

        assert result["status"] == "success"
        assert len(result["results"]) == 1
        assert result["results"][0]["path"] == "a/b.py"
        assert result["results"][0]["line"] == 12
        assert result["results"][0]["text"] == "    def hello():"
        assert result["truncated"] is False
        # The content text (the ONLY part the LLM sees) must include the match
        # itself, not just the count.
        content_text = result["content"][0]["text"]
        assert "a/b.py:12:    def hello():" in content_text

    @pytest.mark.asyncio
    async def test_no_match_rc1_is_success(self, tmp_path: Path) -> None:
        """rg exit code 1 (no match) returns success with empty results."""
        from yukar.agents.tools.grep_tools import make_grep_tools

        ctx = _make_agent_context(tmp_path)
        tools = make_grep_tools(ctx)
        repo_grep = tools[0]

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch(
            "yukar.agents.tools.grep_tools.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await repo_grep(pattern="no_match_here")

        assert result["status"] == "success"
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_rg_not_installed_returns_error(self, tmp_path: Path) -> None:
        """FileNotFoundError from rg yields error with install message."""
        from yukar.agents.tools.grep_tools import make_grep_tools

        ctx = _make_agent_context(tmp_path)
        tools = make_grep_tools(ctx)
        repo_grep = tools[0]

        with patch(
            "yukar.agents.tools.grep_tools.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("rg not found"),
        ):
            result = await repo_grep(pattern="hello")

        assert result["status"] == "error"
        assert "ripgrep" in result["content"][0]["text"].lower()
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_max_results_truncation(self, tmp_path: Path) -> None:
        """Results exceeding max_results are truncated with truncated=True."""
        from yukar.agents.tools.grep_tools import make_grep_tools

        ctx = _make_agent_context(tmp_path)
        tools = make_grep_tools(ctx)
        repo_grep = tools[0]

        lines = [f"file.py\x1f{i}\x1fline {i}".encode() for i in range(1, 11)]
        rg_stdout = b"\n".join(lines) + b"\n"

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(rg_stdout, b""))

        with patch(
            "yukar.agents.tools.grep_tools.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await repo_grep(pattern="line", max_results=5)

        assert result["status"] == "success"
        assert len(result["results"]) == 5
        assert result["truncated"] is True
        assert "truncated" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_context_lines_rendered_and_flag_passed(self, tmp_path: Path) -> None:
        """context>0 adds -C to argv; context lines (rg's 'path-lineno-text'
        form) are shown in content but NOT parsed as structured matches, even
        when their text happens to look like 'str:int:rest'."""
        from yukar.agents.tools.grep_tools import make_grep_tools

        ctx = _make_agent_context(tmp_path)
        tools = make_grep_tools(ctx)
        repo_grep = tools[0]

        rg_stdout = (
            b"a/b.py-11-    # started at 12:34:56\n"
            b"a/b.py\x1f12\x1f    def hello():\n"
            b"a/b.py-13-        pass\n"
        )

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(rg_stdout, b""))

        with patch(
            "yukar.agents.tools.grep_tools.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ) as mock_exec:
            result = await repo_grep(pattern="hello", context=2)

        argv = mock_exec.call_args.args
        assert "-C" in argv
        assert argv[argv.index("-C") + 1] == "2"

        assert result["status"] == "success"
        # Only the real match is structured — the timestamp-looking context
        # line must not be miscounted as a match.
        assert len(result["results"]) == 1
        assert result["results"][0]["line"] == 12
        content_text = result["content"][0]["text"]
        assert "a/b.py-11-    # started at 12:34:56" in content_text
        assert "a/b.py:12:    def hello():" in content_text
        assert "a/b.py-13-        pass" in content_text

    @pytest.mark.asyncio
    async def test_context_zero_omits_flag(self, tmp_path: Path) -> None:
        """Default context=0 must not pass -C to rg."""
        from yukar.agents.tools.grep_tools import make_grep_tools

        ctx = _make_agent_context(tmp_path)
        tools = make_grep_tools(ctx)
        repo_grep = tools[0]

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch(
            "yukar.agents.tools.grep_tools.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ) as mock_exec:
            result = await repo_grep(pattern="hello")

        assert result["status"] == "success"
        assert "-C" not in mock_exec.call_args.args

    @pytest.mark.asyncio
    async def test_rg_error_rc2_returns_error(self, tmp_path: Path) -> None:
        """rg exit code >= 2 returns error with stderr."""
        from yukar.agents.tools.grep_tools import make_grep_tools

        ctx = _make_agent_context(tmp_path)
        tools = make_grep_tools(ctx)
        repo_grep = tools[0]

        mock_proc = MagicMock()
        mock_proc.returncode = 2
        mock_proc.communicate = AsyncMock(return_value=(b"", b"rg: bad pattern\n"))

        with patch(
            "yukar.agents.tools.grep_tools.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await repo_grep(pattern="[bad")

        assert result["status"] == "error"
        assert "rg error" in result["content"][0]["text"]


# ===========================================================================
# issue⑥: run_worker exception → worker_finalized=True
# ===========================================================================


class TestWorkerExceptionHandling:
    """issue⑥ — run_worker exception path in run_one_attempt."""

    @pytest.mark.asyncio
    async def test_worker_exception_returns_finalized_true(self, tmp_path: Path) -> None:
        """When run_worker raises, run_one_attempt returns worker_finalized=True."""
        from yukar.agents.dispatch_attempt import run_one_attempt
        from yukar.models.events import WorkerFailedEvent
        from yukar.models.task import Task, TasksFile

        repo = _make_bare_repo(tmp_path, "repo_exc")
        (tmp_path / "ws_exc").mkdir()

        task = Task(id="T1", title="Feature", status="todo", repo="repo_exc")

        published: list[Any] = []

        async def raising_run_worker(**kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("Agent crashed")

        async def fake_run_git(*args: Any, **kwargs: Any) -> MagicMock:
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        ctx_d = _make_dispatch_context(
            tmp_path / "ws_exc",
            run_worker_fn=raising_run_worker,
            pub_fn=published.append,
            project_id="proj",
            epic_id="epic1",
        )
        ctx_d.tasks_holder[0] = TasksFile(tasks=[task])

        with (
            patch("yukar.agents.dispatch_attempt.run_git", side_effect=fake_run_git),
            patch("yukar.agents.dispatch_attempt.state_repo.save_state", AsyncMock()),
            patch("yukar.agents.dispatch_attempt.threads_repo") as mock_threads_repo,
            patch("yukar.agents.dispatch_attempt.publish_diff_update", AsyncMock(return_value=0)),
            patch("yukar.agents.dispatch_attempt.register_agent_thread", AsyncMock()),
            patch(
                "yukar.agents.dispatch_attempt.get_repo",
                AsyncMock(return_value=MagicMock(commands=MagicMock(allow=[], deny=[]))),
            ),
            patch(
                "yukar.agents.dispatch_attempt.AgentContext.create",
                AsyncMock(return_value=_make_agent_context(repo)),
            ),
        ):
            mock_threads_repo.update_thread_status = AsyncMock()
            result = await run_one_attempt(
                ctx_d=ctx_d,
                task=task,
                repo_name="repo_exc",
                worktree_path=repo,
                feedback="",
            )

        accepted, _, eval_id, feedback_out, files_changed, worker_finalized = result

        assert accepted is False
        assert worker_finalized is True
        assert eval_id is None
        assert files_changed == 0
        assert "worker failed" in feedback_out

        failed_events = [e for e in published if isinstance(e, WorkerFailedEvent)]
        assert failed_events, (
            f"WorkerFailedEvent not published; got: {[type(e) for e in published]}"
        )
        assert failed_events[0].reason == "RuntimeError"

        mock_threads_repo.update_thread_status.assert_awaited_once()
        call_args = mock_threads_repo.update_thread_status.call_args
        assert call_args[0][-1] == "failed"

    @pytest.mark.asyncio
    async def test_max_tokens_exception_reason(self, tmp_path: Path) -> None:
        """MaxTokensReachedException is mapped to reason='max_tokens'."""
        from yukar.agents.dispatch_attempt import run_one_attempt
        from yukar.models.events import WorkerFailedEvent
        from yukar.models.task import Task, TasksFile

        class MaxTokensReachedException(Exception):
            pass

        repo = _make_bare_repo(tmp_path, "repo_maxtok")
        (tmp_path / "ws_maxtok").mkdir()

        task = Task(id="T2", title="Task", status="todo", repo="repo_maxtok")

        published: list[Any] = []

        async def raising_run_worker(**kwargs: Any) -> dict[str, Any]:
            raise MaxTokensReachedException("tokens exceeded")

        async def fake_run_git(*args: Any, **kwargs: Any) -> MagicMock:
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        ctx_d = _make_dispatch_context(
            tmp_path / "ws_maxtok",
            run_worker_fn=raising_run_worker,
            pub_fn=published.append,
            project_id="proj",
            epic_id="epic2",
        )
        ctx_d.tasks_holder[0] = TasksFile(tasks=[task])

        with (
            patch("yukar.agents.dispatch_attempt.run_git", side_effect=fake_run_git),
            patch("yukar.agents.dispatch_attempt.state_repo.save_state", AsyncMock()),
            patch("yukar.agents.dispatch_attempt.threads_repo") as mock_threads_repo,
            patch("yukar.agents.dispatch_attempt.publish_diff_update", AsyncMock(return_value=0)),
            patch("yukar.agents.dispatch_attempt.register_agent_thread", AsyncMock()),
            patch(
                "yukar.agents.dispatch_attempt.get_repo",
                AsyncMock(return_value=MagicMock(commands=MagicMock(allow=[], deny=[]))),
            ),
            patch(
                "yukar.agents.dispatch_attempt.AgentContext.create",
                AsyncMock(return_value=_make_agent_context(repo)),
            ),
        ):
            mock_threads_repo.update_thread_status = AsyncMock()
            result = await run_one_attempt(
                ctx_d=ctx_d,
                task=task,
                repo_name="repo_maxtok",
                worktree_path=repo,
                feedback="",
            )

        _, _, _, feedback_out, _, _ = result
        failed_events = [e for e in published if isinstance(e, WorkerFailedEvent)]
        assert failed_events, "WorkerFailedEvent not published"
        assert failed_events[0].reason == "max_tokens", (
            f"Expected reason='max_tokens', got {failed_events[0].reason!r}"
        )
        assert "max_tokens" in feedback_out


# ===========================================================================
# issue⑥: dispatch _handle_dispatch_item skips double-update when worker_finalized
# ===========================================================================


class TestDispatchWorkerFinalizedSkipsDoubleUpdate:
    """issue⑥ — worker_finalized=True → only eval thread (None) updated."""

    @pytest.mark.asyncio
    async def test_worker_finalized_skips_worker_thread_update(self, tmp_path: Path) -> None:
        """When worker_finalized=True, worker_id must be None in _update_thread_statuses."""
        from yukar.agents.dispatch import _handle_dispatch_item
        from yukar.models.task import Task, TasksFile

        task = Task(id="T1", title="Task", status="todo", repo="repo")
        tasks_file = TasksFile(tasks=[task])

        update_calls: list[tuple[str | None, str | None, str]] = []

        async def mock_update(
            ctx_d: Any,
            wid: str | None,
            eid: str | None,
            status: str,
        ) -> None:
            update_calls.append((wid, eid, status))

        async def fake_run_one(**kwargs: Any) -> tuple[
            bool, str | None, str | None, str, int, bool
        ]:
            return (False, "worker-abc", None, "worker failed: RuntimeError", 0, True)

        repo_wt = _make_bare_repo(tmp_path, "repo_wf")

        ctx_d = _make_dispatch_context(tmp_path, project_id="proj", epic_id="epic1")
        ctx_d.tasks_holder[0] = tasks_file

        completed_ids: set[str] = set()
        results: list[dict[str, Any]] = [{}]

        with (
            patch("yukar.agents.dispatch._update_thread_statuses", side_effect=mock_update),
            patch("yukar.agents.dispatch.run_one_attempt", side_effect=fake_run_one),
            patch(
                "yukar.agents.dispatch.ensure_worktree_for_repo",
                AsyncMock(return_value=repo_wt),
            ),
            patch(
                "yukar.agents.dispatch.get_first_repo",
                AsyncMock(return_value=MagicMock(name="repo")),
            ),
            patch("yukar.agents.dispatch.tasks_repo") as mock_tasks_repo,
        ):
            mock_tasks_repo.save_tasks = AsyncMock()

            await _handle_dispatch_item(
                0,
                {"task_id": "T1", "feedback": ""},
                ctx_d,
                tasks_file,
                completed_ids,
                results,
            )

        assert update_calls, "_update_thread_statuses was not called"
        wid, eid, status = update_calls[0]
        assert wid is None, (
            f"When worker_finalized=True, worker_id must be None (not {wid!r})"
        )
        assert status == "failed"
