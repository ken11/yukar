"""User-facing language: the Web UI locale reaches the Manager / Reviewer prompt.

The browser sends ``X-Yukar-Locale`` on every request; run-starting endpoints
forward it through the supervisor to the orchestrator, which appends a
language directive to the Manager / Reviewer system prompt.
"""

from __future__ import annotations

from typing import Any

import pytest


class TestParseUiLanguage:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ja", "ja"),
            ("en", "en"),
            ("JA", "ja"),
            (" en ", "en"),
            ("ja-JP", "ja"),
            ("en_US", "en"),
            ("fr", None),
            ("", None),
            (None, None),
        ],
    )
    def test_parse(self, raw: str | None, expected: str | None) -> None:
        from yukar.models.language import parse_ui_language

        assert parse_ui_language(raw) == expected

    def test_dependency_reads_header(self) -> None:
        from yukar.deps import get_ui_language

        assert get_ui_language("ja") == "ja"
        assert get_ui_language(None) is None


class TestLanguageDirective:
    def test_none_yields_no_directive(self) -> None:
        from yukar.agents.prompts import language_directive

        assert language_directive(None) == ""
        assert language_directive("zz") == ""

    def test_japanese_directive_is_explicit(self) -> None:
        from yukar.agents.prompts import language_directive

        text = language_directive("ja")
        assert "## Language (hard rule)" in text
        assert "Japanese" in text
        assert "task_update" in text  # task titles/contracts follow the language too
        assert "Never switch" in text

    def test_english_directive(self) -> None:
        from yukar.agents.prompts import language_directive

        assert "English" in language_directive("en")


class TestLanguagePlumbing:
    def test_orchestrator_stores_language(self) -> None:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(provider="fake"),
            git_author_name="yukar",
            git_author_email="yukar@localhost",
            language="ja",
        )
        assert orch._language == "ja"

    def test_supervisor_runner_factories_forward_language(self) -> None:
        from yukar.config.settings import LLMSettings, Settings
        from yukar.runs.supervisor import RunSupervisor

        settings = Settings(llm=LLMSettings(provider="fake"))
        sup = RunSupervisor(settings_getter=lambda: settings)
        fresh = sup._make_runner(language="en")
        cont = sup._make_continuation_runner("hello", language="ja")
        assert getattr(fresh, "_language", None) == "en"
        assert getattr(cont, "_language", None) == "ja"

    async def test_start_or_inject_forwards_language_to_continuation(self) -> None:
        from unittest.mock import AsyncMock, patch

        from yukar.runs.supervisor import RunSupervisor

        sup = RunSupervisor()
        mock_cont = AsyncMock(return_value="run-x")
        with patch.object(sup, "start_continuation", mock_cont):
            forwarded = await sup.start_or_inject(
                "/tmp/ws", "p", "e", "manager", "hi", language="ja"
            )
        assert forwarded is False
        assert mock_cont.await_args is not None
        assert mock_cont.await_args.kwargs["language"] == "ja"

    async def test_run_loop_appends_directive_to_system_prompt(self, tmp_path: Any) -> None:
        """A scripted Manager run built with language='ja' carries the directive."""
        from unittest.mock import patch

        from tests._helpers import make_git_repo, run_until_parked
        from tests.test_dispatch_agents import _bootstrap, _make_workspace
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings
        from yukar.llm.fake import FakeModel, TextTurn

        repo = make_git_repo(tmp_path, "myrepo")
        root, project_id, epic_id = _make_workspace(tmp_path)
        await _bootstrap(root, project_id, epic_id, repo)

        seen_prompts: list[str] = []
        real_agent_cls = __import__("strands").Agent

        class _SpyAgent(real_agent_cls):  # type: ignore[misc]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                seen_prompts.append(str(kwargs.get("system_prompt", "")))
                super().__init__(*args, **kwargs)

        def fake_create_model(settings: Any, role: Any = None, **kwargs: Any) -> FakeModel:
            return FakeModel(script=[TextTurn("計画を提示します。")])

        with (
            patch("yukar.agents.orchestrator.create_model", side_effect=fake_create_model),
            patch("yukar.agents.orchestrator.Agent", _SpyAgent),
        ):
            orch = EpicOrchestrator(
                llm_settings=LLMSettings(provider="fake"),
                git_author_name="yukar",
                git_author_email="yukar@localhost",
                language="ja",
            )
            await run_until_parked(orch, root, project_id, epic_id, "run-lang")

        assert seen_prompts, "the Manager Agent was not constructed"
        assert "## Language (hard rule)" in seen_prompts[0]
        assert "Japanese" in seen_prompts[0]
        # The directive is the LAST section of the system prompt.
        assert seen_prompts[0].rstrip().endswith("ask you to switch.")
