"""Tests for the configurable LLM request (read) timeout.

High reasoning effort (thinking=adaptive at xhigh/max) can leave the streaming
socket idle for minutes between chunks. The strands Bedrock default of 120s and
the Anthropic SDK default surface as a connection/read timeout mid-run, so the
timeout is configurable via LLMSettings.request_timeout and wired into both
provider clients in create_model().

Covers:
1. LLMSettings: request_timeout defaults to 900 and rejects values < 1.
2. bedrock: boto client read_timeout reflects request_timeout (default + custom).
3. bedrock: connect_timeout stays bounded (not raised to request_timeout).
4. bedrock + prompt_caching: read_timeout still applied.
5. anthropic: client timeout reflects request_timeout (default + custom).
6. anthropic + prompt_caching: client timeout still applied.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# 1. Settings field
# ---------------------------------------------------------------------------


class TestRequestTimeoutSetting:
    def test_default_is_900(self) -> None:
        from yukar.config.settings import LLMSettings

        assert LLMSettings().request_timeout == 900

    def test_custom_value_accepted(self) -> None:
        from yukar.config.settings import LLMSettings

        assert LLMSettings(request_timeout=1800).request_timeout == 1800

    def test_zero_rejected(self) -> None:
        from pydantic import ValidationError

        from yukar.config.settings import LLMSettings

        with pytest.raises(ValidationError):
            LLMSettings(request_timeout=0)


# ---------------------------------------------------------------------------
# 2-4. Bedrock
# ---------------------------------------------------------------------------


class TestBedrockReadTimeout:
    def _make_settings(self, prompt_caching: bool = False, request_timeout: int = 900):  # type: ignore[return]
        from yukar.config.settings import LLMSettings

        return LLMSettings(
            provider="bedrock",
            model_id="anthropic.claude-opus-4",
            prompt_caching=prompt_caching,
            request_timeout=request_timeout,
        )

    def test_default_read_timeout_applied(self) -> None:
        from strands.models.bedrock import BedrockModel

        from yukar.llm.factory import create_model

        model = create_model(self._make_settings())
        assert isinstance(model, BedrockModel)
        # strands default is 120; the factory must override it.
        assert model.client.meta.config.read_timeout == 900

    def test_custom_read_timeout_applied(self) -> None:
        from strands.models.bedrock import BedrockModel

        from yukar.llm.factory import create_model

        model = create_model(self._make_settings(request_timeout=1800), effort="xhigh")
        assert isinstance(model, BedrockModel)
        assert model.client.meta.config.read_timeout == 1800

    def test_connect_timeout_not_inflated(self) -> None:
        """connect_timeout must stay small — only the read timeout is raised."""
        from strands.models.bedrock import BedrockModel

        from yukar.llm.factory import create_model

        model = create_model(self._make_settings(request_timeout=1800))
        assert isinstance(model, BedrockModel)
        assert model.client.meta.config.connect_timeout == 60

    def test_read_timeout_applied_with_caching(self) -> None:
        from strands.models.bedrock import BedrockModel

        from yukar.llm.factory import create_model

        model = create_model(
            self._make_settings(prompt_caching=True, request_timeout=1200), effort="max"
        )
        assert isinstance(model, BedrockModel)
        assert model.client.meta.config.read_timeout == 1200


# ---------------------------------------------------------------------------
# 5-6. Anthropic
# ---------------------------------------------------------------------------


class TestAnthropicTimeout:
    def _make_settings(self, prompt_caching: bool = False, request_timeout: int = 900):  # type: ignore[return]
        from yukar.config.settings import LLMSettings

        return LLMSettings(
            provider="anthropic",
            model_id="claude-opus-4-5",
            prompt_caching=prompt_caching,
            request_timeout=request_timeout,
        )

    def test_default_timeout_applied(self) -> None:
        from strands.models.anthropic import AnthropicModel

        from yukar.llm.factory import create_model

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            model = create_model(self._make_settings())
        assert isinstance(model, AnthropicModel)
        assert model.client.timeout == 900.0

    def test_custom_timeout_applied(self) -> None:
        from strands.models.anthropic import AnthropicModel

        from yukar.llm.factory import create_model

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            model = create_model(self._make_settings(request_timeout=1500), effort="xhigh")
        assert isinstance(model, AnthropicModel)
        assert model.client.timeout == 1500.0

    def test_timeout_applied_with_caching(self) -> None:
        from strands.models.anthropic import AnthropicModel

        from yukar.llm.factory import create_model

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            model = create_model(
                self._make_settings(prompt_caching=True, request_timeout=1500), effort="max"
            )
        assert isinstance(model, AnthropicModel)
        assert model.client.timeout == 1500.0
