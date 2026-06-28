"""Global settings model — maps spec §4.2 settings.yaml."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class McpSettings(BaseModel):
    """MCP allowlist policy — controls which MCP servers agents may register/connect.

    Enforcement has two layers:

    1. **Registration gate** (``write_mcp_server`` tool): when
       ``allow_agent_registration`` is ``False`` (the default), the Manager
       agent cannot add any MCP server to ``mcp.yaml``.  When ``True``, the
       server URL/command must still match the corresponding allowlist; an empty
       allowlist always rejects (fail-closed — the operator must explicitly
       enumerate permitted hosts/commands).

    2. **Connection gate** (``McpClientManager._start``): applied when a
       non-empty allowlist is configured.  An *empty* allowlist means
       "no restriction at connection time" so that user-configured MCP servers
       work out of the box without any settings change.  A non-empty allowlist
       enables strict egress control for operators who want it.  With
       ``enforce_connection_allowlist=True`` (strict mode), an empty allowlist
       becomes fail-closed (all connections rejected) rather than unrestricted.
    """

    model_config = ConfigDict(extra="forbid")

    # Default False → agent self-registration is disabled out of the box.
    allow_agent_registration: bool = False

    # SSE host allowlist.  Empty = no restriction at *connection* time.
    # At *registration* time (allow_agent_registration=True): empty = reject all.
    allowed_sse_hosts: list[str] = []

    # stdio command basename allowlist.  Same empty-means-different-things rule
    # as allowed_sse_hosts (see class docstring).
    allowed_stdio_commands: list[str] = []

    # Strict connection gate.  When True, an empty allowlist is fail-closed:
    # any server whose type has no allowlist entries is rejected at connection
    # time.  Default False preserves backward-compatible "empty = no restriction"
    # behaviour.  True: empty allowed_sse_hosts / allowed_stdio_commands → all
    # SSE / stdio connections blocked regardless of what is configured in mcp.yaml.
    enforce_connection_allowlist: bool = False


class LLMRoleSettings(BaseModel):
    """Per-role model override.  Any field left as None falls back to the global value."""

    model_config = ConfigDict(extra="forbid")
    model_id: str | None = None


class LLMRolesSettings(BaseModel):
    """Optional per-role model overrides (manager / worker / evaluator / arbiter)."""

    model_config = ConfigDict(extra="forbid")
    manager: LLMRoleSettings = Field(default_factory=LLMRoleSettings)
    worker: LLMRoleSettings = Field(default_factory=LLMRoleSettings)
    evaluator: LLMRoleSettings = Field(default_factory=LLMRoleSettings)
    arbiter: LLMRoleSettings = Field(default_factory=LLMRoleSettings)


class ConversationSummarySettings(BaseModel):
    """Conversation history summarisation settings (SummarizingConversationManager)."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    summary_ratio: float = Field(default=0.3, ge=0.1, le=0.8)
    preserve_recent_messages: int = Field(default=10, ge=1)
    # If context utilisation exceeds this ratio (0, 1], compress proactively before the model call.
    # None means reactive (summarise only on overflow).
    proactive_compression_threshold: float | None = Field(default=None, gt=0.0, le=1.0)


# ---------------------------------------------------------------------------
# api_key_env validator helpers
# ---------------------------------------------------------------------------

# Valid env-var identifier: starts with a letter or underscore, followed by
# letters, digits, or underscores.  Matches the POSIX shell variable-name rule.
_ENV_VAR_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Exact names that are high-value foreign credentials.  Receiving one of these
# from a PUT /api/settings would let a compromised agent redirect the API key
# to e.g. an AWS credential and exfiltrate it to api.anthropic.com.
_SECRET_ENV_EXACT: frozenset[str] = frozenset(
    {
        "AWS_SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITLAB_TOKEN",
        "SSH_AUTH_SOCK",
        "GOOGLE_APPLICATION_CREDENTIALS",
    }
)

# Prefix patterns for well-known cloud/infrastructure credential namespaces.
_SECRET_ENV_PREFIXES: tuple[str, ...] = ("AWS_", "AZURE_", "GCP_")

# Substring patterns — any var whose name contains one of these is also blocked.
_SECRET_ENV_SUBSTRINGS: tuple[str, ...] = ("SECRET", "PASSWORD", "PRIVATE_KEY")


def _is_forbidden_api_key_env(name: str) -> bool:
    """Return True if *name* refers to a high-value foreign credential.

    Allows ``ANTHROPIC_API_KEY`` and any generic custom name (``MY_LLM_KEY``).
    Rejects:
    - exact denylist (AWS_SECRET_ACCESS_KEY, GITHUB_TOKEN, …)
    - AWS_* / AZURE_* / GCP_* prefix patterns
    - any name containing SECRET, PASSWORD, or PRIVATE_KEY

    Matching is case-insensitive so that mixed-case variants like
    ``aws_secret_access_key`` or ``My_Secret_Token`` are also rejected.
    """
    upper = name.upper()
    if upper in _SECRET_ENV_EXACT:
        return True
    if any(upper.startswith(p) for p in _SECRET_ENV_PREFIXES):
        return True
    return any(sub in upper for sub in _SECRET_ENV_SUBSTRINGS)


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["bedrock", "anthropic", "fake"] = "bedrock"
    model_id: str = "us.anthropic.claude-sonnet-4-6-20251201-v1:0"
    # Anthropic-specific fields (ignored for bedrock / fake).
    api_key_env: str | None = None  # env var name that holds ANTHROPIC_API_KEY

    @field_validator("api_key_env", mode="before")
    @classmethod
    def validate_api_key_env(cls, v: object) -> object:
        """Validate that api_key_env is a safe env-var name.

        Allows None and valid identifier-shaped names that do not refer to
        high-value foreign credentials (AWS_*, AZURE_*, GCP_*, and names
        containing SECRET/PASSWORD/PRIVATE_KEY).
        ANTHROPIC_API_KEY and custom names like MY_KEY are explicitly permitted.
        """
        if v is None:
            return v
        if not isinstance(v, str):
            raise ValueError(f"api_key_env must be a string or None, got {type(v).__name__!r}")
        if not _ENV_VAR_IDENT_RE.match(v):
            raise ValueError(
                f"api_key_env {v!r} is not a valid env-var identifier "
                "(must match ^[A-Za-z_][A-Za-z0-9_]*$)"
            )
        if _is_forbidden_api_key_env(v):
            raise ValueError(
                f"api_key_env {v!r} refers to a high-value credential and is not permitted; "
                "use a dedicated ANTHROPIC_API_KEY or a custom name like MY_LLM_KEY"
            )
        return v
    max_tokens: int = 8192
    # Enable Anthropic prompt caching (ignored for bedrock / fake).
    prompt_caching: bool = True
    # Per-role overrides — any unset field falls back to global model_id above.
    roles: LLMRolesSettings = Field(default_factory=LLMRolesSettings)
    # Conversation history summarisation settings.
    summarization: ConversationSummarySettings = Field(default_factory=ConversationSummarySettings)


class EmbeddingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["bedrock", "fake"] = "bedrock"
    model_id: str = "amazon.titan-embed-text-v2:0"
    # None → boto3 standard region resolution (AWS_REGION env / profile).
    region: str | None = None
    # None → omit from request body (preserve existing index compatibility).
    # Set to e.g. 1024 to pass dimensions+normalize=true to Titan v2.
    dimensions: int | None = None


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_parallel_epics: int = Field(default=2, ge=1)
    max_parallel_workers: int = Field(default=4, ge=1)
    worker_max_turns: int = Field(default=60, ge=1)
    evaluator_max_turns: int = Field(default=20, ge=1)
    worker_max_total_tokens: int | None = None
    evaluator_max_total_tokens: int | None = None


class UsageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fetch_exchange_rate: bool = True


class GitSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    author_name: str = "yukar"
    author_email: str = "yukar@localhost"


class IndexerSettings(BaseModel):
    """Indexer / watcher configuration."""

    model_config = ConfigDict(extra="forbid")
    watch: bool = True  # Enable filesystem watcher for automatic re-indexing


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_root: str = "~/yukar-projects"
    llm: LLMSettings = Field(default_factory=LLMSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    git: GitSettings = Field(default_factory=GitSettings)
    indexer: IndexerSettings = Field(default_factory=IndexerSettings)
    usage: UsageSettings = Field(default_factory=UsageSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
