"""LLM model factory.

Creates Strands Model instances from Settings.llm, with optional per-role
model overrides (manager / worker / evaluator).

Design decisions:
- Bedrock and Anthropic clients are initialised lazily inside each call to
  create_model() so that import-time credential checks are avoided entirely.
- The 'anthropic' package is a declared dependency (pyproject.toml), so the
  provider="anthropic" branch can rely on it being importable.  It is still
  imported inside the function body to keep the (heavier) Anthropic / boto3
  client construction off the import path for fake / bedrock runs; the
  ModuleNotFoundError guard is defensive only (e.g. a broken partial install).
- provider="fake" is supported for tests and local smoke runs via FakeModel.
- create_conversation_manager() returns a new ResilientSummarizingConversationManager
  instance per call (the manager holds mutable _summary_message state and must
  not be shared across agents).  ResilientSummarizingConversationManager is a
  thin subclass that tolerates session state persisted by a different class
  (e.g. the old Strands default SlidingWindowConversationManager), preventing
  run_failed crashes on epics created before the summarisation feature landed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from strands.models.model import Model

from yukar.config.settings import LLMRoleSettings, LLMSettings
from yukar.models.roles import AgentRole

if TYPE_CHECKING:
    from strands.agent.conversation_manager import ConversationManager


def create_model(
    settings: LLMSettings,
    role: AgentRole | None = None,
    effort: str | None = None,
) -> Model:
    """Return a Strands Model for the given role (or the global default).

    Args:
        settings: Global LLM settings from config.
        role: Optional agent role.  When provided and the settings define a
              per-role override, that override's model_id is used instead of
              the global model_id.
        effort: Optional reasoning effort level (e.g. "high", "xhigh", "max").
              When provided, extended thinking (thinking=adaptive) is enabled on
              the model.  Provider-specific wiring (``effort`` is nested under
              ``output_config`` for both providers — it is not a top-level
              field of the Messages API, and Bedrock ConverseStream rejects a
              top-level ``effort`` with "Converse Stream effort not permitted"):
                - bedrock: injected via ``additional_request_fields`` as
                  ``{"thinking": {"type": "adaptive"},
                     "output_config": {"effort": effort}}``.
                - anthropic: injected via ``params`` as
                  ``{"thinking": {"type": "adaptive"},
                     "output_config": {"effort": effort}}``.
                - fake: effort is ignored.
              When ``None``, no thinking/effort fields are sent — worker and
              evaluator behaviour is unchanged.

    Returns:
        A configured Strands Model instance.

    Raises:
        ValueError: If the provider is unknown.
        ModuleNotFoundError: If provider="anthropic" but the 'anthropic' package
            is not installed.
    """
    # Resolve effective model_id: role override wins over global.
    model_id = settings.model_id
    if role is not None:
        role_cfg: LLMRoleSettings | None = getattr(settings.roles, role, None)
        if role_cfg is not None and role_cfg.model_id is not None:
            model_id = role_cfg.model_id

    provider = settings.provider

    if provider == "fake":
        from yukar.llm.fake import FakeModel

        return FakeModel.from_env(role, model_id=model_id)

    if provider == "bedrock":
        from strands.models.bedrock import BedrockModel

        extra: dict[str, Any] = {}
        if effort is not None:
            extra["additional_request_fields"] = {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": effort},
            }

        if settings.prompt_caching:
            from strands.models import CacheConfig

            # Use strategy="anthropic" (not "auto") so that cachePoint injection
            # is unconditional.  strategy="auto" checks whether the model_id
            # contains "claude" or "anthropic" as a substring; application
            # inference-profile ARNs (e.g. "arn:aws:bedrock:...:application-
            # inference-profile/xxxx") do not contain those substrings and
            # therefore resolve to None → warning + no cachePoint injected.
            # yukar uses only Claude models on Bedrock, so "anthropic" is always
            # correct regardless of whether the model_id is a plain id or an ARN.
            return BedrockModel(
                model_id=model_id,
                cache_config=CacheConfig(strategy="anthropic"),
                max_tokens=settings.max_tokens,
                **extra,
            )

        return BedrockModel(model_id=model_id, max_tokens=settings.max_tokens, **extra)

    if provider == "anthropic":
        # Lazy import: fails with clear message if 'anthropic' is not installed.
        # CachingAnthropicModel imports AnthropicModel internally, so both are
        # wrapped in the same try/except to surface a helpful message when the
        # 'anthropic' package is absent.
        try:
            from strands.models.anthropic import AnthropicModel

            if settings.prompt_caching:
                from yukar.llm.anthropic_cache import CachingAnthropicModel
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "provider='anthropic' requires the 'anthropic' package. Run: uv add anthropic"
            ) from exc

        import os

        api_key_env = settings.api_key_env or "ANTHROPIC_API_KEY"
        api_key = os.environ.get(api_key_env)
        client_args: dict[str, Any] = {"api_key": api_key} if api_key else {}

        # Mirror the bedrock ``extra`` pattern: a ``dict[str, Any]`` (value type
        # ``Any``) so spreading it as ``**`` keyword args type-checks cleanly.
        anthropic_extra: dict[str, Any] = {}
        if effort is not None:
            anthropic_extra["params"] = {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": effort},
            }

        if settings.prompt_caching:
            from strands.models import CacheConfig

            return CachingAnthropicModel(
                cache_config=CacheConfig(strategy="anthropic"),
                model_id=model_id,
                max_tokens=settings.max_tokens,
                client_args=client_args or None,
                **anthropic_extra,
            )

        return AnthropicModel(
            model_id=model_id,
            max_tokens=settings.max_tokens,
            client_args=client_args or None,
            **anthropic_extra,
        )

    raise ValueError(f"Unknown LLM provider: {provider!r}")


def create_conversation_manager(settings: LLMSettings) -> ConversationManager | None:
    """Return a new ``SummarizingConversationManager`` (or ``None``).

    A new instance is returned on every call because
    ``SummarizingConversationManager`` holds mutable state (``_summary_message``)
    and must not be shared between agents.

    When ``settings.summarization.enabled`` is ``False``, returns ``None`` so
    the caller falls back to Strands' default ``SlidingWindowConversationManager``.

    The ``summarization_agent`` parameter is intentionally omitted so that the
    manager calls the parent agent's model directly — this avoids creating an
    extra ``FileSessionManager``, preserving the invariant that only the
    orchestrator owns a session manager (spec §6.4 / CLAUDE.md).

    Args:
        settings: Global LLM settings from config.

    Returns:
        A freshly constructed ``SummarizingConversationManager``, or ``None``
        when summarisation is disabled.
    """
    from strands.agent.conversation_manager import ProactiveCompressionConfig

    from yukar.llm.conversation import ResilientSummarizingConversationManager

    cfg = settings.summarization
    if not cfg.enabled:
        return None

    proactive: ProactiveCompressionConfig | None = None
    if cfg.proactive_compression_threshold is not None:
        proactive = ProactiveCompressionConfig(
            compression_threshold=cfg.proactive_compression_threshold
        )

    return ResilientSummarizingConversationManager(
        summary_ratio=cfg.summary_ratio,
        preserve_recent_messages=cfg.preserve_recent_messages,
        proactive_compression=proactive,
    )
