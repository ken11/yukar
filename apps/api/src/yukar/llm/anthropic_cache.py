"""Anthropic prompt-caching model wrapper driven by Strands ``CacheConfig``.

Strands exposes prompt caching through the :class:`strands.models.CacheConfig`
dataclass and the ``cachePoint`` content-block primitive.  In the installed
Strands version that ``cache_config`` model-config key is consumed by
``BedrockModel`` only — the direct ``AnthropicModel`` provider ignores it.  This
thin subclass closes that gap *using the same mechanism* Bedrock uses for
``strategy="anthropic"`` (``BedrockModel._inject_cache_point``):

1. Inject a Strands ``cachePoint`` block at the end of the last user message.
2. Delegate to ``AnthropicModel.format_request``, whose ``_format_request_messages``
   converts every ``cachePoint`` into Anthropic ``cache_control: {"type":
   "ephemeral"}``.

A single cache breakpoint on the last user message caches the **entire request
prefix** — tool definitions, system prompt, and all prior conversation —
because an Anthropic cache breakpoint covers everything before it in the
``tools → system → messages`` ordering.  As the conversation grows the
breakpoint advances, so every manager turn and every tool-loop round-trip reuses
the cached prefix.
"""

from __future__ import annotations

from typing import Any, cast

from strands.models import CacheConfig
from strands.models.anthropic import AnthropicModel
from strands.types.content import Messages
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

# Content-block keys the parent AnthropicModel formatter always emits as a real
# block (i.e. never skipped like ``cachePoint`` or location-source media).  The
# cachePoint must follow at least one such block, otherwise the parent's
# ``formatted_contents[-1]`` cache_control assignment would hit an empty list.
_ANCHOR_KEYS = ("text", "toolUse", "toolResult", "reasoningContent")


class CachingAnthropicModel(AnthropicModel):
    """``AnthropicModel`` that applies a Strands ``CacheConfig`` via ``cachePoint``.

    Args mirror ``AnthropicModel`` plus a keyword-only ``cache_config``.  When
    omitted, ``CacheConfig(strategy="anthropic")`` is used (the only strategy the
    direct Anthropic provider can honour — ``"auto"`` model-support detection is a
    Bedrock-only concept).

    Note: the ``AnthropicModel`` message formatter maps any ``cachePoint`` to a
    plain ``cache_control: {"type": "ephemeral"}`` and ignores the cache point's
    ``ttl`` (only Bedrock forwards TTLs), so ``CacheConfig.ttl`` has no effect for
    this provider; it is accepted for API parity.
    """

    def __init__(
        self,
        *,
        cache_config: CacheConfig | None = None,
        client_args: dict[str, Any] | None = None,
        **model_config: Any,
    ) -> None:
        """Initialise the caching model.

        Args:
            cache_config: Strands prompt-cache configuration.  Defaults to
                ``CacheConfig(strategy="anthropic")``.
            client_args: Passed through to ``AnthropicModel.__init__``.
            **model_config: Passed through to ``AnthropicModel.__init__``.
                Must not contain ``cache_config`` (not an ``AnthropicConfig`` key).
        """
        super().__init__(client_args=client_args, **model_config)
        self._cache_config: CacheConfig = (
            cache_config if cache_config is not None else CacheConfig(strategy="anthropic")
        )

    def format_request(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        tool_choice: ToolChoice | None = None,
    ) -> dict[str, Any]:
        """Inject a ``cachePoint`` then delegate to ``AnthropicModel.format_request``.

        Args:
            messages: List of message objects to be processed by the model.
            tool_specs: List of tool specifications to make available.
            system_prompt: System prompt to provide context to the model.
            tool_choice: Selection strategy for tool invocation.

        Returns:
            The Anthropic streaming request dict, with the cache breakpoint
            applied by the parent via the injected ``cachePoint``.
        """
        cached = self._with_cache_point(messages)
        return super().format_request(cached, tool_specs, system_prompt, tool_choice)

    def format_chunk(self, event: dict[str, Any]) -> StreamEvent:
        """Delegate to the parent then inject cache token counts into metadata events.

        Strands' ``AnthropicModel.format_chunk`` handles the ``"metadata"`` event
        type (emitted once per stream from the final message snapshot) but only
        propagates ``inputTokens`` / ``outputTokens`` / ``totalTokens``.  The
        Anthropic API also returns ``cache_read_input_tokens`` and
        ``cache_creation_input_tokens`` in that same ``usage`` dict, but the
        parent discards them.

        This override picks them up from the raw ``event["usage"]`` dict and
        re-attaches them as ``cacheReadInputTokens`` / ``cacheWriteInputTokens``
        (camelCase — the keys Strands ``telemetry/metrics.py`` accumulates when
        it processes the metadata StreamEvent).  Fields are added only when
        present in the raw event; ``totalTokens`` is left as the parent computed
        it.  For all other event types the parent result is returned unchanged.

        Args:
            event: A raw streaming chunk dict from the Anthropic API (or the
                synthetic ``"metadata"`` dict built by ``AnthropicModel.stream``
                from the final message snapshot).

        Returns:
            The Strands ``StreamEvent`` dict, enriched with cache usage keys
            for ``"metadata"`` events that carry cache token counts.
        """
        chunk: StreamEvent = super().format_chunk(event)

        if event.get("type") == "metadata":
            raw_usage: dict[str, Any] = event.get("usage") or {}
            cache_read = raw_usage.get("cache_read_input_tokens")
            cache_write = raw_usage.get("cache_creation_input_tokens")
            if cache_read is not None or cache_write is not None:
                # chunk is {"metadata": {"usage": {...}, "metrics": {...}}}.
                # Shallow-copy at both levels to avoid mutating the parent's dict.
                meta: dict[str, Any] = dict(cast(dict[str, Any], chunk).get("metadata") or {})
                usage: dict[str, Any] = dict(meta.get("usage") or {})
                if cache_read is not None:
                    usage["cacheReadInputTokens"] = int(cache_read)
                if cache_write is not None:
                    usage["cacheWriteInputTokens"] = int(cache_write)
                meta["usage"] = usage
                chunk = cast(StreamEvent, {"metadata": meta})

        return chunk

    def _with_cache_point(self, messages: Messages) -> Messages:
        """Return a copy of ``messages`` with a ``cachePoint`` on the last user message.

        Mirrors ``BedrockModel._inject_cache_point``: the breakpoint goes on the
        last user message that carries an *anchor* block (see ``_ANCHOR_KEYS``).
        An anchor is a content block the parent formatter is guaranteed to emit,
        so the converted ``cache_control`` always attaches to a real block — a
        message of only ``cachePoint`` / location-source blocks (which the parent
        skips) would otherwise leave nothing to anchor onto.

        The caller's message list — the live conversation owned by the Agent /
        session — is never mutated; only the targeted message and its content
        list are copied.

        Args:
            messages: The agent's outgoing messages.

        Returns:
            A new messages list with the cache point injected, or the original
            list unchanged when there is no eligible user message.
        """
        last_user_idx: int | None = None
        for idx, msg in enumerate(messages):
            if msg.get("role") != "user":
                continue
            if any(key in block for block in (msg.get("content") or []) for key in _ANCHOR_KEYS):
                last_user_idx = idx

        if last_user_idx is None:
            return messages

        cache_point: dict[str, Any] = {"type": "default"}
        if self._cache_config.ttl:
            cache_point["ttl"] = self._cache_config.ttl

        new_messages: list[Any] = list(messages)
        target: dict[str, Any] = dict(new_messages[last_user_idx])
        content: list[Any] = [block for block in target["content"] if "cachePoint" not in block]
        content.append({"cachePoint": cache_point})
        target["content"] = content
        new_messages[last_user_idx] = target
        return cast(Messages, new_messages)
