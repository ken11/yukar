"""Tests for ResilientSummarizingConversationManager.

Covers the backward-compatibility fix for epics created before the
SummarizingConversationManager was introduced (commit 3f0ad1e).  Those sessions
have ``conversation_manager_state.__name__ == "SlidingWindowConversationManager"``
(old Strands default), which caused a ValueError crash on re-run.

Test plan
---------
1. Mismatched __name__ → no exception, returns None, state is clean.
2. Matched __name__ (own class) with prior summary → restored correctly.
3. Matched __name__ with no summary → returns None, removed_message_count restored.
4. create_conversation_manager returns ResilientSummarizingConversationManager.
5. Existing create_conversation_manager behaviour (None when disabled, new instance
   per call, thresholds forwarded) still passes.
"""

from __future__ import annotations

from typing import Any

import pytest

# ---------------------------------------------------------------------------
# ResilientSummarizingConversationManager unit tests
# ---------------------------------------------------------------------------


class TestResilientSummarizingConversationManager:
    """Unit tests for the resilient restore_from_session override."""

    def _make_manager(self, **kwargs: Any):  # type: ignore[return]
        from yukar.llm.conversation import ResilientSummarizingConversationManager

        return ResilientSummarizingConversationManager(**kwargs)

    # ------------------------------------------------------------------
    # Mismatch path — backward-compatibility
    # ------------------------------------------------------------------

    def test_sliding_window_state_returns_none_no_exception(self) -> None:
        """Persisted SlidingWindowConversationManager state must not raise."""
        mgr = self._make_manager()
        state: dict[str, Any] = {
            "__name__": "SlidingWindowConversationManager",
            "removed_message_count": 3,
        }
        # Must not raise ValueError
        result = mgr.restore_from_session(state)
        assert result is None

    def test_mismatch_does_not_restore_removed_message_count(self) -> None:
        """On mismatch, removed_message_count must stay at 0 (clean state)."""
        mgr = self._make_manager()
        state: dict[str, Any] = {
            "__name__": "SlidingWindowConversationManager",
            "removed_message_count": 99,
        }
        mgr.restore_from_session(state)
        assert mgr.removed_message_count == 0

    def test_mismatch_does_not_set_summary_message(self) -> None:
        """On mismatch, _summary_message must remain None."""
        mgr = self._make_manager()
        state: dict[str, Any] = {
            "__name__": "SlidingWindowConversationManager",
            "removed_message_count": 5,
            "summary_message": {"role": "user", "content": [{"text": "summary"}]},
        }
        mgr.restore_from_session(state)
        assert mgr._summary_message is None  # type: ignore[attr-defined]

    def test_arbitrary_unknown_name_mismatch_also_safe(self) -> None:
        """Any unrecognised class name is handled gracefully."""
        mgr = self._make_manager()
        state: dict[str, Any] = {
            "__name__": "SomeOtherConversationManager",
            "removed_message_count": 1,
        }
        result = mgr.restore_from_session(state)
        assert result is None
        assert mgr.removed_message_count == 0

    def test_missing_name_key_returns_none_no_exception(self) -> None:
        """State without __name__ key is also handled gracefully."""
        mgr = self._make_manager()
        state: dict[str, Any] = {"removed_message_count": 2}
        result = mgr.restore_from_session(state)
        assert result is None
        assert mgr.removed_message_count == 0

    # ------------------------------------------------------------------
    # Match path — normal forward-compatible restore
    # ------------------------------------------------------------------

    def test_matching_name_with_summary_restores_summary(self) -> None:
        """When __name__ matches and a summary exists, it is returned as a list."""
        from yukar.llm.conversation import ResilientSummarizingConversationManager

        mgr = self._make_manager()
        summary_msg = {"role": "user", "content": [{"text": "prior summary"}]}
        state: dict[str, Any] = {
            "__name__": ResilientSummarizingConversationManager.__name__,
            "removed_message_count": 7,
            "summary_message": summary_msg,
        }
        result = mgr.restore_from_session(state)
        assert result == [summary_msg]
        assert mgr._summary_message == summary_msg  # type: ignore[attr-defined]
        assert mgr.removed_message_count == 7

    def test_matching_name_without_summary_returns_none(self) -> None:
        """When __name__ matches but no prior summary exists, None is returned."""
        from yukar.llm.conversation import ResilientSummarizingConversationManager

        mgr = self._make_manager()
        state: dict[str, Any] = {
            "__name__": ResilientSummarizingConversationManager.__name__,
            "removed_message_count": 4,
            "summary_message": None,
        }
        result = mgr.restore_from_session(state)
        assert result is None
        assert mgr.removed_message_count == 4

    def test_matching_name_removed_message_count_restored(self) -> None:
        """removed_message_count is restored correctly on a matching restore."""
        from yukar.llm.conversation import ResilientSummarizingConversationManager

        mgr = self._make_manager()
        state: dict[str, Any] = {
            "__name__": ResilientSummarizingConversationManager.__name__,
            "removed_message_count": 13,
            "summary_message": None,
        }
        mgr.restore_from_session(state)
        assert mgr.removed_message_count == 13

    # ------------------------------------------------------------------
    # get_state round-trip
    # ------------------------------------------------------------------

    def test_get_state_uses_subclass_name(self) -> None:
        """get_state must record the subclass __name__ so future restores match."""
        from yukar.llm.conversation import ResilientSummarizingConversationManager

        mgr = self._make_manager()
        state = mgr.get_state()
        assert state["__name__"] == ResilientSummarizingConversationManager.__name__

    def test_get_state_restore_round_trip(self) -> None:
        """State produced by get_state must be restored correctly by restore_from_session."""
        mgr = self._make_manager()
        # Simulate some state
        mgr.removed_message_count = 5  # type: ignore[attr-defined]

        state = mgr.get_state()
        fresh = self._make_manager()
        result = fresh.restore_from_session(state)
        assert result is None  # no summary message
        assert fresh.removed_message_count == 5

    # ------------------------------------------------------------------
    # Constructor parameter forwarding
    # ------------------------------------------------------------------

    def test_summary_ratio_forwarded(self) -> None:
        mgr = self._make_manager(summary_ratio=0.5)
        assert mgr.summary_ratio == pytest.approx(0.5)

    def test_preserve_recent_messages_forwarded(self) -> None:
        mgr = self._make_manager(preserve_recent_messages=3)
        assert mgr.preserve_recent_messages == 3

    def test_proactive_compression_threshold_forwarded(self) -> None:
        mgr = self._make_manager(proactive_compression={"compression_threshold": 0.6})
        assert mgr._compression_threshold == pytest.approx(0.6)  # type: ignore[attr-defined]

    def test_proactive_compression_none_by_default(self) -> None:
        mgr = self._make_manager()
        assert mgr._compression_threshold is None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# create_conversation_manager — returns ResilientSummarizingConversationManager
# ---------------------------------------------------------------------------


class TestCreateConversationManagerUsesResilient:
    """create_conversation_manager must return ResilientSummarizingConversationManager."""

    def test_enabled_true_returns_resilient_manager(self) -> None:
        from yukar.config.settings import ConversationSummarySettings, LLMSettings
        from yukar.llm.conversation import ResilientSummarizingConversationManager
        from yukar.llm.factory import create_conversation_manager

        settings = LLMSettings(
            provider="fake",
            summarization=ConversationSummarySettings(enabled=True),
        )
        mgr = create_conversation_manager(settings)
        assert isinstance(mgr, ResilientSummarizingConversationManager)

    def test_enabled_false_still_returns_none(self) -> None:
        from yukar.config.settings import ConversationSummarySettings, LLMSettings
        from yukar.llm.factory import create_conversation_manager

        settings = LLMSettings(
            provider="fake",
            summarization=ConversationSummarySettings(enabled=False),
        )
        mgr = create_conversation_manager(settings)
        assert mgr is None

    def test_resilient_manager_tolerates_sliding_window_state(self) -> None:
        """End-to-end: factory-produced manager survives SlidingWindow session state.

        This is the core regression test for the run_failed crash:
        a manager produced by create_conversation_manager must not raise when
        restore_from_session receives state persisted by the old default
        SlidingWindowConversationManager.
        """
        from yukar.config.settings import ConversationSummarySettings, LLMSettings
        from yukar.llm.factory import create_conversation_manager

        settings = LLMSettings(
            provider="fake",
            summarization=ConversationSummarySettings(enabled=True),
        )
        mgr = create_conversation_manager(settings)
        assert mgr is not None

        # State as would be stored by a pre-3f0ad1e epic session
        legacy_state: dict[str, Any] = {
            "__name__": "SlidingWindowConversationManager",
            "removed_message_count": 3,
        }
        # Must not raise ValueError
        result = mgr.restore_from_session(legacy_state)
        assert result is None
        assert mgr.removed_message_count == 0  # type: ignore[union-attr]
