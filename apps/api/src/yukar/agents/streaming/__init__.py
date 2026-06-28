"""Strands stream event → RunEvent translation and usage recording.

This package re-exports all public names that were previously in the
single-file ``agents/streaming.py`` so that existing imports remain
unchanged:

    from yukar.agents.streaming import StreamTranslator
    from yukar.agents.streaming import AgentUsageRecorder
    from yukar.agents.streaming import extract_final_text
    from yukar.agents.streaming import resolve_model_id

Internal layout
---------------
- ``translator.py``    — :class:`StreamTranslator`
- ``usage_recorder.py`` — :class:`AgentUsageRecorder`
- ``helpers.py``       — :func:`extract_final_text`, :func:`resolve_model_id`,
                         and private snapshot/delta helpers

Verified callback_handler kwargs (strands-agents, FakeModel + real Agent.stream_async):

  - ``"data"`` (str)              — text delta (TextStreamEvent); fires once per
                                    streaming text chunk.
  - ``"type"=="tool_use_stream"`` — fires exactly once per tool call, with
                                    ``current_tool_use["input"]`` as a **str**
                                    (partial/complete JSON).  Input is never a
                                    dict; there is no "re-fire once complete"
                                    behaviour.  This event is ignored here.
  - ``"message"`` (dict)          — fires for each completed message added to
                                    conversation history:
                                    - assistant message with ``content``
                                      containing ``{"toolUse": {...}}`` blocks
                                      (complete input as dict, toolUseId, name)
                                      → ``ToolCallEvent``
                                    - user message with ``content`` containing
                                      ``{"toolResult": {...}}`` blocks
                                      (toolUseId, status, content list)
                                      → ``ToolResultEvent``
                                    - ``"type"=="tool_result"`` is NOT a
                                      separate kwargs key; it never appears.
  - ``"result"``                  — AgentResultEvent (final, not a stream chunk)
  - ``"init_event_loop"``         — ignored

The ``StreamTranslator`` maintains an internal ``_tool_id_to_name`` map that
records ``toolUseId → tool_name`` from completed assistant toolUse messages.
When a toolResult message arrives, the name is looked up from this map.

Both ``ToolCallEvent`` and ``ToolResultEvent`` carry ``tool_use_id`` (the
Strands ``toolUseId`` string).  A ``_published_ids`` set prevents duplicate
publishes if the same toolUseId is ever seen more than once (defensive).
"""

from yukar.agents.streaming.helpers import extract_final_text, resolve_model_id
from yukar.agents.streaming.translator import StreamTranslator
from yukar.agents.streaming.usage_recorder import AgentUsageRecorder

__all__ = [
    "AgentUsageRecorder",
    "StreamTranslator",
    "extract_final_text",
    "resolve_model_id",
]
