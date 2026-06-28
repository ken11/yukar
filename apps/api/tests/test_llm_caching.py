"""Tests for prompt-caching and conversation-manager factory additions.

Covers:
- CachingAnthropicModel: Strands CacheConfig + cachePoint injection, converted by
  the parent AnthropicModel into cache_control ephemeral on the last user message.
- create_model(provider="anthropic") returns CachingAnthropicModel / AnthropicModel
  depending on prompt_caching.
- create_conversation_manager returns SummarizingConversationManager / None
  depending on enabled flag and proactive_compression_threshold.
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# CachingAnthropicModel tests
# ---------------------------------------------------------------------------


class TestCachingAnthropicModel:
    """Unit tests for CachingAnthropicModel (CacheConfig + cachePoint injection)."""

    def _make_model(self, **kwargs: Any):  # type: ignore[return]
        from yukar.llm.anthropic_cache import CachingAnthropicModel

        return CachingAnthropicModel(
            model_id="claude-3-haiku-20240307",
            max_tokens=256,
            client_args={"api_key": "test-key"},
            **kwargs,
        )

    def _minimal_messages(self) -> list[dict[str, Any]]:
        """One user text message."""
        return [{"role": "user", "content": [{"text": "hello"}]}]

    def _minimal_tools(self) -> list[dict[str, Any]]:
        """Two minimal tool specs."""
        return [
            {
                "name": "tool_a",
                "description": "A",
                "inputSchema": {"json": {"type": "object", "properties": {}}},
            },
            {
                "name": "tool_b",
                "description": "B",
                "inputSchema": {"json": {"type": "object", "properties": {}}},
            },
        ]

    def test_default_cache_config_strategy_anthropic(self) -> None:
        model = self._make_model()
        assert model._cache_config.strategy == "anthropic"  # type: ignore[attr-defined]

    # The injected cachePoint is converted by AnthropicModel into cache_control
    # ephemeral on the last user message's last content block.
    def test_cache_control_on_last_user_message(self) -> None:
        model = self._make_model()
        req = model.format_request(
            messages=self._minimal_messages(),
            tool_specs=self._minimal_tools(),
            system_prompt="You are helpful.",
        )
        last_content = req["messages"][-1]["content"]
        # cachePoint is consumed (never emitted as its own block); cache_control
        # lands on the preceding real content block.
        assert last_content[-1].get("cache_control") == {"type": "ephemeral"}
        assert last_content[-1]["type"] == "text"

    # A single message breakpoint already caches tools+system as the request
    # prefix (Anthropic cache ordering tools -> system -> messages), so system
    # stays a plain string and tools carry no separate cache_control.
    def test_system_and_tools_left_unannotated(self) -> None:
        model = self._make_model()
        req = model.format_request(
            messages=self._minimal_messages(),
            tool_specs=self._minimal_tools(),
            system_prompt="You are helpful.",
        )
        assert isinstance(req["system"], str)
        for tool in req["tools"]:
            assert "cache_control" not in tool

    # The breakpoint is placed on the LAST content block of the last user message.
    def test_only_last_content_block_annotated(self) -> None:
        model = self._make_model()
        messages = [
            {"role": "user", "content": [{"text": "first"}]},
            {"role": "user", "content": [{"text": "a"}, {"text": "b"}]},
        ]
        req = model.format_request(messages=messages, system_prompt="sys")
        last_content = req["messages"][-1]["content"]
        assert "cache_control" not in last_content[0]
        assert last_content[1].get("cache_control") == {"type": "ephemeral"}
        # First message must not be annotated.
        assert "cache_control" not in req["messages"][0]["content"][0]

    # The caller's live message list (owned by the Agent / session) is not mutated.
    def test_source_messages_not_mutated(self) -> None:
        model = self._make_model()
        messages = self._minimal_messages()
        before = json.loads(json.dumps(messages))
        model.format_request(messages=messages, system_prompt="sys")
        assert messages == before, "source messages must not be mutated"
        # No cachePoint leaked into the source.
        assert all("cachePoint" not in block for block in messages[0]["content"])

    # CacheConfig.ttl is carried onto the injected cachePoint.
    def test_ttl_forwarded_to_cache_point(self) -> None:
        from strands.models import CacheConfig

        model = self._make_model(cache_config=CacheConfig(strategy="anthropic", ttl="1h"))
        injected = model._with_cache_point(self._minimal_messages())  # type: ignore[attr-defined]
        cache_block = injected[-1]["content"][-1]
        assert cache_block["cachePoint"] == {"type": "default", "ttl": "1h"}

    # Boundary: no messages at all → no injection, no crash.
    def test_no_messages_no_injection(self) -> None:
        model = self._make_model()
        req = model.format_request(messages=[], system_prompt="sys")
        assert req["messages"] == []

    # Boundary: no user message → original list returned unchanged.
    def test_no_user_message_returns_unchanged(self) -> None:
        model = self._make_model()
        messages = [{"role": "assistant", "content": [{"text": "hi"}]}]
        injected = model._with_cache_point(messages)  # type: ignore[attr-defined]
        assert injected is messages

    # Boundary: a user message with empty content is not chosen as the target.
    def test_empty_content_user_message_skipped(self) -> None:
        model = self._make_model()
        messages = [{"role": "user", "content": []}]
        injected = model._with_cache_point(messages)  # type: ignore[attr-defined]
        assert injected is messages

    # Boundary: a user message whose blocks are all non-anchor (e.g. media the
    # parent may skip) is not chosen — guards against an empty formatted block
    # list when the cachePoint is converted. The earlier text message wins.
    def test_anchorless_user_message_not_targeted(self) -> None:
        model = self._make_model()
        image_block = {"image": {"format": "png", "source": {"bytes": b"x"}}}
        messages = [
            {"role": "user", "content": [{"text": "anchor here"}]},
            {"role": "assistant", "content": [{"text": "ok"}]},
            {"role": "user", "content": [image_block]},
        ]
        injected = model._with_cache_point(messages)  # type: ignore[attr-defined]
        # cachePoint lands on the first (text) user message, not the image one.
        assert any("cachePoint" in b for b in injected[0]["content"])
        assert all("cachePoint" not in b for b in injected[2]["content"])

    # Boundary: no anchor block anywhere → no injection, no crash.
    def test_no_anchor_block_returns_unchanged(self) -> None:
        model = self._make_model()
        messages = [{"role": "user", "content": [{"image": {"format": "png", "source": {}}}]}]
        injected = model._with_cache_point(messages)  # type: ignore[attr-defined]
        assert injected is messages

    # format_chunk enriches metadata events with cache token counts.
    # The "metadata" synthetic event is built by AnthropicModel.stream from the
    # final message snapshot (get_final_message().usage.model_dump()).
    def test_format_chunk_metadata_enriched_with_cache_tokens(self) -> None:
        """format_chunk must add cacheReadInputTokens / cacheWriteInputTokens to metadata."""
        model = self._make_model()
        # Simulate the synthetic "metadata" event that AnthropicModel.stream builds
        # from the final message snapshot's usage.model_dump() output.
        metadata_event = {
            "type": "metadata",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 80,
                "cache_creation_input_tokens": 10,
            },
        }
        chunk = model.format_chunk(metadata_event)
        assert isinstance(chunk, dict)
        assert "metadata" in chunk
        usage = chunk["metadata"]["usage"]  # type: ignore[index]
        assert usage.get("cacheReadInputTokens") == 80
        assert usage.get("cacheWriteInputTokens") == 10
        # Parent's keys must be preserved.
        assert "inputTokens" in usage
        assert "outputTokens" in usage

    def test_format_chunk_no_cache_fields_passthrough(self) -> None:
        """format_chunk must not inject keys when raw usage has no cache fields."""
        model = self._make_model()
        metadata_event = {
            "type": "metadata",
            "usage": {
                "input_tokens": 50,
                "output_tokens": 5,
            },
        }
        chunk = model.format_chunk(metadata_event)
        assert isinstance(chunk, dict)
        assert "metadata" in chunk
        usage = chunk["metadata"]["usage"]  # type: ignore[index]
        assert "cacheReadInputTokens" not in usage
        assert "cacheWriteInputTokens" not in usage

    def test_format_chunk_non_metadata_event_passthrough(self) -> None:
        """format_chunk must pass through non-metadata events unchanged."""
        model = self._make_model()
        message_start_event = {"type": "message_start"}
        chunk = model.format_chunk(message_start_event)
        assert isinstance(chunk, dict)
        assert "messageStart" in chunk


# ---------------------------------------------------------------------------
# create_model provider=bedrock tests
# ---------------------------------------------------------------------------


class TestCreateModelBedrock:
    """create_model with provider='bedrock' respects prompt_caching."""

    def test_prompt_caching_true_attaches_cache_config(self) -> None:
        from strands.models import CacheConfig
        from strands.models.bedrock import BedrockModel

        from yukar.config.settings import LLMSettings
        from yukar.llm.factory import create_model

        settings = LLMSettings(
            provider="bedrock",
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            prompt_caching=True,
        )
        model = create_model(settings)
        assert isinstance(model, BedrockModel)
        # BedrockModel stores model config via self.config (a dict).
        cfg = model.config  # type: ignore[attr-defined]
        assert "cache_config" in cfg, "cache_config should be set when prompt_caching=True"
        cc = cfg["cache_config"]
        assert isinstance(cc, CacheConfig)
        # strategy must be "anthropic" (not "auto") so that cachePoint injection
        # is unconditional — "auto" fails on application inference-profile ARNs
        # because they do not contain "claude"/"anthropic" as a substring.
        assert cc.strategy == "anthropic"

    def test_prompt_caching_true_with_arn_model_id_uses_anthropic_strategy(self) -> None:
        """Application inference-profile ARNs must also get strategy='anthropic'.

        strategy='auto' checks for 'claude'/'anthropic' substrings in the model_id
        and silently disables caching for ARN-shaped ids like
        'arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/xxxx'.
        """
        from strands.models import CacheConfig
        from strands.models.bedrock import BedrockModel

        from yukar.config.settings import LLMSettings
        from yukar.llm.factory import create_model

        arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"
        settings = LLMSettings(
            provider="bedrock",
            model_id=arn,
            prompt_caching=True,
        )
        model = create_model(settings)
        assert isinstance(model, BedrockModel)
        cfg = model.config  # type: ignore[attr-defined]
        assert "cache_config" in cfg
        cc = cfg["cache_config"]
        assert isinstance(cc, CacheConfig)
        assert cc.strategy == "anthropic", (
            "ARN model ids must still get strategy='anthropic' so cachePoint is injected"
        )

    def test_prompt_caching_false_no_cache_config(self) -> None:
        from strands.models.bedrock import BedrockModel

        from yukar.config.settings import LLMSettings
        from yukar.llm.factory import create_model

        settings = LLMSettings(
            provider="bedrock",
            model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            prompt_caching=False,
        )
        model = create_model(settings)
        assert isinstance(model, BedrockModel)
        cfg = model.config  # type: ignore[attr-defined]
        assert "cache_config" not in cfg, "cache_config must be absent when prompt_caching=False"


# ---------------------------------------------------------------------------
# create_model provider=anthropic tests
# ---------------------------------------------------------------------------


class TestCreateModelAnthropic:
    """create_model with provider='anthropic' returns the right class."""

    def test_prompt_caching_true_returns_caching_model(self) -> None:
        from yukar.config.settings import LLMSettings
        from yukar.llm.anthropic_cache import CachingAnthropicModel
        from yukar.llm.factory import create_model

        settings = LLMSettings(
            provider="anthropic",
            model_id="claude-3-haiku-20240307",
            prompt_caching=True,
        )
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            model = create_model(settings)
        assert isinstance(model, CachingAnthropicModel)
        assert model._cache_config.strategy == "anthropic"  # type: ignore[attr-defined]

    def test_prompt_caching_false_returns_base_model(self) -> None:
        from strands.models.anthropic import AnthropicModel

        from yukar.config.settings import LLMSettings
        from yukar.llm.anthropic_cache import CachingAnthropicModel
        from yukar.llm.factory import create_model

        settings = LLMSettings(
            provider="anthropic",
            model_id="claude-3-haiku-20240307",
            prompt_caching=False,
        )
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            model = create_model(settings)
        assert isinstance(model, AnthropicModel)
        assert not isinstance(model, CachingAnthropicModel)

    # The base model (caching disabled) injects no cache_control at all.
    def test_base_model_emits_no_cache_control(self) -> None:
        from strands.models.anthropic import AnthropicModel

        model = AnthropicModel(
            model_id="claude-3-haiku-20240307",
            max_tokens=256,
            client_args={"api_key": "test-key"},
        )
        req = model.format_request(
            messages=[{"role": "user", "content": [{"text": "hello"}]}],
            tool_specs=[],
            system_prompt="sys",
        )
        for block in req["messages"][-1]["content"]:
            assert "cache_control" not in block


# ---------------------------------------------------------------------------
# create_conversation_manager tests
# ---------------------------------------------------------------------------


class TestCreateConversationManager:
    """create_conversation_manager returns the right instance."""

    def test_enabled_true_returns_summarizing_manager(self) -> None:
        from strands.agent.conversation_manager import SummarizingConversationManager

        from yukar.config.settings import ConversationSummarySettings, LLMSettings
        from yukar.llm.factory import create_conversation_manager

        settings = LLMSettings(
            provider="fake",
            summarization=ConversationSummarySettings(enabled=True),
        )
        mgr = create_conversation_manager(settings)
        assert isinstance(mgr, SummarizingConversationManager)

    def test_enabled_false_returns_none(self) -> None:
        from yukar.config.settings import ConversationSummarySettings, LLMSettings
        from yukar.llm.factory import create_conversation_manager

        settings = LLMSettings(
            provider="fake",
            summarization=ConversationSummarySettings(enabled=False),
        )
        mgr = create_conversation_manager(settings)
        assert mgr is None

    def test_returns_new_instance_each_call(self) -> None:
        from yukar.config.settings import ConversationSummarySettings, LLMSettings
        from yukar.llm.factory import create_conversation_manager

        settings = LLMSettings(
            provider="fake",
            summarization=ConversationSummarySettings(enabled=True),
        )
        mgr1 = create_conversation_manager(settings)
        mgr2 = create_conversation_manager(settings)
        assert mgr1 is not mgr2, "each call must return a distinct instance"

    def test_proactive_compression_threshold_none(self) -> None:
        """When proactive_compression_threshold is None, _compression_threshold is None."""
        from yukar.config.settings import ConversationSummarySettings, LLMSettings
        from yukar.llm.factory import create_conversation_manager

        settings = LLMSettings(
            provider="fake",
            summarization=ConversationSummarySettings(
                enabled=True,
                proactive_compression_threshold=None,
            ),
        )
        mgr = create_conversation_manager(settings)
        assert mgr is not None
        # Internal attribute set by ConversationManager.__init__
        assert mgr._compression_threshold is None  # type: ignore[union-attr]

    def test_proactive_compression_threshold_set(self) -> None:
        """When proactive_compression_threshold is set, _compression_threshold matches."""
        from yukar.config.settings import ConversationSummarySettings, LLMSettings
        from yukar.llm.factory import create_conversation_manager

        settings = LLMSettings(
            provider="fake",
            summarization=ConversationSummarySettings(
                enabled=True,
                proactive_compression_threshold=0.8,
            ),
        )
        mgr = create_conversation_manager(settings)
        assert mgr is not None
        assert mgr._compression_threshold == pytest.approx(0.8)  # type: ignore[union-attr]

    def test_summary_ratio_and_preserve_recent_messages_forwarded(self) -> None:
        from strands.agent.conversation_manager import SummarizingConversationManager

        from yukar.config.settings import ConversationSummarySettings, LLMSettings
        from yukar.llm.factory import create_conversation_manager

        settings = LLMSettings(
            provider="fake",
            summarization=ConversationSummarySettings(
                enabled=True,
                summary_ratio=0.5,
                preserve_recent_messages=5,
            ),
        )
        raw = create_conversation_manager(settings)
        assert raw is not None
        mgr = raw
        assert isinstance(mgr, SummarizingConversationManager)
        assert mgr.summary_ratio == pytest.approx(0.5)
        assert mgr.preserve_recent_messages == 5
