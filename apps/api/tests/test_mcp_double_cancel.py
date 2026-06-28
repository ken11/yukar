"""Tests for MCP cleanup resilience under double-cancel scenarios.

Verifies that orchestrator's MCP cleanup in finally block:
- Calls _stop() even when a second CancelledError arrives during stop_async()
- Sets _mcp_manager to None unconditionally
- Re-raises CancelledError after cleanup (cooperative cancellation)

This simulates the supervisor.stop() double-cancel pattern where:
  1. handle.task.cancel() is called via timeout path
  2. handle.task.cancel() is called again via outer cancel absorption path
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fake McpClientManager for isolation
# ---------------------------------------------------------------------------


class FakeMcpManager:
    """Records whether _stop() completed and stop_async() was awaited."""

    def __init__(self) -> None:
        self.stop_called: bool = False
        self._stop_done: bool = False
        # asyncio.Event so we can sequence the cancel injection
        self._stop_started: asyncio.Event = asyncio.Event()

    def _stop(self) -> None:
        """Synchronous stop — simulates MCPClient.__exit__ / thread join."""
        self._stop_done = True

    async def stop_async(self) -> None:
        """Async wrapper that simulates to_thread latency."""
        self.stop_called = True
        self._stop_started.set()
        # Simulate blocking work in a thread
        await asyncio.to_thread(self._stop)


# ---------------------------------------------------------------------------
# Helper: run a coroutine that uses asyncio.shield to wrap stop_async
# ---------------------------------------------------------------------------


async def _cleanup_with_shield(mgr: FakeMcpManager) -> asyncio.CancelledError | None:
    """Mirrors the finally-block logic in EpicOrchestrator._run_loop.

    Returns the CancelledError if one was received during cleanup,
    or None if cleanup completed normally.
    """
    _mcp_cancel: asyncio.CancelledError | None = None
    if mgr is not None:
        try:
            await asyncio.shield(mgr.stop_async())
        except asyncio.CancelledError as _ce:
            _mcp_cancel = _ce
        except Exception:
            pass
    return _mcp_cancel


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMcpDoubleCancel:
    @pytest.mark.asyncio
    async def test_stop_async_completes_on_single_cancel(self) -> None:
        """Normal cancel during stop_async: _stop() still runs to completion.

        _cleanup_with_shield absorbs CancelledError internally and returns it
        (caller decides whether to re-raise). The task completes normally from
        asyncio's perspective, but we verify _stop() still finished.
        """
        mgr = FakeMcpManager()
        received_cancel: list[asyncio.CancelledError | None] = []

        async def _run() -> None:
            ce = await _cleanup_with_shield(mgr)
            received_cancel.append(ce)
            # Mirror orchestrator: re-raise if cancelled.
            if ce is not None:
                raise ce

        task = asyncio.create_task(_run())
        # Wait until stop_async has started, then cancel.
        await asyncio.wait_for(mgr._stop_started.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # _stop() ran to completion inside the shield.
        assert mgr.stop_called is True
        assert mgr._stop_done is True

    @pytest.mark.asyncio
    async def test_stop_async_completes_on_double_cancel(self) -> None:
        """Simulate second cancel arriving immediately after first: _stop() still completes."""
        mgr = FakeMcpManager()
        received_cancel: list[asyncio.CancelledError] = []

        async def _run() -> None:
            _ce = await _cleanup_with_shield(mgr)
            if _ce is not None:
                received_cancel.append(_ce)
                raise _ce

        task = asyncio.create_task(_run())
        # Fire two cancels in quick succession.
        await asyncio.wait_for(mgr._stop_started.wait(), timeout=2.0)
        task.cancel()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert mgr.stop_called is True
        assert mgr._stop_done is True

    @pytest.mark.asyncio
    async def test_mcp_manager_reset_to_none_on_normal_completion(self) -> None:
        """_mcp_manager reference is cleared before stop_async call — not after."""
        # Verify the reference is cleared in the orchestrator's finally block
        # by checking the pattern directly: reference is reset before shield().
        mgr = FakeMcpManager()
        holder: list[FakeMcpManager | None] = [mgr]

        async def _orchestrator_finally() -> None:
            _mcp_cancel: asyncio.CancelledError | None = None
            if holder[0] is not None:
                _mcp = holder[0]
                # Clear before awaiting — so re-entry cannot call stop twice.
                holder[0] = None
                try:
                    await asyncio.shield(_mcp.stop_async())
                except asyncio.CancelledError as _ce:
                    _mcp_cancel = _ce
                except Exception:
                    pass
            if _mcp_cancel is not None:
                raise _mcp_cancel

        await _orchestrator_finally()
        assert holder[0] is None  # cleared before shield
        assert mgr._stop_done is True  # _stop ran

    @pytest.mark.asyncio
    async def test_mcp_manager_reset_to_none_even_on_cancel(self) -> None:
        """_mcp_manager holder is None even when cancel arrives during stop."""
        mgr = FakeMcpManager()
        holder: list[FakeMcpManager | None] = [mgr]

        async def _orchestrator_finally() -> None:
            _mcp_cancel: asyncio.CancelledError | None = None
            if holder[0] is not None:
                _mcp = holder[0]
                holder[0] = None  # unconditional clear
                try:
                    await asyncio.shield(_mcp.stop_async())
                except asyncio.CancelledError as _ce:
                    _mcp_cancel = _ce
                except Exception:
                    pass
            if _mcp_cancel is not None:
                raise _mcp_cancel

        task = asyncio.create_task(_orchestrator_finally())
        await asyncio.wait_for(mgr._stop_started.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # holder[0] must be None regardless of cancellation timing.
        assert holder[0] is None
        assert mgr._stop_done is True

    @pytest.mark.asyncio
    async def test_cancelled_error_reraised_after_cleanup(self) -> None:
        """CancelledError is re-raised after cleanup so asyncio marks task cancelled."""
        mgr = FakeMcpManager()
        holder: list[FakeMcpManager | None] = [mgr]
        cleanup_ran: list[bool] = []

        async def _orchestrator_finally() -> None:
            _mcp_cancel: asyncio.CancelledError | None = None
            if holder[0] is not None:
                _mcp = holder[0]
                holder[0] = None
                try:
                    await asyncio.shield(_mcp.stop_async())
                except asyncio.CancelledError as _ce:
                    _mcp_cancel = _ce
                except Exception:
                    pass
            cleanup_ran.append(True)
            if _mcp_cancel is not None:
                raise _mcp_cancel

        task = asyncio.create_task(_orchestrator_finally())
        await asyncio.wait_for(mgr._stop_started.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Cleanup (the finally body) executed before CancelledError propagated.
        assert cleanup_ran == [True]
        assert mgr._stop_done is True


# ---------------------------------------------------------------------------
# Tests for project_extras graceful degradation
# ---------------------------------------------------------------------------


class TestProjectExtrasGracefulDegradation:
    def test_build_skills_plugin_returns_none_when_import_fails(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """build_skills_plugin returns None + warning when strands.AgentSkills unavailable."""
        import sys

        # Hide AgentSkills by making 'from strands import AgentSkills' fail.
        # We patch the module-level import inside project_extras by temporarily
        # replacing the strands module with a broken one.
        original = sys.modules.get("strands")
        broken = MagicMock()
        broken.AgentSkills = None

        class _BrokenStrands(MagicMock):
            """Raises ImportError on attribute access for AgentSkills."""

            def __getattr__(self, name: str) -> Any:
                if name == "AgentSkills":
                    raise ImportError("AgentSkills not available")
                return super().__getattr__(name)

        sys.modules["strands"] = _BrokenStrands()

        # Create a skills dir with a SKILL.md so the plugin would normally load.
        from yukar.config.paths import skill_md_path

        md_path = skill_md_path(str(tmp_path), "proj", "my-skill")
        md_path.parent.mkdir(parents=True)
        md_path.write_text("# Skill")

        try:
            # Force re-import so the patched strands is used.
            import importlib

            import yukar.agents.project_extras as pe

            importlib.reload(pe)

            with patch.object(pe.logger, "warning") as mock_warn:
                plugin = pe.build_skills_plugin(str(tmp_path), "proj")

            assert plugin is None
            # Warning about unavailability should have been logged.
            assert mock_warn.called
            warning_messages = " ".join(str(c) for c in mock_warn.call_args_list)
            assert "AgentSkills" in warning_messages or "available" in warning_messages
        finally:
            if original is not None:
                sys.modules["strands"] = original
            else:
                sys.modules.pop("strands", None)
            # Re-import to restore normal state
            import importlib

            import yukar.agents.project_extras as pe

            importlib.reload(pe)
