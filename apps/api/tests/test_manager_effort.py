"""Tests for Manager reasoning effort feature.

Covers:
1. factory: bedrock + effort → additional_request_fields has thinking/output_config.effort.
2. factory: bedrock + no effort → no additional_request_fields.
3. factory: anthropic + effort → params has thinking/output_config.effort.
4. factory: anthropic + no effort → params absent.
5. epics router: POST with manager_effort saves and returns it.
6. epics router: POST without manager_effort defaults to "high".
7. epics router: PATCH updates manager_effort.
8. epics router: PATCH without manager_effort leaves it unchanged.
9. Epic model: manager_effort field defaults to "high", accepts all three values.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# 1-4. LLM factory tests
# ---------------------------------------------------------------------------


class TestCreateModelEffortBedrock:
    """create_model with provider='bedrock' and effort parameter."""

    def _make_settings(self, prompt_caching: bool = False):  # type: ignore[return]
        from yukar.config.settings import LLMSettings

        return LLMSettings(
            provider="bedrock",
            model_id="anthropic.claude-opus-4",
            prompt_caching=prompt_caching,
        )

    def test_effort_high_sets_additional_request_fields(self) -> None:
        from strands.models.bedrock import BedrockModel

        from yukar.llm.factory import create_model

        model = create_model(self._make_settings(), effort="high")
        assert isinstance(model, BedrockModel)
        cfg = cast(dict[str, Any], model.config)  # type: ignore[attr-defined]
        assert "additional_request_fields" in cfg
        arf = cast(dict[str, Any], cfg["additional_request_fields"])
        assert arf["thinking"] == {"type": "adaptive"}
        # effort is nested under output_config (not top-level): Bedrock
        # ConverseStream rejects a top-level ``effort`` field.
        assert arf["output_config"] == {"effort": "high"}

    def test_effort_xhigh_sets_additional_request_fields(self) -> None:
        from strands.models.bedrock import BedrockModel

        from yukar.llm.factory import create_model

        model = create_model(self._make_settings(), effort="xhigh")
        assert isinstance(model, BedrockModel)
        cfg = cast(dict[str, Any], model.config)  # type: ignore[attr-defined]
        arf = cast(dict[str, Any], cfg["additional_request_fields"])
        assert arf["output_config"] == {"effort": "xhigh"}
        assert arf["thinking"] == {"type": "adaptive"}

    def test_effort_max_sets_additional_request_fields(self) -> None:
        from strands.models.bedrock import BedrockModel

        from yukar.llm.factory import create_model

        model = create_model(self._make_settings(), effort="max")
        assert isinstance(model, BedrockModel)
        cfg = cast(dict[str, Any], model.config)  # type: ignore[attr-defined]
        arf = cast(dict[str, Any], cfg["additional_request_fields"])
        assert arf["output_config"] == {"effort": "max"}

    def test_no_effort_no_additional_request_fields(self) -> None:
        from strands.models.bedrock import BedrockModel

        from yukar.llm.factory import create_model

        model = create_model(self._make_settings())
        assert isinstance(model, BedrockModel)
        cfg = cast(dict[str, Any], model.config)  # type: ignore[attr-defined]
        assert "additional_request_fields" not in cfg

    def test_effort_with_prompt_caching_sets_fields(self) -> None:
        """Effort is injected even when prompt_caching=True (cache + thinking together)."""
        from strands.models.bedrock import BedrockModel

        from yukar.llm.factory import create_model

        model = create_model(self._make_settings(prompt_caching=True), effort="high")
        assert isinstance(model, BedrockModel)
        cfg = cast(dict[str, Any], model.config)  # type: ignore[attr-defined]
        assert "additional_request_fields" in cfg
        arf = cast(dict[str, Any], cfg["additional_request_fields"])
        assert arf["thinking"] == {"type": "adaptive"}

    def test_prompt_caching_without_effort_no_additional_request_fields(self) -> None:
        from strands.models.bedrock import BedrockModel

        from yukar.llm.factory import create_model

        model = create_model(self._make_settings(prompt_caching=True))
        assert isinstance(model, BedrockModel)
        cfg = cast(dict[str, Any], model.config)  # type: ignore[attr-defined]
        assert "additional_request_fields" not in cfg


class TestCreateModelEffortAnthropic:
    """create_model with provider='anthropic' and effort parameter."""

    def _make_settings(self, prompt_caching: bool = False):  # type: ignore[return]
        from yukar.config.settings import LLMSettings

        return LLMSettings(
            provider="anthropic",
            model_id="claude-opus-4-5",
            prompt_caching=prompt_caching,
        )

    def test_effort_high_sets_params(self) -> None:
        from strands.models.anthropic import AnthropicModel

        from yukar.llm.factory import create_model

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            model = create_model(self._make_settings(), effort="high")
        assert isinstance(model, AnthropicModel)
        cfg = cast(dict[str, Any], model.config)  # type: ignore[attr-defined]
        assert "params" in cfg
        params = cast(dict[str, Any], cfg["params"])
        assert params["thinking"] == {"type": "adaptive"}
        assert params["output_config"] == {"effort": "high"}

    def test_effort_xhigh_sets_params(self) -> None:
        from strands.models.anthropic import AnthropicModel

        from yukar.llm.factory import create_model

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            model = create_model(self._make_settings(), effort="xhigh")
        assert isinstance(model, AnthropicModel)
        cfg = cast(dict[str, Any], model.config)  # type: ignore[attr-defined]
        params = cast(dict[str, Any], cfg["params"])
        assert params["output_config"] == {"effort": "xhigh"}

    def test_no_effort_no_params(self) -> None:
        from strands.models.anthropic import AnthropicModel

        from yukar.llm.factory import create_model

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            model = create_model(self._make_settings())
        assert isinstance(model, AnthropicModel)
        cfg = cast(dict[str, Any], model.config)  # type: ignore[attr-defined]
        assert "params" not in cfg

    def test_effort_with_caching_sets_params(self) -> None:
        """Effort is injected on CachingAnthropicModel when prompt_caching=True."""
        from yukar.llm.anthropic_cache import CachingAnthropicModel
        from yukar.llm.factory import create_model

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            model = create_model(self._make_settings(prompt_caching=True), effort="max")
        assert isinstance(model, CachingAnthropicModel)
        cfg = cast(dict[str, Any], model.config)  # type: ignore[attr-defined]
        assert "params" in cfg
        params = cast(dict[str, Any], cfg["params"])
        assert params["thinking"] == {"type": "adaptive"}
        assert params["output_config"] == {"effort": "max"}

    def test_caching_without_effort_no_params(self) -> None:
        from yukar.llm.anthropic_cache import CachingAnthropicModel
        from yukar.llm.factory import create_model

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            model = create_model(self._make_settings(prompt_caching=True))
        assert isinstance(model, CachingAnthropicModel)
        cfg = cast(dict[str, Any], model.config)  # type: ignore[attr-defined]
        assert "params" not in cfg


# ---------------------------------------------------------------------------
# 5-8. Epics router tests
# ---------------------------------------------------------------------------


async def _setup_project(root: str, project_id: str = "proj") -> None:
    from yukar.models.project import Project
    from yukar.storage.project_repo import save_project

    await save_project(root, Project(id=project_id, name=project_id))


class TestCreateEpicManagerEffort:
    async def test_default_effort_is_high(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        await _setup_project(root)

        resp = await app_client.post(
            "/api/projects/proj/epics",
            json={"title": "My Epic"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["manager_effort"] == "high"

    async def test_explicit_xhigh(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        await _setup_project(root)

        resp = await app_client.post(
            "/api/projects/proj/epics",
            json={"title": "My Epic", "manager_effort": "xhigh"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["manager_effort"] == "xhigh"

    async def test_explicit_max(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        await _setup_project(root)

        resp = await app_client.post(
            "/api/projects/proj/epics",
            json={"title": "My Epic", "manager_effort": "max"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["manager_effort"] == "max"

    async def test_invalid_effort_rejected(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        await _setup_project(root)

        resp = await app_client.post(
            "/api/projects/proj/epics",
            json={"title": "My Epic", "manager_effort": "low"},
        )
        assert resp.status_code == 422

    async def test_effort_persisted_to_disk(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        await _setup_project(root)

        resp = await app_client.post(
            "/api/projects/proj/epics",
            json={"title": "Persist Test", "manager_effort": "max"},
        )
        assert resp.status_code == 201, resp.text
        epic_id = resp.json()["id"]

        from yukar.storage.epic_repo import get_epic

        loaded = await get_epic(root, "proj", epic_id)
        assert loaded is not None
        assert loaded.manager_effort == "max"


class TestPatchEpicManagerEffort:
    async def _create_epic(self, app_client: Any, root: str) -> str:
        await _setup_project(root)
        resp = await app_client.post(
            "/api/projects/proj/epics",
            json={"title": "Patch Target"},
        )
        assert resp.status_code == 201, resp.text
        return cast(str, resp.json()["id"])

    async def test_patch_updates_effort(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        epic_id = await self._create_epic(app_client, root)

        resp = await app_client.patch(
            f"/api/projects/proj/epics/{epic_id}",
            json={"manager_effort": "xhigh"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["manager_effort"] == "xhigh"

    async def test_patch_without_effort_leaves_unchanged(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        root = str(tmp_workspace)
        epic_id = await self._create_epic(app_client, root)

        # First, set to max
        await app_client.patch(
            f"/api/projects/proj/epics/{epic_id}",
            json={"manager_effort": "max"},
        )
        # Now patch title only — effort should remain "max"
        resp = await app_client.patch(
            f"/api/projects/proj/epics/{epic_id}",
            json={"title": "New Title"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["manager_effort"] == "max"

    async def test_patch_effort_persisted(self, app_client: Any, tmp_workspace: Path) -> None:
        root = str(tmp_workspace)
        epic_id = await self._create_epic(app_client, root)

        await app_client.patch(
            f"/api/projects/proj/epics/{epic_id}",
            json={"manager_effort": "xhigh"},
        )

        from yukar.storage.epic_repo import get_epic

        loaded = await get_epic(root, "proj", epic_id)
        assert loaded is not None
        assert loaded.manager_effort == "xhigh"


# ---------------------------------------------------------------------------
# 9. Epic model unit tests
# ---------------------------------------------------------------------------


class TestEpicManagerEffortField:
    def test_default_is_high(self) -> None:
        from yukar.models.epic import Epic

        e = Epic(id="EP-1", slug="s", title="T")
        assert e.manager_effort == "high"

    def test_accepts_xhigh(self) -> None:
        from yukar.models.epic import Epic

        e = Epic(id="EP-1", slug="s", title="T", manager_effort="xhigh")
        assert e.manager_effort == "xhigh"

    def test_accepts_max(self) -> None:
        from yukar.models.epic import Epic

        e = Epic(id="EP-1", slug="s", title="T", manager_effort="max")
        assert e.manager_effort == "max"

    def test_invalid_effort_raises(self) -> None:
        from pydantic import ValidationError

        from yukar.models.epic import Epic

        with pytest.raises(ValidationError):
            Epic(id="EP-1", slug="s", title="T", manager_effort="low")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    def test_old_epic_yaml_without_field_loads_as_high(self) -> None:
        """Existing epic.yaml files that lack manager_effort should default to 'high'."""
        from yukar.models.epic import Epic

        # Simulate loading an old epic.yaml that has no manager_effort key
        raw = {
            "id": "EP-1",
            "slug": "s",
            "title": "T",
            "status": "planned",
        }
        e = Epic.model_validate(raw)
        assert e.manager_effort == "high"
