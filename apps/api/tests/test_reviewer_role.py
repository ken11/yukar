"""Reviewer role (Phase 2 of the trial/session decoupling).

The Reviewer is a read-only, conversational agent the user can spawn at any
time to independently check the Manager's work against the epic's intent and
report back to the USER (it never instructs the Manager directly).  It reuses the
orchestrator's conversation loop in a read-only "reviewer mode".

Phase 2a (this batch): role plumbing — reviewer is a first-class AgentRole /
ThreadRole / ConfigurableAgentRole / UserCreatableThreadRole, with an optional
per-role model override.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, get_args

import pytest
from httpx import AsyncClient

if TYPE_CHECKING:
    from yukar.usage.tracker import TokenUsageTracker


class TestReviewerRoleLiterals:
    def test_reviewer_in_all_role_sets(self) -> None:
        from yukar.models.roles import (
            AgentRole,
            ConfigurableAgentRole,
            ThreadRole,
            UserCreatableThreadRole,
        )

        assert "reviewer" in get_args(AgentRole)
        assert "reviewer" in get_args(ThreadRole)
        assert "reviewer" in get_args(ConfigurableAgentRole)
        assert "reviewer" in get_args(UserCreatableThreadRole)

    def test_llm_roles_settings_has_reviewer(self) -> None:
        from yukar.config.settings import LLMRolesSettings

        s = LLMRolesSettings()
        assert hasattr(s, "reviewer")
        assert s.reviewer.model_id is None

    def test_factory_uses_reviewer_override(self) -> None:
        """create_model(role='reviewer') picks the reviewer model override."""
        from yukar.config.settings import LLMRoleSettings, LLMRolesSettings, LLMSettings
        from yukar.llm.factory import create_model

        settings = LLMSettings(
            provider="fake",
            model_id="global-model",
            roles=LLMRolesSettings(reviewer=LLMRoleSettings(model_id="reviewer-model")),
        )
        model = create_model(settings, role="reviewer")
        # FakeModel exposes the resolved model id via get_config().
        assert model.get_config().get("model_id") == "reviewer-model"


class TestReviewerPrompt:
    def test_build_reviewer_prompt_includes_intent_and_conversation(self) -> None:
        from yukar.agents.prompts import _build_reviewer_prompt
        from yukar.models.epic import Epic

        epic = Epic(
            id="EP-1",
            slug="s",
            title="Add auth",
            description="Implement login.",
            acceptance_criteria="login works",
            branch="yukar/ep-1-s",
        )
        prompt = _build_reviewer_prompt(
            epic,
            project_docs="",
            epic_docs="",
            manager_conversation=(
                "**User:** use OAuth, not passwords.\n\n**Manager:** Done in auth.py."
            ),
            hitl_prefix="",
        )
        assert "Add auth" in prompt
        assert "login works" in prompt
        # The agreed decision (OAuth) and the final report must both reach the reviewer.
        assert "use OAuth, not passwords." in prompt
        assert "Done in auth.py." in prompt
        assert "read_branch_diff" in prompt

    def test_format_manager_conversation_keeps_agreement_drops_tool_noise(self) -> None:
        from yukar.agents.prompts import format_manager_conversation
        from yukar.models.message import (
            ContentPart,
            Message,
            MessagePayload,
            ToolResultBlock,
            ToolUseBlock,
        )

        messages = [
            # Manager narration + a LEGACY ask_user question (tool_use) —
            # old sessions recorded questions this way; the reader keeps
            # extracting them (format_manager_conversation legacy compat).
            Message(
                message=MessagePayload(
                    role="assistant",
                    content=[
                        ContentPart(text="Here is my plan: add auth.py."),
                        ContentPart(
                            toolUse=ToolUseBlock(
                                toolUseId="t1",
                                name="ask_user",
                                input={"question": "OAuth or password login?"},
                            )
                        ),
                    ],
                ),
                message_id=0,
            ),
            # User reply (the agreement).
            Message(
                message=MessagePayload(
                    role="user",
                    content=[ContentPart(text="OAuth please.")],
                ),
                message_id=1,
            ),
            # Tool noise: a dispatch tool_use + a worker tool_result — must be dropped.
            Message(
                message=MessagePayload(
                    role="assistant",
                    content=[
                        ContentPart(
                            toolUse=ToolUseBlock(toolUseId="t2", name="dispatch", input={})
                        )
                    ],
                ),
                message_id=2,
            ),
            Message(
                message=MessagePayload(
                    role="user",
                    content=[
                        ContentPart(
                            toolResult=ToolResultBlock(toolUseId="t2", text="worker output blob")
                        )
                    ],
                ),
                message_id=3,
            ),
        ]
        out = format_manager_conversation(messages)
        assert "Here is my plan: add auth.py." in out
        assert "OAuth or password login?" in out  # legacy ask_user question kept
        assert "OAuth please." in out  # user agreement kept
        assert "worker output blob" not in out  # tool_result noise dropped
        assert "dispatch" not in out  # dispatch tool_use dropped


class TestReviewerThreadCreatable:
    @pytest.mark.asyncio
    async def test_post_threads_reviewer_accepted(self, app_client: AsyncClient) -> None:
        r = await app_client.post(
            "/api/projects",
            json={"id": "rev-proj", "name": "rev-proj", "repos": []},
        )
        assert r.status_code == 201, r.text
        r2 = await app_client.post(
            "/api/projects/rev-proj/epics",
            json={"title": "E", "description": ""},
        )
        assert r2.status_code == 201, r2.text
        epic_id = r2.json()["id"]

        r3 = await app_client.post(
            f"/api/projects/rev-proj/epics/{epic_id}/threads",
            json={"title": "Review", "role": "reviewer"},
        )
        assert r3.status_code == 201, r3.text
        assert r3.json()["role"] == "reviewer"


# ---------------------------------------------------------------------------
# Phase 2e: reviewer run-lifecycle wiring (supervisor + API)
# ---------------------------------------------------------------------------


def _fake_tracker() -> TokenUsageTracker:
    """A stand-in usage tracker that is never over budget."""
    from yukar.usage.tracker import TokenUsageTracker

    tracker = TokenUsageTracker.__new__(TokenUsageTracker)
    tracker.is_over_budget = lambda: False  # type: ignore[attr-defined]
    return tracker


async def _seed_manager_conversation(root: str, pid: str, eid: str) -> None:
    """Seed a couple of Manager↔user messages under the 'manager' thread."""
    from yukar.storage import session_store

    await session_store.ensure_agent(
        root, pid, eid, "manager", {"title": "Trial 1", "role": "manager", "status": "active"}
    )
    await session_store.append_message(
        root, pid, eid, "manager", "user", "use OAuth, not passwords"
    )
    await session_store.append_message(
        root, pid, eid, "manager", "assistant", "Done in auth.py"
    )


class TestReviewerRunLifecycle:
    @pytest.mark.asyncio
    async def test_start_review_creates_thread_and_starts_reviewer_run(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """POST /review creates a reviewer thread and starts a reviewer-mode run
        seeded with the Manager↔user conversation, without touching the epic."""
        from unittest.mock import AsyncMock, patch

        from yukar.api.routers import threads as threads_router
        from yukar.api.routers.threads import StartReviewRequest
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.runs.supervisor import RunSupervisor
        from yukar.storage import threads_repo
        from yukar.storage.epic_repo import get_epic, save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        pid, eid = "p-rev", "EP-rev"
        await save_project(root, Project(id=pid, name=pid))
        await save_epic(
            root,
            pid,
            Epic(
                id=eid,
                slug="s",
                title="Add auth",
                status="open",
                branch="yukar/ep-rev-s",
                active_thread_id="manager",
            ),
        )
        await _seed_manager_conversation(root, pid, eid)

        sup = RunSupervisor()
        mock_start = AsyncMock(return_value="run-rev")
        with patch.object(sup, "start", mock_start):
            entry = await threads_router.start_review(
                project_id=pid,
                epic_id=eid,
                body=StartReviewRequest(),
                root=root,
                supervisor=sup,
                usage_tracker=_fake_tracker(),
            )

        # A reviewer thread was created and persisted.
        assert entry.role == "reviewer"
        assert entry.status == "active"
        assert entry.branch is None
        tf = await threads_repo.get_threads(root, pid, eid)
        assert any(t.id == entry.id and t.role == "reviewer" for t in tf.threads)

        # supervisor.start was called in reviewer mode, bound to the reviewer thread,
        # with the Manager↔user conversation seeded as review_context.
        assert mock_start.await_count == 1
        call = mock_start.await_args
        assert call is not None
        assert call.kwargs["agent_role"] == "reviewer"
        assert call.kwargs["manager_thread_id"] == entry.id
        rc = call.kwargs["review_context"]
        assert "use OAuth, not passwords" in rc
        assert "Done in auth.py" in rc

        # The epic lifecycle is untouched: same active trial, same status.
        loaded = await get_epic(root, pid, eid)
        assert loaded is not None
        assert loaded.active_thread_id == "manager"
        assert loaded.status == "open"

    @pytest.mark.asyncio
    async def test_start_review_409_when_run_active(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """POST /review returns 409 while a run is active (reviewer is exclusive)."""
        from unittest.mock import patch

        from fastapi import HTTPException

        from yukar.api.routers import threads as threads_router
        from yukar.api.routers.threads import StartReviewRequest
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.runs.supervisor import RunSupervisor
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        pid, eid = "p-rev2", "EP-rev2"
        await save_project(root, Project(id=pid, name=pid))
        await save_epic(root, pid, Epic(id=eid, slug="s", title="T", status="open"))

        sup = RunSupervisor()
        with (
            patch.object(sup, "is_running", return_value=True),
            pytest.raises(HTTPException) as ei,
        ):
            await threads_router.start_review(
                project_id=pid,
                epic_id=eid,
                body=StartReviewRequest(),
                root=root,
                supervisor=sup,
                usage_tracker=_fake_tracker(),
            )
        assert ei.value.status_code == 409

    @pytest.mark.asyncio
    async def test_start_review_allowed_on_completed_epic(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """POST /review works on a completed epic — the reviewer is read-only,
        so inspecting finished work never requires reopening the epic
        (regression guard for the 523a495 behaviour, now backend-enforced)."""
        from unittest.mock import AsyncMock, patch

        from yukar.api.routers import threads as threads_router
        from yukar.api.routers.threads import StartReviewRequest
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.runs.supervisor import RunSupervisor
        from yukar.storage import threads_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        pid, eid = "p-rev-done", "EP-rev-done"
        await save_project(root, Project(id=pid, name=pid))
        await save_epic(root, pid, Epic(id=eid, slug="s", title="T", status="completed"))

        sup = RunSupervisor()
        mock_start = AsyncMock(return_value="run-rev")
        with patch.object(sup, "start", mock_start):
            entry = await threads_router.start_review(
                project_id=pid,
                epic_id=eid,
                body=StartReviewRequest(),
                root=root,
                supervisor=sup,
                usage_tracker=_fake_tracker(),
            )
        assert entry.role == "reviewer"
        assert mock_start.await_count == 1
        tf = await threads_repo.get_threads(root, pid, eid)
        assert any(t.id == entry.id and t.role == "reviewer" for t in tf.threads)

    @pytest.mark.asyncio
    async def test_supervisor_start_reviewer_allowed_on_completed_epic(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The supervisor's completed-epic TOCTOU guard only blocks manager runs:
        a reviewer run starts fine on a completed epic (read-only contract)."""
        from unittest.mock import patch

        from yukar.models.epic import Epic
        from yukar.runs.runner import DummyRunner
        from yukar.runs.supervisor import RunSupervisor
        from yukar.storage.epic_repo import get_epic, save_epic

        root = str(tmp_path / "ws")
        pid, eid = "p-rev-done2", "EP-rev-done2"
        await save_epic(root, pid, Epic(id=eid, slug="s", title="T", status="completed"))

        sup = RunSupervisor()
        dummy = DummyRunner()

        async def _instant_start(root_: str, project_id: str, epic_id: str, run_id: str) -> None:
            pass

        with (
            patch.object(dummy, "start", side_effect=_instant_start),
            patch.object(sup, "_make_runner", return_value=dummy),
        ):
            await sup.start(root, pid, eid, manager_thread_id="rev-1", agent_role="reviewer")
            await sup._runs[(pid, eid)].task

        # The epic stays completed — the reviewer run never touches the status.
        loaded = await get_epic(root, pid, eid)
        assert loaded is not None
        assert loaded.status == "completed"

    @pytest.mark.asyncio
    async def test_post_message_to_reviewer_thread_routes_reviewer_mode(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A user reply to a reviewer thread routes through start_or_inject in
        reviewer mode and returns a synthetic ack (FSM is the sole writer)."""
        from unittest.mock import AsyncMock, patch

        from yukar.api.routers import threads as threads_router
        from yukar.api.routers.threads import PostMessageRequest
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.thread import ThreadEntry
        from yukar.runs.supervisor import RunSupervisor
        from yukar.storage import threads_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        pid, eid = "p-rev3", "EP-rev3"
        await save_project(root, Project(id=pid, name=pid))
        await save_epic(
            root,
            pid,
            Epic(id=eid, slug="s", title="T", status="open", active_thread_id="manager"),
        )
        await _seed_manager_conversation(root, pid, eid)
        await threads_repo.add_thread(
            root,
            pid,
            eid,
            ThreadEntry(id="rev-1", title="Review 1", role="reviewer", status="active"),
        )

        sup = RunSupervisor()
        mock_soi = AsyncMock(return_value=True)
        with patch.object(sup, "start_or_inject", mock_soi):
            msg = await threads_router.post_message(
                project_id=pid,
                epic_id=eid,
                thread_id="rev-1",
                body=PostMessageRequest(content="what about tests?"),
                root=root,
                supervisor=sup,
            )

        # Synthetic ack — the message is not persisted here.
        assert msg.message_id == -1
        assert mock_soi.await_count == 1
        call = mock_soi.await_args
        assert call is not None
        assert call.args[3] == "rev-1"
        assert call.args[4] == "what about tests?"
        assert call.kwargs["agent_role"] == "reviewer"
        # A reply is always an inject or a continuation (seed_prompt wins), so the
        # reviewer branch does NOT rebuild review_context — it would be wasted work.
        assert "review_context" not in call.kwargs

    @pytest.mark.asyncio
    async def test_reviewer_start_leaves_epic_status_untouched(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """supervisor.start(agent_role='reviewer') runs read-only: it never fires
        an epic.yaml status transition and passes the reviewer args to _make_runner."""
        from unittest.mock import patch

        from yukar.models.epic import Epic
        from yukar.runs.runner import DummyRunner
        from yukar.runs.supervisor import RunSupervisor
        from yukar.storage.epic_repo import get_epic, save_epic

        root = str(tmp_path / "ws")
        pid, eid = "p-rev4", "EP-rev4"
        await save_epic(root, pid, Epic(id=eid, slug="s", title="T", status="open"))

        sup = RunSupervisor()
        dummy = DummyRunner()

        async def _instant_start(root_: str, project_id: str, epic_id: str, run_id: str) -> None:
            pass

        with (
            patch.object(dummy, "start", side_effect=_instant_start),
            patch.object(sup, "_make_runner", return_value=dummy) as mk,
        ):
            await sup.start(
                root,
                pid,
                eid,
                manager_thread_id="rev-1",
                agent_role="reviewer",
                review_context="CTX",
            )
            # Await the background run task to completion (deterministic).
            task = sup._runs[(pid, eid)].task
            await task

        mk.assert_called_once_with(
            manager_thread_id="rev-1", agent_role="reviewer", review_context="CTX"
        )
        loaded = await get_epic(root, pid, eid)
        assert loaded is not None
        assert loaded.status == "open"  # unchanged by the reviewer run


def _reviewer_orchestrator():  # type: ignore[no-untyped-def]
    """A minimal reviewer-mode EpicOrchestrator for testing pure helpers."""
    from yukar.agents.orchestrator import EpicOrchestrator
    from yukar.config.settings import LLMSettings

    return EpicOrchestrator(
        llm_settings=LLMSettings(provider="fake"),
        git_author_name="x",
        git_author_email="y@z",
        agent_role="reviewer",
    )


class TestReviewerWorktreeTools:
    """_build_worktree_ro_tools binds run_tests/fs_read/repo_grep for EVERY repo
    registered in the project, multi-repo aware (the tools take a `repo` arg),
    preferring the active manager trial's worktree and falling back to a repo's
    base checkout when no worktree exists yet.  (The positive single-repo path is
    also exercised end-to-end by e2e/reviewer.spec.ts, whose fake reviewer calls
    fs_read on the manager trial's worktree.)"""

    @pytest.mark.asyncio
    async def test_multi_repo_binds_per_repo_tools(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A multi-repo epic gets ONE set of tools that dispatch by `repo`; omitting
        `repo` errors (ambiguous), and each repo reads its own worktree."""
        from yukar.config import paths as p
        from yukar.models.epic import Epic
        from yukar.models.project import Project, Repo
        from yukar.storage.project_repo import save_project, save_repo

        root = str(tmp_path / "ws")
        pid, eid = "p-multi", "EP-multi"
        await save_project(root, Project(id=pid, name=pid))
        for name in ("repoA", "repoB"):
            rdir = tmp_path / name
            rdir.mkdir()
            await save_repo(root, pid, Repo(name=name, path=str(rdir)))
            # Active trial's worktree (active_thread_id=None → trial "manager").
            wt = p.worktree_dir(root, pid, eid, "manager", name)
            wt.mkdir(parents=True)
            (wt / "marker.txt").write_text(f"content of {name}")

        orch = _reviewer_orchestrator()
        orch._epic = Epic(
            id=eid, slug="s", title="T", status="open", touched_repos=["repoA", "repoB"]
        )
        tools = await orch._build_worktree_ro_tools(root, pid, eid)
        by_name = {getattr(t, "tool_name", None): t for t in tools}
        # One set of tools (not duplicated per repo).
        assert set(by_name) == {"run_tests", "fs_read", "repo_grep"}

        fs_read = by_name["fs_read"]
        # Dispatch to the correct worktree via `repo`.
        a = fs_read(path="marker.txt", repo="repoA")
        assert a["status"] == "success"
        assert "content of repoA" in a["content"][0]["text"]
        b = fs_read(path="marker.txt", repo="repoB")
        assert b["status"] == "success"
        assert "content of repoB" in b["content"][0]["text"]
        # `repo` is always required → omitting it errors (lists available repos).
        missing_repo = fs_read(path="marker.txt")
        assert missing_repo["status"] == "error"
        # Unknown repo → error.
        unknown = fs_read(path="marker.txt", repo="nope")
        assert unknown["status"] == "error"

    @pytest.mark.asyncio
    async def test_missing_worktree_falls_back_to_base_checkout(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A registered repo with NO trial worktree still gets tools, reading the
        repo's base checkout — inspection must work from Turn 0, before any task
        has created a worktree."""
        from yukar.models.epic import Epic
        from yukar.models.project import Project, Repo
        from yukar.storage.project_repo import save_project, save_repo

        root = str(tmp_path / "ws")
        pid, eid = "p-base", "EP-base"
        await save_project(root, Project(id=pid, name=pid))
        # Register a repo whose base checkout has a file — but never create a worktree.
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "base.txt").write_text("base checkout content")
        await save_repo(root, pid, Repo(name="myrepo", path=str(repo_dir)))

        orch = _reviewer_orchestrator()
        # touched_repos is empty: the repo has not been touched by any task yet.
        orch._epic = Epic(id=eid, slug="s", title="T", status="open", touched_repos=[])
        tools = await orch._build_worktree_ro_tools(root, pid, eid)
        by_name = {getattr(t, "tool_name", None): t for t in tools}
        assert set(by_name) == {"run_tests", "fs_read", "repo_grep"}
        # fs_read resolves against the base checkout when no worktree exists.
        result = by_name["fs_read"](path="base.txt", repo="myrepo")
        assert result["status"] == "success"
        assert "base checkout content" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_no_registered_repos_yields_no_tools(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A project with no registered repos gets no worktree tools."""
        from yukar.models.epic import Epic

        orch = _reviewer_orchestrator()
        orch._epic = Epic(
            id="EP-nw", slug="s", title="T", status="open", touched_repos=["myrepo"]
        )
        # No project/repo was ever saved under (root, "p").
        tools = await orch._build_worktree_ro_tools(str(tmp_path / "ws"), "p", "EP-nw")
        assert tools == []

    @pytest.mark.asyncio
    async def test_single_repo_with_worktree_binds_read_only_tools(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A single-repo epic with an existing trial worktree gets exactly
        run_tests / fs_read / repo_grep (read-only), no fs_write/list/delete."""
        from yukar.config import paths as p
        from yukar.models.epic import Epic
        from yukar.models.project import Project, Repo
        from yukar.storage.project_repo import save_project, save_repo

        root = str(tmp_path / "ws")
        pid, eid = "p-ctx", "EP-ctx"
        await save_project(root, Project(id=pid, name=pid))
        # Register a repo (path need not be a real git repo for tool construction).
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        await save_repo(root, pid, Repo(name="myrepo", path=str(repo_dir)))

        # Create the active trial's worktree (active_thread_id=None → trial "manager").
        wt = p.worktree_dir(root, pid, eid, "manager", "myrepo")
        wt.mkdir(parents=True)

        orch = _reviewer_orchestrator()
        orch._epic = Epic(
            id=eid, slug="s", title="T", status="open", touched_repos=["myrepo"]
        )
        tools = await orch._build_worktree_ro_tools(root, pid, eid)
        names = {getattr(t, "tool_name", None) for t in tools}
        assert names == {"run_tests", "fs_read", "repo_grep"}
        # Read-only: none of the mutating fs tools leak in.
        assert "fs_write" not in names
        assert "fs_delete" not in names

    @pytest.mark.asyncio
    async def test_manager_variant_excludes_run_tests(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """include_run_tests=False (Manager) yields exactly fs_read + repo_grep
        (read-only branch inspection), never run_tests or any mutating fs tool."""
        from yukar.config import paths as p
        from yukar.models.epic import Epic
        from yukar.models.project import Project, Repo
        from yukar.storage.project_repo import save_project, save_repo

        root = str(tmp_path / "ws")
        pid, eid = "p-mgr", "EP-mgr"
        await save_project(root, Project(id=pid, name=pid))
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        await save_repo(root, pid, Repo(name="myrepo", path=str(repo_dir)))
        wt = p.worktree_dir(root, pid, eid, "manager", "myrepo")
        wt.mkdir(parents=True)
        (wt / "marker.txt").write_text("hello from myrepo")

        orch = _reviewer_orchestrator()
        orch._epic = Epic(
            id=eid, slug="s", title="T", status="open", touched_repos=["myrepo"]
        )
        tools = await orch._build_worktree_ro_tools(root, pid, eid, include_run_tests=False)
        by_name = {getattr(t, "tool_name", None): t for t in tools}
        assert set(by_name) == {"fs_read", "repo_grep"}
        assert "run_tests" not in by_name
        assert "fs_write" not in by_name
        assert "fs_delete" not in by_name
        # `repo` is required even for a single-repo epic (no default/auto-pick).
        result = by_name["fs_read"](path="marker.txt", repo="myrepo")
        assert result["status"] == "success"
        assert "hello from myrepo" in result["content"][0]["text"]
        # Omitting `repo` errors rather than silently picking the sole repo.
        assert by_name["fs_read"](path="marker.txt")["status"] == "error"


class TestReviewerLegacyEpic:
    """A legacy epic (created before the trial/session decoupling and its new
    branch-naming) must not become inconsistent, and the Reviewer must launch on
    it.  Legacy shape: epic.active_thread_id is None, the single manager thread
    is 'manager' with trial_id=None, and the worktree lives at the pre-decoupling
    path worktrees/manager/{repo} (which resolve_active_trial_id → 'manager' →
    worktree_dir(..., 'manager', ...) still points at)."""

    @pytest.mark.asyncio
    async def test_reviewer_launches_on_legacy_session(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, patch

        from yukar.agents.trials import resolve_active_trial_id
        from yukar.api.routers import threads as threads_router
        from yukar.api.routers.threads import StartReviewRequest
        from yukar.config import paths as p
        from yukar.models.epic import Epic
        from yukar.models.project import Project, Repo
        from yukar.models.thread import ThreadEntry
        from yukar.runs.supervisor import RunSupervisor
        from yukar.storage import threads_repo
        from yukar.storage.epic_repo import get_epic, save_epic
        from yukar.storage.project_repo import save_project, save_repo

        root = str(tmp_path / "ws")
        pid, eid = "p-legacy", "EP-legacy"
        await save_project(root, Project(id=pid, name=pid))
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        await save_repo(root, pid, Repo(name="myrepo", path=str(repo_dir)))

        # Legacy epic on disk: old-style branch, NO active_thread_id.
        await save_epic(
            root,
            pid,
            Epic(
                id=eid,
                slug="legacy",
                title="Legacy",
                status="open",
                branch="yukar/ep-legacy-legacy",
                touched_repos=["myrepo"],
                active_thread_id=None,
            ),
        )
        # Legacy single manager thread: trial_id=None (pre-decoupling).
        await threads_repo.add_thread(
            root,
            pid,
            eid,
            ThreadEntry(id="manager", title="Trial 1", role="manager", status="resolved"),
        )
        # Legacy worktree at the pre-decoupling path (keyed by "manager").
        p.worktree_dir(root, pid, eid, "manager", "myrepo").mkdir(parents=True)
        # A legacy Manager↔user conversation under the "manager" thread.
        await _seed_manager_conversation(root, pid, eid)

        epic = await get_epic(root, pid, eid)
        assert epic is not None

        # 1) No branch-naming inconsistency: the trial resolves to the legacy
        #    "manager" worktree id (the same path the pre-decoupling code used).
        assert await resolve_active_trial_id(root, pid, eid, epic) == "manager"

        # 2) The Reviewer's worktree tools bind to the legacy worktree.
        orch = _reviewer_orchestrator()
        orch._epic = epic
        tools = await orch._build_worktree_ro_tools(root, pid, eid)
        assert {getattr(t, "tool_name", None) for t in tools} == {
            "run_tests",
            "fs_read",
            "repo_grep",
        }

        # 3) POST /review launches on the legacy session: a reviewer thread is
        #    created, seeded from the legacy "manager" conversation, and the run
        #    starts in reviewer mode — without touching the legacy epic.
        sup = RunSupervisor()
        mock_start = AsyncMock(return_value="run-rev")
        with patch.object(sup, "start", mock_start):
            entry = await threads_router.start_review(
                project_id=pid,
                epic_id=eid,
                body=StartReviewRequest(),
                root=root,
                supervisor=sup,
                usage_tracker=_fake_tracker(),
            )
        assert entry.role == "reviewer"
        call = mock_start.await_args
        assert call is not None
        assert call.kwargs["agent_role"] == "reviewer"
        assert "use OAuth, not passwords" in call.kwargs["review_context"]

        # The legacy epic is untouched: active_thread_id stays None, status open.
        loaded = await get_epic(root, pid, eid)
        assert loaded is not None
        assert loaded.active_thread_id is None
        assert loaded.status == "open"


# ---------------------------------------------------------------------------
# Regression: a live reviewer run holds the epic's single run slot, so trial
# mutations must be rejected — even though the reviewer's run is bound to the
# reviewer thread (invisible to a per-manager-trial run check).  Without the
# epic-level is_running guard, "continue on current branch" would archive the
# manager conversation and repoint active_thread_id while the reviewer keeps the
# slot, wedging the epic (new trial can be neither run nor messaged).
# ---------------------------------------------------------------------------


class TestReviewerBlocksTrialMutations:
    @staticmethod
    def _inject_reviewer_run(sup, root: str, pid: str, eid: str, reviewer_thread_id: str) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        from yukar.runs.supervisor import _RunHandle

        task = MagicMock()
        task.done.return_value = False
        sup._runs[sup._key(pid, eid)] = _RunHandle(
            run_id="run-rev",
            runner=MagicMock(is_parked=False),  # executing (not parked)
            task=task,
            root=root,
            project_id=pid,
            epic_id=eid,
            manager_thread_id=reviewer_thread_id,  # bound to the reviewer, not th-M
        )

    @staticmethod
    async def _setup(  # type: ignore[no-untyped-def]
        tmp_path,
        *,
        manager_status: Literal["active", "resolved", "failed", "archived"] = "active",
    ):
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.models.thread import ThreadEntry
        from yukar.runs.supervisor import RunSupervisor
        from yukar.storage import threads_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        pid, eid = "p-rev-guard", "EP-rev-guard"
        await save_project(root, Project(id=pid, name=pid))
        await save_epic(
            root,
            pid,
            Epic(
                id=eid,
                slug="s",
                title="T",
                status="open",
                branch="yukar/ep-rev-guard",
                active_thread_id="th-M",
            ),
        )
        await threads_repo.add_thread(
            root,
            pid,
            eid,
            ThreadEntry(
                id="th-M",
                title="Trial 1",
                role="manager",
                status=manager_status,
                branch="yukar/ep-rev-guard",
                trial_id="th-M",
            ),
        )
        await threads_repo.add_thread(
            root,
            pid,
            eid,
            ThreadEntry(id="th-REV", title="Review 1", role="reviewer", status="active"),
        )
        sup = RunSupervisor()
        TestReviewerBlocksTrialMutations._inject_reviewer_run(sup, root, pid, eid, "th-REV")
        return root, pid, eid, sup

    @pytest.mark.asyncio
    async def test_same_branch_409_while_reviewer_active(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from fastapi import HTTPException

        from yukar.api.routers import threads as threads_router
        from yukar.api.routers.threads import CreateThreadRequest
        from yukar.storage import threads_repo
        from yukar.storage.epic_repo import get_epic

        root, pid, eid, sup = await self._setup(tmp_path)
        # The live run is bound to the reviewer thread (th-REV), not the manager
        # trial (th-M) — exactly what a per-manager-trial run check would miss, so
        # the guard must reject on the epic-level is_running instead.
        assert sup.is_running(pid, eid) is True
        assert sup._runs[sup._key(pid, eid)].manager_thread_id == "th-REV"

        with pytest.raises(HTTPException) as ei:
            await threads_router.create_thread(
                project_id=pid,
                epic_id=eid,
                body=CreateThreadRequest(role="manager", same_branch=True, title=""),
                root=root,
                supervisor=sup,
            )
        assert ei.value.status_code == 409

        # The manager conversation was NOT archived and active_thread_id is intact.
        tf = await threads_repo.get_threads(root, pid, eid)
        assert next(t for t in tf.threads if t.id == "th-M").status == "active"
        loaded = await get_epic(root, pid, eid)
        assert loaded is not None
        assert loaded.active_thread_id == "th-M"

    @pytest.mark.asyncio
    async def test_archive_active_new_trial_409_while_reviewer_active(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from fastapi import HTTPException

        from yukar.api.routers import threads as threads_router
        from yukar.api.routers.threads import CreateThreadRequest
        from yukar.storage import threads_repo

        root, pid, eid, sup = await self._setup(tmp_path)
        with pytest.raises(HTTPException) as ei:
            await threads_router.create_thread(
                project_id=pid,
                epic_id=eid,
                body=CreateThreadRequest(role="manager", archive_active=True, title="Trial 2"),
                root=root,
                supervisor=sup,
            )
        assert ei.value.status_code == 409
        tf = await threads_repo.get_threads(root, pid, eid)
        assert next(t for t in tf.threads if t.id == "th-M").status == "active"

    @pytest.mark.asyncio
    async def test_new_trial_409_while_reviewer_active_after_manager_resolved(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """Even with a legacy resolved manager entry — the path with no
        archive_active — a new trial is blocked while the reviewer holds the slot."""
        from fastapi import HTTPException

        from yukar.api.routers import threads as threads_router
        from yukar.api.routers.threads import CreateThreadRequest

        root, pid, eid, sup = await self._setup(tmp_path, manager_status="resolved")
        with pytest.raises(HTTPException) as ei:
            await threads_router.create_thread(
                project_id=pid,
                epic_id=eid,
                body=CreateThreadRequest(role="manager", title="Trial 2"),
                root=root,
                supervisor=sup,
            )
        assert ei.value.status_code == 409

    @pytest.mark.asyncio
    async def test_archive_thread_409_while_reviewer_active(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from fastapi import HTTPException

        from yukar.api.routers import threads as threads_router
        from yukar.storage import threads_repo

        root, pid, eid, sup = await self._setup(tmp_path)
        with pytest.raises(HTTPException) as ei:
            await threads_router.archive_thread(
                project_id=pid,
                epic_id=eid,
                thread_id="th-M",
                root=root,
                supervisor=sup,
            )
        assert ei.value.status_code == 409
        tf = await threads_repo.get_threads(root, pid, eid)
        assert next(t for t in tf.threads if t.id == "th-M").status == "active"

    @pytest.mark.asyncio
    async def test_user_thread_creation_allowed_while_reviewer_active(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The guard is scoped to trial (manager) mutations; ad-hoc user threads
        do not touch trials/active_thread_id and remain creatable during a run."""
        from yukar.api.routers import threads as threads_router
        from yukar.api.routers.threads import CreateThreadRequest

        root, pid, eid, sup = await self._setup(tmp_path)
        entry = await threads_router.create_thread(
            project_id=pid,
            epic_id=eid,
            body=CreateThreadRequest(role="user", title="notes"),
            root=root,
            supervisor=sup,
        )
        assert entry.role == "user"
