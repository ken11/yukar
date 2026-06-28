"""Strands stream event → RunEvent translation (StreamTranslator).

Extracted from :mod:`~yukar.agents.streaming` for readability.  All public
names continue to be importable from ``yukar.agents.streaming`` via the
package ``__init__.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from yukar.events import bus as event_bus
from yukar.models.events import (
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)

logger = logging.getLogger(__name__)


class StreamTranslator:
    """Translates Strands callback_handler kwargs into RunEvents on the bus.

    Designed to be used as an agent's ``callback_handler``.  Each call
    corresponds to one Strands stream event (``TypedEvent.as_dict()``).

    Args:
        project_id: Parent project identifier.
        epic_id: Parent epic identifier.
        run_id: Active run identifier.
        thread_id: Thread (agent) identifier — used as the ``thread_id`` on
            token/tool events so the UI can filter by thread.
    """

    def __init__(
        self,
        project_id: str,
        epic_id: str,
        run_id: str,
        thread_id: str,
    ) -> None:
        self._project_id = project_id
        self._epic_id = epic_id
        self._run_id = run_id
        self._thread_id = thread_id
        # Maps toolUseId → tool_name so that toolResult messages can carry the
        # real tool name (toolResult blocks only contain the id on the result side).
        self._tool_id_to_name: dict[str, str] = {}
        # Deduplication: track toolUseIds already published as ToolCallEvent /
        # ToolResultEvent to guard against hypothetical double-fires.
        self._published_call_ids: set[str] = set()
        self._published_result_ids: set[str] = set()
        # Utterance-segment counter: incremented after each completed assistant message.
        # Reset to 0 per StreamTranslator construction (= per stream_async turn).
        self._msg_index: int = 0

    def _pub(self, event: object) -> None:
        event_bus.publish(self._project_id, self._epic_id, event)

    def callback(self, **kwargs: Any) -> None:
        """Strands callback_handler interface.

        Called once per streaming event by the Strands event loop.
        Translates the event and publishes to the bus.
        """
        # Text delta — TextStreamEvent sets "data" key.
        if "data" in kwargs and isinstance(kwargs["data"], str):
            text: str = kwargs["data"]
            if text:
                self._pub(
                    TokenEvent(
                        project_id=self._project_id,
                        epic_id=self._epic_id,
                        run_id=self._run_id,
                        thread_id=self._thread_id,
                        delta=text,
                        msg_index=self._msg_index,
                    )
                )
            return

        # Message events — arrive as {"message": <Message dict>} for both
        # ModelMessageEvent (assistant) and ToolResultMessageEvent (user).
        # This is the authoritative source for tool call and result data:
        # - assistant message: content contains toolUse blocks with complete input dict
        # - user message: content contains toolResult blocks
        if "message" in kwargs:
            self._handle_message(kwargs["message"])
            return

        # All other events (init_event_loop, start, start_event_loop,
        # tool_use_stream, result, force_stop, etc.) are intentionally ignored.

    def _handle_message(self, message: dict[str, Any]) -> None:
        """Dispatch on message role to extract tool call / result events."""
        role: str = message.get("role", "")
        content: list[dict[str, Any]] = message.get("content", [])

        if role == "assistant":
            for block in content:
                tool_use = block.get("toolUse")
                if tool_use is None:
                    continue
                self._handle_tool_use_block(tool_use)
            self._msg_index += 1

        elif role == "user":
            for block in content:
                tool_result = block.get("toolResult")
                if tool_result is None:
                    continue
                self._handle_tool_result_block(tool_result)

    def _handle_tool_use_block(self, tool_use: dict[str, Any]) -> None:
        """Publish a ToolCallEvent from an assistant toolUse content block.

        The block shape (verified via probe):
            {"toolUseId": str, "name": str, "input": dict}
        """
        tool_use_id: str = tool_use.get("toolUseId", "")
        tool_name: str = tool_use.get("name", "unknown_tool")
        tool_input: dict[str, Any] = tool_use.get("input") or {}

        # Ensure input is a dict (defensive — should always be dict here).
        if not isinstance(tool_input, dict):
            tool_input = {}

        # Deduplication guard.
        if tool_use_id and tool_use_id in self._published_call_ids:
            return

        # Record id→name for later result correlation.
        if tool_use_id:
            self._tool_id_to_name[tool_use_id] = tool_name
            self._published_call_ids.add(tool_use_id)

        self._pub(
            ToolCallEvent(
                project_id=self._project_id,
                epic_id=self._epic_id,
                run_id=self._run_id,
                thread_id=self._thread_id,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_use_id=tool_use_id,
                msg_index=self._msg_index,
            )
        )

    def _handle_tool_result_block(self, tool_result: dict[str, Any]) -> None:
        """Publish a ToolResultEvent from a user toolResult content block.

        The block shape (verified via probe):
            {"toolUseId": str, "status": "success"|"error", "content": [{"text": str}]}
        """
        result_tool_use_id: str = tool_result.get("toolUseId", "")
        status: str = tool_result.get("status", "success")
        content_blocks: list[Any] = tool_result.get("content", [])

        # Deduplication guard.
        if result_tool_use_id and result_tool_use_id in self._published_result_ids:
            return
        if result_tool_use_id:
            self._published_result_ids.add(result_tool_use_id)

        # Combine text blocks from the result content list.
        result_text = " ".join(
            block.get("text", "")
            for block in content_blocks
            if isinstance(block, dict) and block.get("text")
        )

        # Map id → real tool name; fall back to the id string.
        result_tool_name: str = self._tool_id_to_name.get(
            result_tool_use_id, result_tool_use_id or "tool"
        )

        # Prefix with error indicator when status is not success.
        if status != "success":
            result_text = f"[{status}] {result_text}".strip()

        self._pub(
            ToolResultEvent(
                project_id=self._project_id,
                epic_id=self._epic_id,
                run_id=self._run_id,
                thread_id=self._thread_id,
                tool_name=result_tool_name,
                result=result_text[:2048],
                tool_use_id=result_tool_use_id,
                msg_index=self._msg_index,
            )
        )
