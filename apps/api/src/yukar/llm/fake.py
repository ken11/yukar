"""FakeModel — deterministic scripted model for tests and local smoke runs.

Usage
-----
Inject a script (list of turns) before constructing the Agent::

    from yukar.llm.fake import (
        FakeModel, TextTurn, ToolUseTurn, MessageTurn, TextBlock, ToolUseBlock,
    )
    import uuid

    model = FakeModel(script=[
        TextTurn("Hello from the fake model"),
        ToolUseTurn(tool_name="fs_read", tool_input={"path": "README.md"}),
        # Mixed text + tool in one assistant message (same contentBlockIndex sequence):
        MessageTurn(blocks=[
            TextBlock(text="Reading the file now"),
            ToolUseBlock(tool_name="fs_read", tool_input={"path": "README.md"}),
        ]),
        TextTurn("Done"),
    ])
    agent = Agent(model=model, tools=[...])

Each ``TextTurn`` becomes a plain assistant text response.
Each ``ToolUseTurn`` becomes a tool_use request; the agent's tool executor will
call the real tool and pass the result back, then the model advances to the next
turn in the script.
Each ``MessageTurn`` emits multiple content blocks (text and/or tool_use) within
a single assistant message, with contentBlockIndex incrementing across blocks.

Streaming events emitted
------------------------
Per turn the model emits the minimal set of events that Strands' event_loop
expects::

    messageStart  → contentBlockStart (text|toolUse) → contentBlockDelta(s)
    → contentBlockStop → messageStop → metadata

The event shapes match Bedrock / ``strands.types.streaming.StreamEvent``.

Environment variable
--------------------
``YUKAR_FAKE_SCRIPT`` can be set to a JSON string that overrides the script
at factory.create_model() time (format: list of {"type": "text"|"tool_use"|"raise"|"message",
"text": "...", "tool_name": "...", "tool_input": {...}, "stop_reason": "...",
"usage": {...}, "chunk_input": bool, "raw_input_json": "...",
"exc_name": "...", "message": "...",
"blocks": [{"type": "text"|"tool_use", ...}]}).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncGenerator, AsyncIterable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from strands.models.model import Model
from strands.types.content import Messages, SystemContentBlock
from strands.types.event_loop import Usage
from strands.types.exceptions import (
    ContextWindowOverflowException,
    MaxTokensReachedException,
    ModelThrottledException,
)
from strands.types.streaming import StopReason, StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

# ---------------------------------------------------------------------------
# Per-role invocation counters (for per_call script dispatch)
# ---------------------------------------------------------------------------

#: Tracks how many times ``FakeModel.from_env(role=...)`` has been called for
#: each role when that role's script uses the ``per_call`` form.  Tests must
#: call ``reset_call_counts()`` between cases (conftest autouse fixture does
#: this automatically).
_role_invocation_counts: dict[str, int] = {}


def reset_call_counts() -> None:
    """Clear all per-role invocation counters.

    Call this between test cases so that ``per_call`` script indices do not
    bleed from one test into the next.  The autouse fixture in conftest.py
    invokes this automatically.
    """
    _role_invocation_counts.clear()


# ---------------------------------------------------------------------------
# Streaming chunk configuration
# ---------------------------------------------------------------------------

#: Approximate chunk size (chars) for splitting text deltas.
_CHUNK_SIZE: int = 12


def _chunk_sleep() -> float:
    """Return the per-chunk sleep delay in seconds.

    Reads ``YUKAR_FAKE_SLEEP`` from the environment (default ``"0.02"``).
    Set to ``"0"`` in tests to emit chunks instantly without adding latency.
    """
    raw = os.environ.get("YUKAR_FAKE_SLEEP", "0.02")
    try:
        return float(raw)
    except ValueError:
        return 0.02


def _split_chunks(text: str, size: int = _CHUNK_SIZE) -> list[str]:
    """Split *text* into chunks of at most *size* characters.

    The split tries to honour word boundaries (spaces) to produce more
    natural-looking streaming.  If a run between spaces is longer than
    *size*, it is hard-split at *size*.

    Args:
        text: The full text string to split.
        size: Maximum characters per chunk (must be >= 1).

    Returns:
        A non-empty list of non-empty strings whose concatenation equals
        *text*.  A single-element list is returned when *text* is empty or
        shorter than *size*.
    """
    if not text or len(text) <= size:
        return [text] if text else [""]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= size:
            chunks.append(remaining)
            break
        # Try to split at a word boundary (last space within the window).
        window = remaining[: size + 1]
        split_pos = window.rfind(" ")
        if split_pos > 0:
            # Include the trailing space in the current chunk so that the
            # reconstructed text is identical to the original.
            chunks.append(remaining[: split_pos + 1])
            remaining = remaining[split_pos + 1 :]
        else:
            # No space found — hard-split at size.
            chunks.append(remaining[:size])
            remaining = remaining[size:]
    return chunks


# ---------------------------------------------------------------------------
# Content block types (for MessageTurn)
# ---------------------------------------------------------------------------


@dataclass
class TextBlock:
    """A text content block within a :class:`MessageTurn`."""

    text: str


@dataclass
class ToolUseBlock:
    """A tool_use content block within a :class:`MessageTurn`."""

    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_use_id: str | None = None  # auto-generated if None
    chunk_input: bool = False
    raw_input_json: str | None = None


# ---------------------------------------------------------------------------
# Script turn types
# ---------------------------------------------------------------------------


@dataclass
class TextTurn:
    """A scripted assistant text response."""

    text: str
    stop_reason: StopReason | None = None
    usage: Usage | None = None


@dataclass
class ToolUseTurn:
    """A scripted tool_use request."""

    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_use_id: str | None = None  # auto-generated if None
    stop_reason: StopReason | None = None
    usage: Usage | None = None
    chunk_input: bool = False
    raw_input_json: str | None = None


@dataclass
class RaiseTurn:
    """A scripted exception injection turn.

    When this turn is reached, the specified Strands exception is raised
    instead of emitting stream events.  Useful for testing error-handling
    paths in the orchestrator and event loop.

    Supported ``exc_name`` values:
        - ``"MaxTokensReachedException"``
        - ``"ContextWindowOverflowException"``
        - ``"ModelThrottledException"``

    Unknown values raise :class:`ValueError`.
    """

    exc_name: str
    message: str = ""


@dataclass
class MessageTurn:
    """A scripted assistant message containing multiple content blocks.

    Each element of ``blocks`` is emitted as a separate content block within
    a single assistant message, with ``contentBlockIndex`` incrementing from 0.
    This faithfully mimics Bedrock's behaviour when a model response contains
    both text and tool_use blocks inside the same message.

    The ``stopReason`` in ``messageStop`` defaults to ``"tool_use"`` when at
    least one :class:`ToolUseBlock` is present, otherwise ``"end_turn"``.

    Args:
        blocks: Ordered list of :class:`TextBlock` or :class:`ToolUseBlock`
                instances to emit within this turn.
        stop_reason: Override the ``stopReason`` in ``messageStop``.
        usage: Override the usage metadata (Bedrock camelCase dict).
    """

    blocks: list[TextBlock | ToolUseBlock]
    stop_reason: StopReason | None = None
    usage: Usage | None = None

    def __post_init__(self) -> None:
        # A real assistant message always has at least one content block; an
        # empty MessageTurn would produce a blank message Strands has to backfill.
        if not self.blocks:
            raise ValueError("MessageTurn requires at least one block")


ScriptTurn = TextTurn | ToolUseTurn | RaiseTurn | MessageTurn

#: Mapping from exc_name string to Strands exception class.
_EXC_MAP: dict[str, type[Exception]] = {
    "MaxTokensReachedException": MaxTokensReachedException,
    "ContextWindowOverflowException": ContextWindowOverflowException,
    "ModelThrottledException": ModelThrottledException,
}

#: Default zero usage block (Bedrock camelCase).
_ZERO_USAGE: Usage = {
    "inputTokens": 0,
    "outputTokens": 0,
    "totalTokens": 0,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_block(raw: dict[str, Any]) -> TextBlock | ToolUseBlock:
    """Parse a single content block dict into a :class:`TextBlock` or :class:`ToolUseBlock`.

    Args:
        raw: Dict with a ``"type"`` key of ``"text"`` or ``"tool_use"``.

    Returns:
        A :class:`TextBlock` or :class:`ToolUseBlock` instance.

    Raises:
        ValueError: If ``"type"`` is not ``"text"`` or ``"tool_use"``.
    """
    bt = raw.get("type")
    if bt == "text":
        return TextBlock(text=raw["text"])
    if bt == "tool_use":
        return ToolUseBlock(
            tool_name=raw["tool_name"],
            tool_input=raw.get("tool_input", {}),
            tool_use_id=raw.get("tool_use_id"),
            chunk_input=raw.get("chunk_input", False),
            raw_input_json=raw.get("raw_input_json"),
        )
    raise ValueError(f"Unknown block type in MessageTurn: {bt!r}")


def _parse_turns(items: list[dict[str, Any]]) -> list[ScriptTurn]:
    """Parse a list of raw turn dicts into :class:`ScriptTurn` objects.

    Args:
        items: Sequence of dicts, each with a ``"type"`` key of ``"text"``,
               ``"tool_use"``, ``"raise"``, or ``"message"``.

    Returns:
        List of :class:`TextTurn`, :class:`ToolUseTurn`, :class:`RaiseTurn`,
        or :class:`MessageTurn` instances.

    Raises:
        ValueError: If a dict has an unknown ``"type"`` value.
    """
    turns: list[ScriptTurn] = []
    for item in items:
        t = item.get("type")
        if t == "text":
            raw_usage = item.get("usage")
            turns.append(
                TextTurn(
                    text=item["text"],
                    stop_reason=cast(StopReason, item["stop_reason"])
                    if "stop_reason" in item
                    else None,
                    usage=cast(Usage, raw_usage) if raw_usage is not None else None,
                )
            )
        elif t == "tool_use":
            raw_usage = item.get("usage")
            turns.append(
                ToolUseTurn(
                    tool_name=item["tool_name"],
                    tool_input=item.get("tool_input", {}),
                    tool_use_id=item.get("tool_use_id"),
                    stop_reason=cast(StopReason, item["stop_reason"])
                    if "stop_reason" in item
                    else None,
                    usage=cast(Usage, raw_usage) if raw_usage is not None else None,
                    chunk_input=item.get("chunk_input", False),
                    raw_input_json=item.get("raw_input_json"),
                )
            )
        elif t == "raise":
            turns.append(
                RaiseTurn(
                    exc_name=item["exc_name"],
                    message=item.get("message", ""),
                )
            )
        elif t == "message":
            raw_usage = item.get("usage")
            raw_blocks: list[dict[str, Any]] = item.get("blocks", [])
            turns.append(
                MessageTurn(
                    blocks=[_parse_block(b) for b in raw_blocks],
                    stop_reason=cast(StopReason, item["stop_reason"])
                    if "stop_reason" in item
                    else None,
                    usage=cast(Usage, raw_usage) if raw_usage is not None else None,
                )
            )
        else:
            raise ValueError(f"Unknown script turn type: {t!r}")
    return turns


# ---------------------------------------------------------------------------
# FakeModel
# ---------------------------------------------------------------------------


class FakeModel(Model):
    """Deterministic scripted Strands Model.

    Replays ``script`` one turn per invocation of ``stream()``.  When the
    script is exhausted, every subsequent call returns a plain "Script
    exhausted." text response.

    The model config contains only the script so that ``get_config`` /
    ``update_config`` satisfy the abstract interface.
    """

    def __init__(
        self,
        script: Sequence[ScriptTurn] | None = None,
        *,
        model_id: str | None = None,
    ) -> None:
        self._script: list[ScriptTurn] = list(script or [])
        self._index: int = 0
        self._config: dict[str, Any] = {}
        if model_id:
            self._config["model_id"] = model_id

    # ------------------------------------------------------------------
    # Model abstract interface
    # ------------------------------------------------------------------

    def update_config(self, **model_config: Any) -> None:
        self._config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return dict(self._config)

    async def structured_output(
        self,
        output_model: type[Any],
        prompt: Messages,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any]]:
        """Not implemented; yields an empty dict for API compliance."""
        yield {}  # type: ignore[misc]

    def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
        invocation_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        """Return an async generator that replays the current script turn."""
        turn = self._next_turn()
        return self._emit(turn)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_turn(self) -> ScriptTurn:
        if self._index < len(self._script):
            turn = self._script[self._index]
            self._index += 1
            return turn
        return TextTurn("Script exhausted.")

    async def _emit_text_block(
        self,
        text: str,
        block_index: int,
    ) -> AsyncGenerator[StreamEvent]:
        """Yield contentBlockStart + deltas + contentBlockStop for a text block.

        Args:
            text: The text string to stream as deltas.
            block_index: The ``contentBlockIndex`` to use for all events of this block.
        """
        yield {"contentBlockStart": {"contentBlockIndex": block_index, "start": {}}}
        sleep_secs = _chunk_sleep()
        chunks = _split_chunks(text)
        for i, chunk in enumerate(chunks):
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": block_index,
                    "delta": {"text": chunk},
                }
            }
            if sleep_secs > 0 and i < len(chunks) - 1:
                await asyncio.sleep(sleep_secs)
        yield {"contentBlockStop": {"contentBlockIndex": block_index}}

    async def _emit_tool_use_block(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        block_index: int,
        tool_use_id: str | None = None,
        chunk_input: bool = False,
        raw_input_json: str | None = None,
    ) -> AsyncGenerator[StreamEvent]:
        """Yield contentBlockStart + input delta(s) + contentBlockStop for a toolUse block.

        Args:
            tool_name: The tool name to embed in the ``toolUse`` start event.
            tool_input: The tool input dict (serialised to JSON for deltas).
            block_index: The ``contentBlockIndex`` to use for all events of this block.
            tool_use_id: Explicit tool-use id; auto-generated when ``None``.
            chunk_input: When ``True``, split the serialised JSON into multiple deltas.
            raw_input_json: When set, emit this raw string verbatim instead of
                serialising ``tool_input`` (overrides ``chunk_input``).
        """
        resolved_id = tool_use_id or f"fake-{uuid.uuid4().hex[:8]}"
        yield {
            "contentBlockStart": {
                "contentBlockIndex": block_index,
                "start": {
                    "toolUse": {
                        "name": tool_name,
                        "toolUseId": resolved_id,
                    }
                },
            }
        }

        if raw_input_json is not None:
            # Emit the raw string as-is (may be malformed JSON for testing).
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": block_index,
                    "delta": {"toolUse": {"input": raw_input_json}},
                }
            }
        elif chunk_input:
            # Split the JSON-serialised input into multiple deltas.
            input_json = json.dumps(tool_input)
            input_chunks = _split_chunks(input_json)
            for chunk in input_chunks:
                yield {
                    "contentBlockDelta": {
                        "contentBlockIndex": block_index,
                        "delta": {"toolUse": {"input": chunk}},
                    }
                }
        else:
            # Default: single JSON delta (original behaviour).
            input_json = json.dumps(tool_input)
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": block_index,
                    "delta": {"toolUse": {"input": input_json}},
                }
            }

        yield {"contentBlockStop": {"contentBlockIndex": block_index}}

    async def _emit(self, turn: ScriptTurn) -> AsyncGenerator[StreamEvent]:
        """Yield the minimal StreamEvent sequence for one turn."""
        # Handle exception injection first — no stream events are emitted.
        if isinstance(turn, RaiseTurn):
            exc_cls = _EXC_MAP.get(turn.exc_name)
            if exc_cls is None:
                raise ValueError(
                    f"Unknown exc_name for RaiseTurn: {turn.exc_name!r}. "
                    f"Supported: {sorted(_EXC_MAP)}"
                )
            raise exc_cls(turn.message)

        # -- messageStart --
        yield {"messageStart": {"role": "assistant"}}

        if isinstance(turn, TextTurn):
            # Single text block at index 0 (preserves original byte-level emit order).
            async for ev in self._emit_text_block(turn.text, block_index=0):
                yield ev
            stop_reason = turn.stop_reason if turn.stop_reason is not None else "end_turn"

        elif isinstance(turn, ToolUseTurn):
            # Single tool_use block at index 0 (preserves original byte-level emit order).
            async for ev in self._emit_tool_use_block(
                tool_name=turn.tool_name,
                tool_input=turn.tool_input,
                block_index=0,
                tool_use_id=turn.tool_use_id,
                chunk_input=turn.chunk_input,
                raw_input_json=turn.raw_input_json,
            ):
                yield ev
            stop_reason = turn.stop_reason if turn.stop_reason is not None else "end_turn"

        else:  # MessageTurn — multiple blocks with increasing contentBlockIndex.
            has_tool_use = any(isinstance(b, ToolUseBlock) for b in turn.blocks)
            for block_index, block in enumerate(turn.blocks):
                if isinstance(block, TextBlock):
                    async for ev in self._emit_text_block(block.text, block_index=block_index):
                        yield ev
                else:  # ToolUseBlock
                    async for ev in self._emit_tool_use_block(
                        tool_name=block.tool_name,
                        tool_input=block.tool_input,
                        block_index=block_index,
                        tool_use_id=block.tool_use_id,
                        chunk_input=block.chunk_input,
                        raw_input_json=block.raw_input_json,
                    ):
                        yield ev
            if turn.stop_reason is not None:
                stop_reason = turn.stop_reason
            elif has_tool_use:
                stop_reason = "tool_use"
            else:
                stop_reason = "end_turn"

        # -- messageStop + metadata --
        yield {"messageStop": {"stopReason": stop_reason}}  # type: ignore[typeddict-item]
        # Copy the zero-usage default so each emit yields a fresh dict (matches the
        # original per-turn literal and avoids a shared mutable singleton).
        final_usage: Usage = (
            turn.usage if turn.usage is not None else cast(Usage, dict(_ZERO_USAGE))
        )
        yield {  # type: ignore[misc]
            "metadata": {
                "usage": final_usage,
                "metrics": {"latencyMs": 0},
            }
        }

    # ------------------------------------------------------------------
    # Script helpers
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset the script cursor to the beginning."""
        self._index = 0

    @staticmethod
    def from_env(role: str | None = None, *, model_id: str | None = None) -> FakeModel:
        """Build a FakeModel from the YUKAR_FAKE_SCRIPT environment variable.

        ``YUKAR_FAKE_SCRIPT`` may be either:

        1. **JSON array** — used as the script for *all* roles (legacy / simple)::

            [
              {"type": "text", "text": "Hello"},
              {"type": "tool_use", "tool_name": "fs_read", "tool_input": {"path": "x"}},
              {"type": "raise", "exc_name": "ModelThrottledException", "message": "throttled"}
            ]

        2. **JSON object** with per-role arrays — each role receives its own
           independent script copy.  Unknown or missing roles get an empty
           script (the model will always return "Script exhausted.")::

            {
              "manager":   [{"type": "text", "text": "Plan done."}],
              "worker":    [{"type": "tool_use", "tool_name": "fs_write", ...}],
              "evaluator": [{"type": "tool_use", "tool_name": "submit_verdict", ...}]
            }

        Each call to ``from_env`` returns a **new** ``FakeModel`` instance whose
        cursor starts at turn 0, so multiple agents sharing the same role each
        get an independent copy of the script starting from the beginning.

        Supported turn keys (in addition to legacy ``type``/``text``/``tool_name``/
        ``tool_input``/``tool_use_id``):

        - ``stop_reason`` (str): Override the ``stopReason`` in ``messageStop``.
        - ``usage`` (dict): Non-zero Bedrock camelCase usage dict (e.g.
          ``{"inputTokens": 10, "outputTokens": 5, "totalTokens": 15,
          "cacheReadInputTokens": 3}``).
        - ``chunk_input`` (bool, tool_use only): Split tool input JSON into
          multiple ``contentBlockDelta`` events.
        - ``raw_input_json`` (str, tool_use only): Emit this raw string as the
          single tool input delta instead of serialising ``tool_input``.
        - For ``type: "raise"``: ``exc_name`` (str) and ``message`` (str).

        Args:
            role: Agent role string (``"manager"``, ``"worker"``, ``"evaluator"``).
                  Ignored when the env var contains a plain array.

        Returns:
            A new :class:`FakeModel` initialised with the resolved script.

        Raises:
            ValueError: If a turn object has an unknown ``"type"`` value.
            json.JSONDecodeError: If ``YUKAR_FAKE_SCRIPT`` is not valid JSON.
        """
        raw = os.environ.get("YUKAR_FAKE_SCRIPT", "[]")
        parsed: list[dict[str, Any]] | dict[str, Any] = json.loads(raw)

        if isinstance(parsed, list):
            # Legacy / role-agnostic: same script for all roles.
            items: list[dict[str, Any]] = parsed
        elif isinstance(parsed, dict):
            # Role-based: look up the role's value; fall back to empty list.
            role_key = role or ""
            value: Any = parsed.get(role_key, []) if role is not None else []

            if isinstance(value, dict) and "per_call" in value:
                # per_call form: value == {"per_call": [[turn,...], [turn,...], ...]}
                per_call: list[Any] = value["per_call"]
                if not isinstance(per_call, list):
                    raise ValueError(
                        f"per_call for role {role_key!r} must be a list of turn arrays, "
                        f"got {type(per_call).__name__}"
                    )
                # Pick the script for this invocation (clamp to last entry).
                i = _role_invocation_counts.get(role_key, 0)
                if per_call:
                    chosen: Any = per_call[min(i, len(per_call) - 1)]
                    if not isinstance(chosen, list):
                        raise ValueError(
                            f"per_call[{i}] for role {role_key!r} must be a list of turn dicts, "
                            f"got {type(chosen).__name__}"
                        )
                    items = chosen
                else:
                    items = []
                # Advance the counter for this role.
                _role_invocation_counts[role_key] = i + 1
            else:
                # Plain array (legacy role-based form).
                items = value if isinstance(value, list) else []
        else:
            raise ValueError(
                f"YUKAR_FAKE_SCRIPT must be a JSON array or object, got {type(parsed).__name__}"
            )

        return FakeModel(script=_parse_turns(items), model_id=model_id)


# ---------------------------------------------------------------------------
# Convenience: a no-op ContentBlock list that the event_loop expects when it
# assembles the assistant message from streamed events.  (Not used directly
# here but exported for test helpers.)
# ---------------------------------------------------------------------------

__all__ = [
    "FakeModel",
    "TextBlock",
    "ToolUseBlock",
    "TextTurn",
    "ToolUseTurn",
    "RaiseTurn",
    "MessageTurn",
    "ScriptTurn",
    "reset_call_counts",
]
