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

from typing import get_args

import pytest
from httpx import AsyncClient


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
            manager_conversation="**User:** use OAuth, not passwords.\n\n**Manager:** Done in auth.py.",
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
