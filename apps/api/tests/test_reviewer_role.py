"""Reviewer role (Phase 2 of the trial/session decoupling).

The Reviewer is a read-only, conversational agent the user spawns at in_review
to independently check the Manager's work against the epic's intent and report
back to the USER (it never instructs the Manager directly).  It reuses the
orchestrator's conversation loop in a read-only "reviewer mode".

Phase 2a (this batch): role plumbing — reviewer is a first-class AgentRole /
ThreadRole / ConfigurableAgentRole / UserCreatableThreadRole, with an optional
per-role model override.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, get_args

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
            # Manager narration + an ask_user question (tool_use).
            Message(
                message=MessagePayload(
                    role="assistant",
                    content=[
                        ContentPart(text="Here is my plan: add auth.py."),
                        ContentPart(
                            tool_use=ToolUseBlock(
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
                            tool_use=ToolUseBlock(toolUseId="t2", name="dispatch", input={})
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
                            tool_result=ToolResultBlock(toolUseId="t2", text="worker output blob")
                        )
                    ],
                ),
                message_id=3,
            ),
        ]
        out = format_manager_conversation(messages)
        assert "Here is my plan: add auth.py." in out
        assert "OAuth or password login?" in out  # ask_user question kept
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
                status="in_review",
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
        assert loaded.status == "in_review"

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
        await save_epic(root, pid, Epic(id=eid, slug="s", title="T", status="in_review"))

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
    async def test_start_review_409_when_epic_closed_leaves_no_orphan(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """POST /review on a closed epic returns 409 BEFORE creating any thread —
        reviewer threads cannot be archived, so an orphan would be permanent."""
        from unittest.mock import patch

        from fastapi import HTTPException

        from yukar.api.routers import threads as threads_router
        from yukar.api.routers.threads import StartReviewRequest
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.runs.supervisor import RunSupervisor
        from yukar.storage import threads_repo
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_path / "ws")
        pid, eid = "p-rev-closed", "EP-rev-closed"
        await save_project(root, Project(id=pid, name=pid))
        await save_epic(root, pid, Epic(id=eid, slug="s", title="T", status="closed"))

        sup = RunSupervisor()
        # start() is patched to assert it is NEVER reached (the guard must fire first).
        with (
            patch.object(sup, "start", side_effect=AssertionError("start must not run")),
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
        # No reviewer thread was persisted — nothing to orphan.
        tf = await threads_repo.get_threads(root, pid, eid)
        assert not any(t.role == "reviewer" for t in tf.threads)

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
            Epic(id=eid, slug="s", title="T", status="in_review", active_thread_id="manager"),
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
        await save_epic(root, pid, Epic(id=eid, slug="s", title="T", status="in_review"))

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
        assert loaded.status == "in_review"  # unchanged by the reviewer run
