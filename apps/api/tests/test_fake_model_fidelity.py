"""Unit tests for FakeModel streaming-contract fidelity.

Verifies new features added to ``yukar.llm.fake``:
  (a) stop_reason override per turn
  (b) non-zero usage dicts (including cache keys)
  (c) chunk_input / raw_input_json for tool-input deltas
  (d) RaiseTurn exception injection
  (e) from_env parsing of all new keys

These tests do NOT use asyncio.sleep delay (YUKAR_FAKE_SLEEP=0).
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from strands.types.event_loop import Usage

from yukar.llm.fake import (
    FakeModel,
    MessageTurn,
    RaiseTurn,
    TextBlock,
    TextTurn,
    ToolUseBlock,
    ToolUseTurn,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def collect_events(model: FakeModel) -> list[Any]:
    """Drive model.stream() once and return the full list of StreamEvents."""
    events: list[Any] = []
    async for event in model.stream([]):  # type: ignore[arg-type]
        events.append(event)
    return events


def get_message_stop(events: list[Any]) -> dict[str, Any]:
    for ev in events:
        if "messageStop" in ev:
            return ev["messageStop"]  # type: ignore[no-any-return]
    raise AssertionError("No messageStop event found")


def get_metadata(events: list[Any]) -> dict[str, Any]:
    for ev in events:
        if "metadata" in ev:
            return ev["metadata"]  # type: ignore[no-any-return]
    raise AssertionError("No metadata event found")


def get_content_deltas(events: list[Any]) -> list[dict[str, Any]]:
    return [ev["contentBlockDelta"] for ev in events if "contentBlockDelta" in ev]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable per-chunk sleep so tests run fast."""
    monkeypatch.setenv("YUKAR_FAKE_SLEEP", "0")


# ---------------------------------------------------------------------------
# (a) stop_reason
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_turn_default_stop_reason() -> None:
    """TextTurn without stop_reason emits 'end_turn'."""
    model = FakeModel(script=[TextTurn("hello")])
    events = await collect_events(model)
    ms = get_message_stop(events)
    assert ms["stopReason"] == "end_turn"


@pytest.mark.asyncio
async def test_text_turn_custom_stop_reason() -> None:
    """TextTurn with stop_reason='max_tokens' emits that value."""
    model = FakeModel(script=[TextTurn("hi", stop_reason="max_tokens")])
    events = await collect_events(model)
    ms = get_message_stop(events)
    assert ms["stopReason"] == "max_tokens"


@pytest.mark.asyncio
async def test_tool_use_turn_default_stop_reason() -> None:
    """ToolUseTurn without stop_reason emits 'end_turn'."""
    model = FakeModel(
        script=[ToolUseTurn(tool_name="fs_read", tool_input={"path": "x"})]
    )
    events = await collect_events(model)
    ms = get_message_stop(events)
    assert ms["stopReason"] == "end_turn"


@pytest.mark.asyncio
async def test_tool_use_turn_custom_stop_reason() -> None:
    """ToolUseTurn with stop_reason='tool_use' emits that value."""
    model = FakeModel(
        script=[
            ToolUseTurn(
                tool_name="fs_read",
                tool_input={"path": "x"},
                stop_reason="tool_use",
            )
        ]
    )
    events = await collect_events(model)
    ms = get_message_stop(events)
    assert ms["stopReason"] == "tool_use"


# ---------------------------------------------------------------------------
# (b) usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_turn_default_usage_zero() -> None:
    """TextTurn without usage yields all-zero metadata."""
    model = FakeModel(script=[TextTurn("hello")])
    events = await collect_events(model)
    meta = get_metadata(events)
    assert meta["usage"] == {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}


@pytest.mark.asyncio
async def test_text_turn_custom_usage() -> None:
    """TextTurn with usage dict emits that dict verbatim."""
    usage = cast(Usage, {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15})
    model = FakeModel(script=[TextTurn("hi", usage=usage)])
    events = await collect_events(model)
    meta = get_metadata(events)
    assert meta["usage"] == usage


@pytest.mark.asyncio
async def test_tool_use_turn_usage_with_cache_keys() -> None:
    """ToolUseTurn usage dict with cache keys is emitted verbatim."""
    usage = cast(
        Usage,
        {
            "inputTokens": 20,
            "outputTokens": 8,
            "totalTokens": 28,
            "cacheReadInputTokens": 5,
            "cacheWriteInputTokens": 3,
        },
    )
    model = FakeModel(
        script=[
            ToolUseTurn(tool_name="fs_read", tool_input={"path": "y"}, usage=usage)
        ]
    )
    events = await collect_events(model)
    meta = get_metadata(events)
    assert meta["usage"] == usage


@pytest.mark.asyncio
async def test_tool_use_turn_default_usage_zero() -> None:
    """ToolUseTurn without usage yields all-zero metadata."""
    model = FakeModel(
        script=[ToolUseTurn(tool_name="fs_read", tool_input={})]
    )
    events = await collect_events(model)
    meta = get_metadata(events)
    assert meta["usage"] == {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}


# ---------------------------------------------------------------------------
# (c) chunk_input / raw_input_json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_use_turn_single_delta_by_default() -> None:
    """Default ToolUseTurn emits a single contentBlockDelta for tool input."""
    tool_input = {"key": "value", "number": 42}
    model = FakeModel(
        script=[ToolUseTurn(tool_name="do_thing", tool_input=tool_input)]
    )
    events = await collect_events(model)
    deltas = get_content_deltas(events)
    assert len(deltas) == 1
    got = deltas[0]["delta"]["toolUse"]["input"]
    assert json.loads(got) == tool_input


@pytest.mark.asyncio
async def test_tool_use_turn_chunk_input_multiple_deltas() -> None:
    """chunk_input=True splits the JSON into multiple contentBlockDelta events."""
    # Use a payload large enough to force splitting (> _CHUNK_SIZE=12 chars).
    tool_input = {"alpha": "AAAAAAAAAAAAAAAA", "beta": "BBBBBBBBBBBBBB"}
    model = FakeModel(
        script=[
            ToolUseTurn(
                tool_name="do_thing",
                tool_input=tool_input,
                chunk_input=True,
            )
        ]
    )
    events = await collect_events(model)
    deltas = get_content_deltas(events)
    # Must be split into more than 1 delta.
    assert len(deltas) > 1
    # Concatenating all input fragments must reconstruct the original JSON.
    reconstructed = "".join(d["delta"]["toolUse"]["input"] for d in deltas)
    assert json.loads(reconstructed) == tool_input


@pytest.mark.asyncio
async def test_tool_use_turn_chunk_input_false_single_delta() -> None:
    """Explicitly setting chunk_input=False preserves single-delta behaviour."""
    tool_input = {"alpha": "AAAAAAAAAAAAAAAA", "beta": "BBBBBBBBBBBBBB"}
    model = FakeModel(
        script=[
            ToolUseTurn(
                tool_name="do_thing",
                tool_input=tool_input,
                chunk_input=False,
            )
        ]
    )
    events = await collect_events(model)
    deltas = get_content_deltas(events)
    assert len(deltas) == 1


@pytest.mark.asyncio
async def test_tool_use_turn_raw_input_json() -> None:
    """raw_input_json emits the raw string as-is (even malformed JSON)."""
    broken_json = '{"key": "value"'  # intentionally unclosed
    model = FakeModel(
        script=[
            ToolUseTurn(
                tool_name="do_thing",
                tool_input={},
                raw_input_json=broken_json,
            )
        ]
    )
    events = await collect_events(model)
    deltas = get_content_deltas(events)
    assert len(deltas) == 1
    assert deltas[0]["delta"]["toolUse"]["input"] == broken_json


@pytest.mark.asyncio
async def test_raw_input_json_takes_precedence_over_chunk_input() -> None:
    """raw_input_json is emitted as-is even when chunk_input=True."""
    raw = '{"x": 1}'
    model = FakeModel(
        script=[
            ToolUseTurn(
                tool_name="t",
                tool_input={"ignored": True},
                raw_input_json=raw,
                chunk_input=True,
            )
        ]
    )
    events = await collect_events(model)
    deltas = get_content_deltas(events)
    assert len(deltas) == 1
    assert deltas[0]["delta"]["toolUse"]["input"] == raw


# ---------------------------------------------------------------------------
# (d) RaiseTurn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raise_turn_max_tokens() -> None:
    """RaiseTurn with MaxTokensReachedException raises that exception."""
    from strands.types.exceptions import MaxTokensReachedException

    model = FakeModel(
        script=[RaiseTurn(exc_name="MaxTokensReachedException", message="too long")]
    )
    with pytest.raises(MaxTokensReachedException):
        async for _ in model.stream([]):  # type: ignore[arg-type]
            pass


@pytest.mark.asyncio
async def test_raise_turn_context_window() -> None:
    """RaiseTurn with ContextWindowOverflowException raises that exception."""
    from strands.types.exceptions import ContextWindowOverflowException

    model = FakeModel(
        script=[RaiseTurn(exc_name="ContextWindowOverflowException", message="overflow")]
    )
    with pytest.raises(ContextWindowOverflowException):
        async for _ in model.stream([]):  # type: ignore[arg-type]
            pass


@pytest.mark.asyncio
async def test_raise_turn_model_throttled() -> None:
    """RaiseTurn with ModelThrottledException raises that exception."""
    from strands.types.exceptions import ModelThrottledException

    model = FakeModel(
        script=[RaiseTurn(exc_name="ModelThrottledException", message="throttled")]
    )
    with pytest.raises(ModelThrottledException):
        async for _ in model.stream([]):  # type: ignore[arg-type]
            pass


@pytest.mark.asyncio
async def test_raise_turn_unknown_exc_name() -> None:
    """RaiseTurn with an unknown exc_name raises ValueError."""
    model = FakeModel(script=[RaiseTurn(exc_name="NonExistentException")])
    with pytest.raises(ValueError, match="NonExistentException"):
        async for _ in model.stream([]):  # type: ignore[arg-type]
            pass


@pytest.mark.asyncio
async def test_raise_turn_message_is_propagated() -> None:
    """The message string is accessible via the raised exception."""
    from strands.types.exceptions import ModelThrottledException

    model = FakeModel(
        script=[RaiseTurn(exc_name="ModelThrottledException", message="rate limit hit")]
    )
    with pytest.raises(ModelThrottledException, match="rate limit hit"):
        async for _ in model.stream([]):  # type: ignore[arg-type]
            pass


# ---------------------------------------------------------------------------
# (e) from_env parsing of new keys
# ---------------------------------------------------------------------------


def test_from_env_text_with_stop_reason_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from_env parses stop_reason and usage from a text turn."""
    script = json.dumps(
        [
            {
                "type": "text",
                "text": "hello",
                "stop_reason": "max_tokens",
                "usage": {"inputTokens": 5, "outputTokens": 2, "totalTokens": 7},
            }
        ]
    )
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    model = FakeModel.from_env()
    turn = model._script[0]
    assert isinstance(turn, TextTurn)
    assert turn.stop_reason == "max_tokens"
    assert turn.usage == {"inputTokens": 5, "outputTokens": 2, "totalTokens": 7}


def test_from_env_tool_use_with_chunk_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from_env parses chunk_input=True for a tool_use turn."""
    script = json.dumps(
        [
            {
                "type": "tool_use",
                "tool_name": "fs_read",
                "tool_input": {"path": "x"},
                "chunk_input": True,
            }
        ]
    )
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    model = FakeModel.from_env()
    turn = model._script[0]
    assert isinstance(turn, ToolUseTurn)
    assert turn.chunk_input is True


def test_from_env_tool_use_with_raw_input_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from_env parses raw_input_json for a tool_use turn."""
    raw = '{"bad": json'
    script = json.dumps(
        [
            {
                "type": "tool_use",
                "tool_name": "t",
                "tool_input": {},
                "raw_input_json": raw,
            }
        ]
    )
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    model = FakeModel.from_env()
    turn = model._script[0]
    assert isinstance(turn, ToolUseTurn)
    assert turn.raw_input_json == raw


def test_from_env_raise_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """from_env parses a raise turn correctly."""
    script = json.dumps(
        [
            {
                "type": "raise",
                "exc_name": "MaxTokensReachedException",
                "message": "limit",
            }
        ]
    )
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    model = FakeModel.from_env()
    turn = model._script[0]
    assert isinstance(turn, RaiseTurn)
    assert turn.exc_name == "MaxTokensReachedException"
    assert turn.message == "limit"


def test_from_env_role_based_with_raise_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from_env parses raise turns within role-based script objects."""
    script = json.dumps(
        {
            "manager": [{"type": "text", "text": "plan"}],
            "worker": [
                {
                    "type": "raise",
                    "exc_name": "ModelThrottledException",
                    "message": "busy",
                }
            ],
        }
    )
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    worker_model = FakeModel.from_env(role="worker")
    turn = worker_model._script[0]
    assert isinstance(turn, RaiseTurn)
    assert turn.exc_name == "ModelThrottledException"

    manager_model = FakeModel.from_env(role="manager")
    assert isinstance(manager_model._script[0], TextTurn)


def test_from_env_usage_with_cache_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """from_env preserves extra cache keys in usage dict."""
    usage = {
        "inputTokens": 100,
        "outputTokens": 50,
        "totalTokens": 150,
        "cacheReadInputTokens": 80,
        "cacheWriteInputTokens": 20,
    }
    script = json.dumps(
        [{"type": "text", "text": "x", "usage": usage}]
    )
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    model = FakeModel.from_env()
    turn = model._script[0]
    assert isinstance(turn, TextTurn)
    assert turn.usage == usage


def test_from_env_unknown_type_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """from_env raises ValueError for unknown turn type."""
    script = json.dumps([{"type": "unknown_type", "text": "x"}])
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    with pytest.raises(ValueError, match="unknown_type"):
        FakeModel.from_env()


# ---------------------------------------------------------------------------
# Regression: existing _parse_turns / from_env behaviour unchanged
# ---------------------------------------------------------------------------


def test_parse_legacy_text_and_tool_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy text + tool_use turns parse correctly with no new fields."""
    script = json.dumps(
        [
            {"type": "text", "text": "Hello"},
            {"type": "tool_use", "tool_name": "fs_read", "tool_input": {"path": "f"}},
        ]
    )
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    model = FakeModel.from_env()
    assert len(model._script) == 2
    t0 = model._script[0]
    t1 = model._script[1]
    assert isinstance(t0, TextTurn)
    assert t0.text == "Hello"
    assert t0.stop_reason is None
    assert t0.usage is None
    assert isinstance(t1, ToolUseTurn)
    assert t1.tool_name == "fs_read"
    assert t1.chunk_input is False
    assert t1.raw_input_json is None


# ---------------------------------------------------------------------------
# Integration + robustness regressions (from code review)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_from_env_text_emits_stop_reason_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A from_env-parsed turn actually emits its stop_reason/usage when streamed.

    Guards the full YUKAR_FAKE_SCRIPT path the E2E suite relies on (parse → emit),
    not just dataclass construction.
    """
    usage = {"inputTokens": 12, "outputTokens": 3, "totalTokens": 15}
    script = json.dumps(
        [{"type": "text", "text": "hi", "stop_reason": "max_tokens", "usage": usage}]
    )
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    model = FakeModel.from_env()
    events = await collect_events(model)
    assert get_message_stop(events)["stopReason"] == "max_tokens"
    assert get_metadata(events)["usage"] == usage


@pytest.mark.asyncio
async def test_default_usage_dicts_are_independent_copies() -> None:
    """Each default (zero) usage emit yields a fresh dict — no shared singleton.

    Mutating one emit's usage dict must not leak into a later emit.
    """
    model = FakeModel(script=[TextTurn("a"), TextTurn("b")])
    first = get_metadata(await collect_events(model))["usage"]
    first["inputTokens"] = 999  # mutate the first emit's dict in place
    second = get_metadata(await collect_events(model))["usage"]
    assert second == {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}


def test_from_env_raise_turn_missing_exc_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raise turn without exc_name fails fast at parse time (KeyError)."""
    script = json.dumps([{"type": "raise"}])
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    with pytest.raises(KeyError):
        FakeModel.from_env()


# ===========================================================================
# MessageTurn — new multi-block fidelity tests
# ===========================================================================


def get_content_block_starts(events: list[Any]) -> list[dict[str, Any]]:
    return [ev["contentBlockStart"] for ev in events if "contentBlockStart" in ev]


def get_content_block_stops(events: list[Any]) -> list[dict[str, Any]]:
    return [ev["contentBlockStop"] for ev in events if "contentBlockStop" in ev]


# ---------------------------------------------------------------------------
# (f) MessageTurn streaming structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_turn_text_and_tool_use_block_indices() -> None:
    """MessageTurn emits TextBlock at index 0 and ToolUseBlock at index 1."""
    model = FakeModel(
        script=[
            MessageTurn(
                blocks=[
                    TextBlock(text="reading"),
                    ToolUseBlock(tool_name="fs_read", tool_input={"path": "x"}),
                ]
            )
        ]
    )
    events = await collect_events(model)

    starts = get_content_block_starts(events)
    assert len(starts) == 2, f"Expected 2 contentBlockStart events, got {starts}"
    assert starts[0]["contentBlockIndex"] == 0
    assert starts[1]["contentBlockIndex"] == 1

    stops = get_content_block_stops(events)
    assert len(stops) == 2, f"Expected 2 contentBlockStop events, got {stops}"
    assert stops[0]["contentBlockIndex"] == 0
    assert stops[1]["contentBlockIndex"] == 1


@pytest.mark.asyncio
async def test_message_turn_single_message_stop() -> None:
    """MessageTurn emits exactly one messageStop event."""
    model = FakeModel(
        script=[
            MessageTurn(
                blocks=[
                    TextBlock(text="hello"),
                    ToolUseBlock(tool_name="fs_read", tool_input={"path": "f"}),
                ]
            )
        ]
    )
    events = await collect_events(model)
    message_stops = [ev for ev in events if "messageStop" in ev]
    assert len(message_stops) == 1


@pytest.mark.asyncio
async def test_message_turn_stop_reason_tool_use_when_tool_present() -> None:
    """MessageTurn with ToolUseBlock defaults stopReason to 'tool_use'."""
    model = FakeModel(
        script=[
            MessageTurn(
                blocks=[
                    TextBlock(text="reading"),
                    ToolUseBlock(tool_name="fs_read", tool_input={"path": "x"}),
                ]
            )
        ]
    )
    events = await collect_events(model)
    ms = get_message_stop(events)
    assert ms["stopReason"] == "tool_use"


@pytest.mark.asyncio
async def test_message_turn_stop_reason_end_turn_when_text_only() -> None:
    """MessageTurn with only TextBlocks defaults stopReason to 'end_turn'."""
    model = FakeModel(
        script=[
            MessageTurn(blocks=[TextBlock(text="hello"), TextBlock(text=" world")])
        ]
    )
    events = await collect_events(model)
    ms = get_message_stop(events)
    assert ms["stopReason"] == "end_turn"


@pytest.mark.asyncio
async def test_message_turn_stop_reason_override() -> None:
    """MessageTurn stop_reason override takes precedence over default."""
    model = FakeModel(
        script=[
            MessageTurn(
                blocks=[ToolUseBlock(tool_name="t", tool_input={})],
                stop_reason="max_tokens",
            )
        ]
    )
    events = await collect_events(model)
    ms = get_message_stop(events)
    assert ms["stopReason"] == "max_tokens"


@pytest.mark.asyncio
async def test_message_turn_text_delta_concatenation() -> None:
    """Text deltas from the TextBlock in a MessageTurn reconstruct the original text."""
    original = "reading"
    model = FakeModel(
        script=[
            MessageTurn(
                blocks=[
                    TextBlock(text=original),
                    ToolUseBlock(tool_name="fs_read", tool_input={"path": "x"}),
                ]
            )
        ]
    )
    events = await collect_events(model)
    # Only collect deltas from contentBlockIndex 0 (the text block).
    text_deltas = [
        ev["contentBlockDelta"]["delta"]["text"]
        for ev in events
        if "contentBlockDelta" in ev
        and ev["contentBlockDelta"]["contentBlockIndex"] == 0
        and "text" in ev["contentBlockDelta"]["delta"]
    ]
    assert "".join(text_deltas) == original


@pytest.mark.asyncio
async def test_message_turn_tool_input_reconstruction() -> None:
    """Tool input from ToolUseBlock in a MessageTurn reconstructs to the original dict."""
    tool_input = {"path": "x", "mode": "read"}
    model = FakeModel(
        script=[
            MessageTurn(
                blocks=[
                    TextBlock(text="reading"),
                    ToolUseBlock(tool_name="fs_read", tool_input=tool_input),
                ]
            )
        ]
    )
    events = await collect_events(model)
    # Collect deltas from contentBlockIndex 1 (the tool_use block).
    tool_deltas = [
        ev["contentBlockDelta"]["delta"]["toolUse"]["input"]
        for ev in events
        if "contentBlockDelta" in ev
        and ev["contentBlockDelta"]["contentBlockIndex"] == 1
        and "toolUse" in ev["contentBlockDelta"]["delta"]
    ]
    reconstructed = "".join(tool_deltas)
    assert json.loads(reconstructed) == tool_input


@pytest.mark.asyncio
async def test_message_turn_usage_propagated() -> None:
    """MessageTurn with custom usage emits that usage in metadata."""
    usage = cast(Usage, {"inputTokens": 5, "outputTokens": 3, "totalTokens": 8})
    model = FakeModel(
        script=[
            MessageTurn(
                blocks=[TextBlock(text="hi")],
                usage=usage,
            )
        ]
    )
    events = await collect_events(model)
    meta = get_metadata(events)
    assert meta["usage"] == usage


@pytest.mark.asyncio
async def test_message_turn_default_usage_zero() -> None:
    """MessageTurn without usage yields all-zero metadata."""
    model = FakeModel(
        script=[MessageTurn(blocks=[TextBlock(text="hi")])]
    )
    events = await collect_events(model)
    meta = get_metadata(events)
    assert meta["usage"] == {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}


@pytest.mark.asyncio
async def test_message_turn_default_usage_dict_independent_copy() -> None:
    """Default (zero) usage emitted by MessageTurn is a fresh dict per emit."""
    model = FakeModel(
        script=[
            MessageTurn(blocks=[TextBlock(text="a")]),
            MessageTurn(blocks=[TextBlock(text="b")]),
        ]
    )
    first = get_metadata(await collect_events(model))["usage"]
    first["inputTokens"] = 999
    second = get_metadata(await collect_events(model))["usage"]
    assert second == {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}


# ---------------------------------------------------------------------------
# (g) from_env — message turn parsing
# ---------------------------------------------------------------------------


def test_from_env_message_turn_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """from_env parses a 'message' turn with blocks correctly."""
    script = json.dumps(
        [
            {
                "type": "message",
                "blocks": [
                    {"type": "text", "text": "reading"},
                    {"type": "tool_use", "tool_name": "fs_read", "tool_input": {"path": "x"}},
                ],
                "stop_reason": None,
                "usage": {"inputTokens": 2, "outputTokens": 1, "totalTokens": 3},
            }
        ]
    )
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    model = FakeModel.from_env()
    assert len(model._script) == 1
    turn = model._script[0]
    assert isinstance(turn, MessageTurn)
    assert len(turn.blocks) == 2
    assert isinstance(turn.blocks[0], TextBlock)
    assert turn.blocks[0].text == "reading"
    assert isinstance(turn.blocks[1], ToolUseBlock)
    assert turn.blocks[1].tool_name == "fs_read"
    assert turn.blocks[1].tool_input == {"path": "x"}
    assert turn.usage == {"inputTokens": 2, "outputTokens": 1, "totalTokens": 3}


def test_from_env_message_turn_unknown_block_type_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from_env raises ValueError for an unknown block type in a message turn."""
    script = json.dumps(
        [
            {
                "type": "message",
                "blocks": [{"type": "unknown_block", "text": "x"}],
            }
        ]
    )
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    with pytest.raises(ValueError, match="unknown_block"):
        FakeModel.from_env()


# ---------------------------------------------------------------------------
# (h) Integration: StreamTranslator — same msg_index for text + tool in one message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_turn_same_msg_index_via_stream_translator() -> None:
    """TokenEvent and ToolCallEvent from a MessageTurn share the same msg_index.

    This is the canonical proof that the frontend will render text + tool call
    inside a single bubble.  We drive a real StreamTranslator with a real
    strands Agent backed by a FakeModel that emits a MessageTurn containing
    both a TextBlock and a ToolUseBlock.

    The test uses the real Agent + StreamTranslator pipeline (the same path
    production uses) so it guards the full emit → translate chain.
    """
    import asyncio

    from strands import Agent
    from strands import tool as strands_tool

    from yukar.agents.streaming import StreamTranslator
    from yukar.events import bus as event_bus
    from yukar.models.events import TokenEvent, ToolCallEvent

    @strands_tool
    def probe_tool(path: str) -> str:
        """A probe tool for testing."""
        return f"content_of_{path}"

    model = FakeModel(
        script=[
            MessageTurn(
                blocks=[
                    TextBlock(text="reading"),
                    ToolUseBlock(tool_name="probe_tool", tool_input={"path": "x"}),
                ]
            ),
            TextTurn("Done."),
        ]
    )

    translator = StreamTranslator(
        project_id="mt_p", epic_id="mt_e", run_id="mt_r", thread_id="mt_t"
    )
    agent = Agent(
        model=model,
        tools=[probe_tool],
        callback_handler=translator.callback,
    )

    token_events: list[TokenEvent] = []
    tool_call_events: list[ToolCallEvent] = []

    async with event_bus.subscribe("mt_p", "mt_e") as q:

        async def _run() -> None:
            async for _ in agent.stream_async("run probe_tool"):
                pass

        run_task = asyncio.create_task(_run())

        deadline = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < deadline:
            if run_task.done() and q.empty():
                break
            try:
                ev = await asyncio.wait_for(q.get(), timeout=0.1)
                if isinstance(ev, TokenEvent):
                    token_events.append(ev)
                elif isinstance(ev, ToolCallEvent):
                    tool_call_events.append(ev)
            except TimeoutError:
                if run_task.done():
                    break

        await run_task

    # The MessageTurn text and tool call must share the same msg_index.
    assert token_events, "Expected at least one TokenEvent"
    assert tool_call_events, "Expected at least one ToolCallEvent"

    # Filter to only the first assistant message (msg_index from the MessageTurn).
    first_msg_index = token_events[0].msg_index
    call_at_same_index = [e for e in tool_call_events if e.msg_index == first_msg_index]
    assert call_at_same_index, (
        f"Expected ToolCallEvent at msg_index={first_msg_index} (same as TokenEvent), "
        f"but tool call events had indices: {[e.msg_index for e in tool_call_events]}"
    )


def test_message_turn_empty_blocks_rejected() -> None:
    """MessageTurn with no blocks fails fast (a real message always has >=1 block)."""
    with pytest.raises(ValueError, match="at least one block"):
        MessageTurn(blocks=[])


# ===========================================================================
# (i) model_id retention and pricing wiring (including backward compatibility)
# ===========================================================================


def test_fake_model_model_id_stored_in_config() -> None:
    """FakeModel(model_id=...) must store that value in get_config()."""
    m = FakeModel(model_id="us.anthropic.claude-sonnet-4-6-20251201-v1:0")
    assert m.get_config()["model_id"] == "us.anthropic.claude-sonnet-4-6-20251201-v1:0"


def test_fake_model_no_model_id_config_empty() -> None:
    """When model_id is not specified, get_config() must return {} (backward-compat)."""
    m = FakeModel(script=[TextTurn("hi")])
    assert m.get_config() == {}


def test_from_env_model_id_propagated(monkeypatch: pytest.MonkeyPatch) -> None:
    """from_env(role, model_id=...) must reflect model_id in get_config."""
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", "[]")
    m = FakeModel.from_env(
        "manager", model_id="us.anthropic.claude-sonnet-4-6-20251201-v1:0"
    )
    assert m.get_config()["model_id"] == "us.anthropic.claude-sonnet-4-6-20251201-v1:0"


def test_from_env_no_model_id_config_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """When model_id is not passed to from_env, get_config() must remain {} (backward-compat)."""
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", "[]")
    m = FakeModel.from_env("manager")
    assert m.get_config() == {}


def test_resolve_model_id_returns_fake_model_id() -> None:
    """resolve_model_id must read model_id from FakeModel's get_config."""
    from yukar.agents.streaming.helpers import resolve_model_id

    model_id = "us.anthropic.claude-sonnet-4-6-20251201-v1:0"
    m = FakeModel(model_id=model_id)
    assert resolve_model_id(m) == model_id


def test_resolve_model_id_unknown_when_no_model_id() -> None:
    """resolve_model_id must return 'unknown' for a FakeModel without model_id."""
    from yukar.agents.streaming.helpers import resolve_model_id

    m = FakeModel()
    assert resolve_model_id(m) == "unknown"


def test_pricing_nonzero_for_sonnet_model_id() -> None:
    """A model_id containing sonnet-4-6 must return a non-zero cost_usd."""
    from yukar.usage.pricing import compute_cost_usd

    model_id = "us.anthropic.claude-sonnet-4-6-20251201-v1:0"
    cost = compute_cost_usd(model_id, input_tokens=1000, output_tokens=500)
    # 1000 * 3.0/1e6 + 500 * 15.0/1e6 = 0.003 + 0.0075 = 0.0105 USD
    assert cost > 0.0


def test_cost_zero_when_no_tokens_regardless_of_model() -> None:
    """When all token counts are 0, cost must be 0 (backward-compat regression)."""
    from yukar.usage.pricing import compute_cost_usd

    model_id = "us.anthropic.claude-sonnet-4-6-20251201-v1:0"
    cost = compute_cost_usd(model_id, input_tokens=0, output_tokens=0)
    assert cost == 0.0


# ===========================================================================
# (j) per_call — per-call scripting for role-based YUKAR_FAKE_SCRIPT
# ===========================================================================


def test_per_call_advances_through_scripts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A role with per_call in from_env selects a different script on each call."""
    from yukar.llm.fake import FakeModel, reset_call_counts

    script = json.dumps(
        {
            "worker": {
                "per_call": [
                    [{"type": "text", "text": "call-0"}],
                    [{"type": "text", "text": "call-1"}],
                    [{"type": "text", "text": "call-2"}],
                ]
            }
        }
    )
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)

    # autouse fixture already reset counts; explicit reset here for clarity.
    reset_call_counts()

    m0 = FakeModel.from_env(role="worker")
    m1 = FakeModel.from_env(role="worker")
    m2 = FakeModel.from_env(role="worker")

    assert isinstance(m0._script[0], TextTurn)
    assert m0._script[0].text == "call-0"
    assert isinstance(m1._script[0], TextTurn)
    assert m1._script[0].text == "call-1"
    assert isinstance(m2._script[0], TextTurn)
    assert m2._script[0].text == "call-2"


def test_per_call_repeats_last_when_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """When per_call is exhausted, the last script is repeated."""
    from yukar.llm.fake import FakeModel, reset_call_counts

    script = json.dumps(
        {
            "evaluator": {
                "per_call": [
                    [{"type": "text", "text": "first"}],
                    [{"type": "text", "text": "last"}],
                ]
            }
        }
    )
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    reset_call_counts()

    m0 = FakeModel.from_env(role="evaluator")
    m1 = FakeModel.from_env(role="evaluator")
    # From the 3rd call onward, the last script (index 1) is used
    m2 = FakeModel.from_env(role="evaluator")
    m3 = FakeModel.from_env(role="evaluator")

    assert isinstance(m0._script[0], TextTurn) and m0._script[0].text == "first"
    assert isinstance(m1._script[0], TextTurn) and m1._script[0].text == "last"
    assert isinstance(m2._script[0], TextTurn) and m2._script[0].text == "last"
    assert isinstance(m3._script[0], TextTurn) and m3._script[0].text == "last"


@pytest.mark.asyncio
async def test_per_call_different_tool_use_emit_between_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """per_call for the same role emits a different tool_use on the 1st vs 2nd call.

    Minimal configuration modelling the evaluator reject→accept pattern.
    """
    from yukar.llm.fake import FakeModel, reset_call_counts

    script = json.dumps(
        {
            "evaluator": {
                "per_call": [
                    # 1st call: reject
                    [
                        {
                            "type": "tool_use",
                            "tool_name": "submit_verdict",
                            "tool_input": {"verdict": "reject", "reason": "not ready"},
                        }
                    ],
                    # 2nd call: accept
                    [
                        {
                            "type": "tool_use",
                            "tool_name": "submit_verdict",
                            "tool_input": {"verdict": "accept", "reason": "looks good"},
                        }
                    ],
                ]
            }
        }
    )
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    reset_call_counts()

    # 1st call: reject
    m0 = FakeModel.from_env(role="evaluator")
    events0 = await collect_events(m0)
    deltas0 = get_content_deltas(events0)
    input0 = json.loads(deltas0[0]["delta"]["toolUse"]["input"])
    assert input0["verdict"] == "reject"

    # 2nd call: accept
    m1 = FakeModel.from_env(role="evaluator")
    events1 = await collect_events(m1)
    deltas1 = get_content_deltas(events1)
    input1 = json.loads(deltas1[0]["delta"]["toolUse"]["input"])
    assert input1["verdict"] == "accept"


def test_per_call_backward_compat_array_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """When role is an array (legacy form), from_env always returns the same script."""
    from yukar.llm.fake import FakeModel, reset_call_counts

    script = json.dumps(
        {
            "manager": [{"type": "text", "text": "same every time"}],
        }
    )
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    reset_call_counts()

    for _ in range(4):
        m = FakeModel.from_env(role="manager")
        assert len(m._script) == 1
        assert isinstance(m._script[0], TextTurn)
        assert m._script[0].text == "same every time"


def test_reset_call_counts_resets_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """reset_call_counts() resets the counter to 0 so per_call starts from the beginning."""
    from yukar.llm.fake import FakeModel, reset_call_counts

    script = json.dumps(
        {
            "worker": {
                "per_call": [
                    [{"type": "text", "text": "first"}],
                    [{"type": "text", "text": "second"}],
                ]
            }
        }
    )
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    reset_call_counts()

    # Advance the counter by calling twice
    FakeModel.from_env(role="worker")
    m1 = FakeModel.from_env(role="worker")
    assert isinstance(m1._script[0], TextTurn) and m1._script[0].text == "second"

    # After reset, per_call starts from the beginning
    reset_call_counts()
    m_after = FakeModel.from_env(role="worker")
    assert isinstance(m_after._script[0], TextTurn) and m_after._script[0].text == "first"


def test_per_call_invalid_element_not_list_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ValueError must be raised when a per_call element is not a list."""
    from yukar.llm.fake import FakeModel, reset_call_counts

    # per_call element is a dict (not a list)
    script = json.dumps(
        {
            "worker": {
                "per_call": [
                    {"type": "text", "text": "oops — should be a list, not a dict"}
                ]
            }
        }
    )
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    reset_call_counts()

    with pytest.raises(ValueError, match="per_call"):
        FakeModel.from_env(role="worker")


def test_per_call_empty_array_yields_empty_script(monkeypatch: pytest.MonkeyPatch) -> None:
    """When per_call is an empty list, the generated FakeModel script is also empty."""
    from yukar.llm.fake import FakeModel, reset_call_counts

    script = json.dumps({"worker": {"per_call": []}})
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    reset_call_counts()

    m = FakeModel.from_env(role="worker")
    assert m._script == []


def test_per_call_independent_per_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """per_call counters are independent per role."""
    from yukar.llm.fake import FakeModel, reset_call_counts

    script = json.dumps(
        {
            "worker": {
                "per_call": [
                    [{"type": "text", "text": "worker-0"}],
                    [{"type": "text", "text": "worker-1"}],
                ]
            },
            "evaluator": {
                "per_call": [
                    [{"type": "text", "text": "eval-0"}],
                    [{"type": "text", "text": "eval-1"}],
                ]
            },
        }
    )
    monkeypatch.setenv("YUKAR_FAKE_SCRIPT", script)
    reset_call_counts()

    w0 = FakeModel.from_env(role="worker")
    e0 = FakeModel.from_env(role="evaluator")
    w1 = FakeModel.from_env(role="worker")
    e1 = FakeModel.from_env(role="evaluator")

    assert isinstance(w0._script[0], TextTurn) and w0._script[0].text == "worker-0"
    assert isinstance(e0._script[0], TextTurn) and e0._script[0].text == "eval-0"
    assert isinstance(w1._script[0], TextTurn) and w1._script[0].text == "worker-1"
    assert isinstance(e1._script[0], TextTurn) and e1._script[0].text == "eval-1"
