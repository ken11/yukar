"""Resilient conversation manager for yukar.

Provides ``ResilientSummarizingConversationManager``, a thin subclass of
``SummarizingConversationManager`` that tolerates session state persisted by a
*different* conversation manager class (e.g. the old Strands default
``SlidingWindowConversationManager``).

Background
----------
``ConversationManager.restore_from_session`` (Strands base class) validates that
``state["__name__"]`` matches ``self.__class__.__name__``.  Before commit
``3f0ad1e`` the Manager Agent ran with ``SlidingWindowConversationManager``
(Strands' prior default).  After ``3f0ad1e`` the same agent receives a
``SummarizingConversationManager``.  On re-run the ``FileSessionManager``
deserialises the stored state and calls ``restore_from_session``; the class-name
mismatch raises ``ValueError("Invalid conversation manager state.")``, which
propagates as ``run_failed`` within ~15 ms of ``run_started``.

This subclass intercepts that mismatch and falls back to a clean initial state,
so epics created before the summarisation feature was introduced continue to
work.  The conversation *messages* are still restored by the ``FileSessionManager``
independently of this class; only the *conversation manager meta-state*
(``removed_message_count``, ``_summary_message``) is reset to defaults, which is
the safe choice when the prior state is incompatible.
"""

from __future__ import annotations

import logging
from typing import Any

from strands.agent.conversation_manager import (
    ProactiveCompressionConfig,
    SummarizingConversationManager,
)
from strands.types.content import Message

logger = logging.getLogger(__name__)


class ResilientSummarizingConversationManager(SummarizingConversationManager):
    """``SummarizingConversationManager`` that survives cross-class session restore.

    When a stored session contains a ``conversation_manager_state`` whose
    ``__name__`` does not match this class (e.g. ``SlidingWindowConversationManager``
    from a pre-summarisation run), the base class raises ``ValueError``.  This
    subclass catches that case: it logs a warning and returns ``None``, starting
    the manager in a clean initial state.  The agent's conversation *messages*
    are unaffected — they are restored by ``FileSessionManager`` separately.

    When ``__name__`` matches (normal forward-compatible restore), the call is
    delegated to ``super().restore_from_session(state)`` so that
    ``_summary_message`` and ``removed_message_count`` are restored correctly.

    Args:
        summary_ratio: Passed through to ``SummarizingConversationManager``.
        preserve_recent_messages: Passed through to ``SummarizingConversationManager``.
        proactive_compression: Passed through to ``SummarizingConversationManager``.
    """

    def __init__(
        self,
        summary_ratio: float = 0.3,
        preserve_recent_messages: int = 10,
        *,
        proactive_compression: bool | ProactiveCompressionConfig | None = None,
    ) -> None:
        """Initialise with the same signature as ``SummarizingConversationManager``.

        The ``summarization_agent`` parameter is intentionally omitted — per
        spec §6.4 / CLAUDE.md the orchestrator must be the sole owner of a
        ``FileSessionManager``, and a dedicated summarisation agent would need
        its own session state.  Summarisation uses the parent agent's model
        directly (``_generate_summary_with_model``).

        Args:
            summary_ratio: Ratio of messages to summarise when context overflows.
                Clamped to ``[0.1, 0.8]`` by the parent class.
            preserve_recent_messages: Minimum number of recent messages to retain.
            proactive_compression: Proactive compression configuration.  Pass
                ``True`` for the default 0.7 threshold, a
                ``ProactiveCompressionConfig`` dict for a custom threshold, or
                ``None``/``False`` to disable.
        """
        super().__init__(
            summary_ratio=summary_ratio,
            preserve_recent_messages=preserve_recent_messages,
            proactive_compression=proactive_compression,
        )

    def restore_from_session(self, state: dict[str, Any]) -> list[Message] | None:
        """Restore manager state, tolerating stale class-name mismatches.

        When ``state["__name__"]`` differs from ``self.__class__.__name__`` (e.g.
        the session was created with ``SlidingWindowConversationManager`` before
        the summarisation feature was introduced), the mismatch is logged and the
        manager starts fresh — ``removed_message_count`` stays 0 and
        ``_summary_message`` stays ``None``.  The agent's conversation messages
        are restored by ``FileSessionManager`` independently and are not lost.

        When ``__name__`` matches, the call is delegated to
        ``super().restore_from_session(state)`` which restores
        ``_summary_message`` and ``removed_message_count`` in full.

        Args:
            state: Persisted conversation manager state from the session file.

        Returns:
            A list containing the previous summary message to prepend (if any),
            or ``None`` when starting fresh or when no prior summary exists.
        """
        stored_name = state.get("__name__")
        if stored_name != self.__class__.__name__:
            logger.warning(
                "conversation_manager_state.__name__=%r does not match %r; "
                "starting conversation manager in clean initial state "
                "(backward-compatibility measure for sessions created before "
                "SummarizingConversationManager was introduced)",
                stored_name,
                self.__class__.__name__,
            )
            # Do not call super(): the base ConversationManager.restore_from_session
            # would raise ValueError on the name mismatch.  Return None so no
            # messages are prepended; the agent's messages are restored by
            # FileSessionManager separately.
            return None

        # Names match — delegate to the full parent restore so that
        # _summary_message and removed_message_count are recovered correctly.
        return super().restore_from_session(state)
