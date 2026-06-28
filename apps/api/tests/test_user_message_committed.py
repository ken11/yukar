"""Tests for PR-B: UserMessageCommittedEvent + FSM hook + bus backfill.

Covers:
- UserMessageCommittedEvent round-trips through the RunEvent discriminated union.
- bus.publish accumulates UserMessageCommittedEvent in the per-thread buffer.
- bus.get_user_message_backfill returns a snapshot copy of the buffer.
- Backfill is isolated to the correct (project_id, epic_id, thread_id).
- Run-boundary events (RunStartedEvent) clear the user-message buffer.
- The FSM hook emits exactly one event per committed user message (integration).
- The FSM hook does NOT emit for assistant messages.
- The FSM hook does NOT emit for messages containing toolResult blocks.
- thread_stream SSE includes user-message backfill on reconnect.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base(
    project_id: str = "p",
    epic_id: str = "e",
    run_id: str = "r",
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "epic_id": epic_id,
        "run_id": run_id,
        "ts": datetime.now(UTC).isoformat(),
    }


def _user_msg_event(
    thread_id: str = "manager",
    text: str = "hello",
    message_id: int = 0,
    project_id: str = "p",
    epic_id: str = "e",
) -> Any:
    from yukar.models.events import UserMessageCommittedEvent

    return UserMessageCommittedEvent(
        **_base(project_id=project_id, epic_id=epic_id),
        thread_id=thread_id,
        text=text,
        message_id=message_id,
    )


# ---------------------------------------------------------------------------
# 1. RunEvent discriminated union round-trip
# ---------------------------------------------------------------------------


class TestUserMessageCommittedEventRoundTrip:
    """UserMessageCommittedEvent must survive a RunEvent discriminated union cycle."""

    def test_roundtrip_type_literal(self) -> None:
        from pydantic import TypeAdapter

        from yukar.models.events import RunEvent, UserMessageCommittedEvent

        ev = UserMessageCommittedEvent(
            **_base(),
            thread_id="manager",
            text="fix the bug",
            message_id=3,
        )
        ta: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
        parsed = ta.validate_python(ev.model_dump(mode="json"))
        assert isinstance(parsed, UserMessageCommittedEvent)
        assert parsed.type == "user_message_committed"
        assert parsed.text == "fix the bug"
        assert parsed.message_id == 3
        assert parsed.thread_id == "manager"

    def test_discriminator_key_is_type(self) -> None:
        from pydantic import TypeAdapter

        from yukar.models.events import RunEvent

        raw = {
            **_base(),
            "type": "user_message_committed",
            "thread_id": "manager",
            "text": "hi",
            "message_id": 0,
        }
        ta: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
        parsed = ta.validate_python(raw)
        from yukar.models.events import UserMessageCommittedEvent

        assert isinstance(parsed, UserMessageCommittedEvent)


# ---------------------------------------------------------------------------
# 2. bus: accumulation and get_user_message_backfill
# ---------------------------------------------------------------------------


class TestBusUserMessageBuffer:
    """Unit tests for the user-message ring-buffer in events/bus.py."""

    def setup_method(self) -> None:
        from yukar.events import bus as event_bus

        event_bus._thread_user_msg_buffer.clear()

    def test_publish_accumulates_user_message(self) -> None:
        from yukar.events import bus as event_bus

        ev = _user_msg_event(text="hello", message_id=0)
        event_bus.publish("p", "e", ev)

        buf = event_bus.get_user_message_backfill("p", "e", "manager")
        assert len(buf) == 1
        assert buf[0].text == "hello"
        assert buf[0].message_id == 0

    def test_multiple_messages_accumulate_in_order(self) -> None:
        from yukar.events import bus as event_bus

        events = [_user_msg_event(text=f"msg-{i}", message_id=i) for i in range(3)]
        for ev in events:
            event_bus.publish("p", "e", ev)

        buf = event_bus.get_user_message_backfill("p", "e", "manager")
        assert len(buf) == 3
        assert [ev.message_id for ev in buf] == [0, 1, 2]

    def test_backfill_isolated_to_thread(self) -> None:
        from yukar.events import bus as event_bus

        ev_mgr = _user_msg_event(thread_id="manager", text="mgr-text", message_id=0)
        ev_other = _user_msg_event(thread_id="other-thread", text="other-text", message_id=0)
        event_bus.publish("p", "e", ev_mgr)
        event_bus.publish("p", "e", ev_other)

        buf_mgr = event_bus.get_user_message_backfill("p", "e", "manager")
        buf_other = event_bus.get_user_message_backfill("p", "e", "other-thread")

        assert len(buf_mgr) == 1
        assert buf_mgr[0].text == "mgr-text"
        assert len(buf_other) == 1
        assert buf_other[0].text == "other-text"

    def test_backfill_isolated_to_epic(self) -> None:
        from yukar.events import bus as event_bus

        ev1 = _user_msg_event(text="ep1", message_id=0, epic_id="EP-1")
        ev2 = _user_msg_event(text="ep2", message_id=0, epic_id="EP-2")
        event_bus.publish("p", "EP-1", ev1)
        event_bus.publish("p", "EP-2", ev2)

        buf1 = event_bus.get_user_message_backfill("p", "EP-1", "manager")
        buf2 = event_bus.get_user_message_backfill("p", "EP-2", "manager")

        texts1 = [ev.text for ev in buf1]
        texts2 = [ev.text for ev in buf2]
        assert "ep1" in texts1
        assert "ep2" not in texts1
        assert "ep2" in texts2
        assert "ep1" not in texts2

    def test_backfill_isolated_to_project(self) -> None:
        from yukar.events import bus as event_bus

        ev_a = _user_msg_event(text="proj-a", message_id=0, project_id="proj-a")
        ev_b = _user_msg_event(text="proj-b", message_id=0, project_id="proj-b")
        event_bus.publish("proj-a", "e", ev_a)
        event_bus.publish("proj-b", "e", ev_b)

        buf_a = event_bus.get_user_message_backfill("proj-a", "e", "manager")
        buf_b = event_bus.get_user_message_backfill("proj-b", "e", "manager")

        assert buf_a[0].text == "proj-a"
        assert buf_b[0].text == "proj-b"

    def test_returns_empty_when_no_messages(self) -> None:
        from yukar.events import bus as event_bus

        buf = event_bus.get_user_message_backfill("no-proj", "no-epic", "manager")
        assert buf == []

    def test_returns_snapshot_copy(self) -> None:
        from yukar.events import bus as event_bus

        ev = _user_msg_event(text="copy-test", message_id=0)
        event_bus.publish("p", "e", ev)

        buf = event_bus.get_user_message_backfill("p", "e", "manager")
        assert len(buf) == 1
        buf.clear()

        buf2 = event_bus.get_user_message_backfill("p", "e", "manager")
        assert len(buf2) == 1

    def test_run_started_clears_buffer(self) -> None:
        from yukar.events import bus as event_bus
        from yukar.models.events import RunStartedEvent

        ev = _user_msg_event(text="stale", message_id=0)
        event_bus.publish("p", "e", ev)
        assert len(event_bus.get_user_message_backfill("p", "e", "manager")) == 1

        # RunStartedEvent must wipe the buffer.
        event_bus.publish("p", "e", RunStartedEvent(**_base()))
        assert event_bus.get_user_message_backfill("p", "e", "manager") == []

    def test_run_completed_preserves_buffer(self) -> None:
        """Committed user messages are PERSISTENT: they survive run completion so a
        late/reconnect subscriber *after* the run still replays the human turn.

        Regression for the fake-provider smoke finding: RunCompletedEvent must NOT
        wipe the user-message buffer (unlike the ephemeral token buffer).
        """
        from yukar.events import bus as event_bus
        from yukar.models.events import RunCompletedEvent, RunFailedEvent, RunStoppedEvent

        for terminal_cls in (RunCompletedEvent, RunFailedEvent, RunStoppedEvent):
            event_bus._thread_user_msg_buffer.clear()
            ev = _user_msg_event(text="persists", message_id=0)
            event_bus.publish("p", "e", ev)
            event_bus.publish("p", "e", terminal_cls(**_base()))

            # Late subscribe AFTER the terminal event still replays the message once.
            buf = event_bus.get_user_message_backfill("p", "e", "manager")
            assert len(buf) == 1, f"{terminal_cls.__name__} should preserve the buffer"
            assert buf[0].text == "persists"

    def test_run_boundary_clears_only_own_epic(self) -> None:
        """Run boundary for epic A must not clear epic B's buffer."""
        from yukar.events import bus as event_bus
        from yukar.models.events import RunStartedEvent

        ev_a = _user_msg_event(text="ep-a", message_id=0, epic_id="EP-A")
        ev_b = _user_msg_event(text="ep-b", message_id=0, epic_id="EP-B")
        event_bus.publish("p", "EP-A", ev_a)
        event_bus.publish("p", "EP-B", ev_b)

        event_bus.publish("p", "EP-A", RunStartedEvent(**_base(epic_id="EP-A")))

        assert event_bus.get_user_message_backfill("p", "EP-A", "manager") == []
        assert len(event_bus.get_user_message_backfill("p", "EP-B", "manager")) == 1


# ---------------------------------------------------------------------------
# 3. FSM hook: event emission — exactly one per committed user message
# ---------------------------------------------------------------------------


class TestFsmHookEmission:
    """Integration tests for _register_user_message_hook.

    We build a real strands Agent and FileSessionManager, register the hook,
    then fire MessageAddedEvent manually to verify the pub function is called
    with the correct arguments.
    """

    def _make_session_message_mock(self, message_id: int) -> Any:
        m = MagicMock()
        m.message_id = message_id
        return m

    def _make_strands_message(self, role: str, text: str | None = None) -> Any:
        """Build a Strands TypedDict Message."""
        content: list[dict[str, Any]] = []
        if text is not None:
            content.append({"text": text})
        return {"role": role, "content": content}

    def _make_tool_result_message(self) -> Any:
        return {
            "role": "user",
            "content": [{"toolResult": {"toolUseId": "x", "content": [{"text": "ok"}]}}],
        }

    def test_emits_on_user_message(self) -> None:
        """A plain user text message triggers exactly one UserMessageCommittedEvent."""
        from strands.hooks.events import MessageAddedEvent

        from yukar.agents.orchestrator import _register_user_message_hook

        published: list[Any] = []

        mock_agent = MagicMock()
        mock_agent.agent_id = "manager"

        mock_fsm = MagicMock()
        mock_fsm._latest_agent_message = {"manager": self._make_session_message_mock(5)}
        mock_agent.add_hook = MagicMock()

        human_flag: list[bool] = [True]
        _register_user_message_hook(
            manager_agent=mock_agent,
            fsm=mock_fsm,
            project_id="p",
            epic_id="e",
            run_id="r",
            thread_id="manager",
            pub=published.append,
            human_turn_flag=human_flag,
        )

        # Retrieve the registered callback.
        assert mock_agent.add_hook.called
        callback = mock_agent.add_hook.call_args[0][0]

        # Fire MessageAddedEvent with a user message.
        event = MessageAddedEvent(
            agent=mock_agent,
            message=self._make_strands_message("user", "fix the auth bug"),
        )
        callback(event)

        assert len(published) == 1
        from yukar.models.events import UserMessageCommittedEvent

        assert isinstance(published[0], UserMessageCommittedEvent)
        assert published[0].text == "fix the auth bug"
        assert published[0].message_id == 5
        assert published[0].thread_id == "manager"
        assert published[0].project_id == "p"
        assert published[0].epic_id == "e"

    def test_does_not_emit_on_assistant_message(self) -> None:
        """Assistant messages must not trigger the event."""
        from strands.hooks.events import MessageAddedEvent

        from yukar.agents.orchestrator import _register_user_message_hook

        published: list[Any] = []
        mock_agent = MagicMock()
        mock_agent.agent_id = "manager"
        mock_fsm = MagicMock()
        mock_fsm._latest_agent_message = {"manager": self._make_session_message_mock(0)}
        mock_agent.add_hook = MagicMock()

        _register_user_message_hook(
            manager_agent=mock_agent,
            fsm=mock_fsm,
            project_id="p",
            epic_id="e",
            run_id="r",
            thread_id="manager",
            pub=published.append,
            human_turn_flag=[True],
        )
        callback = mock_agent.add_hook.call_args[0][0]

        event = MessageAddedEvent(
            agent=mock_agent,
            message=self._make_strands_message("assistant", "I will now dispatch tasks."),
        )
        callback(event)

        assert published == []

    def test_does_not_emit_on_tool_result_message(self) -> None:
        """Messages that contain a toolResult block must not trigger the event."""
        from strands.hooks.events import MessageAddedEvent

        from yukar.agents.orchestrator import _register_user_message_hook

        published: list[Any] = []
        mock_agent = MagicMock()
        mock_agent.agent_id = "manager"
        mock_fsm = MagicMock()
        mock_fsm._latest_agent_message = {"manager": self._make_session_message_mock(2)}
        mock_agent.add_hook = MagicMock()

        _register_user_message_hook(
            manager_agent=mock_agent,
            fsm=mock_fsm,
            project_id="p",
            epic_id="e",
            run_id="r",
            thread_id="manager",
            pub=published.append,
            human_turn_flag=[True],
        )
        callback = mock_agent.add_hook.call_args[0][0]

        event = MessageAddedEvent(
            agent=mock_agent,
            message=self._make_tool_result_message(),
        )
        callback(event)

        assert published == []

    def test_emits_once_per_user_message(self) -> None:
        """Three user messages → three events, each with the correct message_id."""
        from strands.hooks.events import MessageAddedEvent

        from yukar.agents.orchestrator import _register_user_message_hook

        published: list[Any] = []
        mock_agent = MagicMock()
        mock_agent.agent_id = "manager"
        mock_fsm = MagicMock()
        # _latest_agent_message will be updated each time; simulate monotonic idx.
        mock_fsm._latest_agent_message = {}
        mock_agent.add_hook = MagicMock()

        human_flag: list[bool] = [True]
        _register_user_message_hook(
            manager_agent=mock_agent,
            fsm=mock_fsm,
            project_id="p",
            epic_id="e",
            run_id="r",
            thread_id="manager",
            pub=published.append,
            human_turn_flag=human_flag,
        )
        callback = mock_agent.add_hook.call_args[0][0]

        for idx, text in enumerate(["turn0", "turn2", "turn4"]):
            mock_fsm._latest_agent_message["manager"] = self._make_session_message_mock(idx * 2)
            event = MessageAddedEvent(
                agent=mock_agent,
                message=self._make_strands_message("user", text),
            )
            callback(event)

        assert len(published) == 3
        assert [ev.message_id for ev in published] == [0, 2, 4]
        assert [ev.text for ev in published] == ["turn0", "turn2", "turn4"]

    def test_message_id_minus1_when_fsm_has_no_record(self) -> None:
        """If FSM has not yet stored the message (no _latest_agent_message entry),
        message_id falls back to -1 rather than crashing."""
        from strands.hooks.events import MessageAddedEvent

        from yukar.agents.orchestrator import _register_user_message_hook

        published: list[Any] = []
        mock_agent = MagicMock()
        mock_agent.agent_id = "manager"
        mock_fsm = MagicMock()
        mock_fsm._latest_agent_message = {}  # empty — no entry for "manager"
        mock_agent.add_hook = MagicMock()

        _register_user_message_hook(
            manager_agent=mock_agent,
            fsm=mock_fsm,
            project_id="p",
            epic_id="e",
            run_id="r",
            thread_id="manager",
            pub=published.append,
            human_turn_flag=[True],
        )
        callback = mock_agent.add_hook.call_args[0][0]

        event = MessageAddedEvent(
            agent=mock_agent,
            message=self._make_strands_message("user", "seed message"),
        )
        callback(event)

        assert len(published) == 1
        assert published[0].message_id == -1

    def test_does_not_emit_when_flag_is_false(self) -> None:
        """When human_turn_flag[0] is False (boilerplate turn), no event is published."""
        from strands.hooks.events import MessageAddedEvent

        from yukar.agents.orchestrator import _register_user_message_hook

        published: list[Any] = []
        mock_agent = MagicMock()
        mock_agent.agent_id = "manager"
        mock_fsm = MagicMock()
        mock_fsm._latest_agent_message = {"manager": self._make_session_message_mock(0)}
        mock_agent.add_hook = MagicMock()

        human_flag: list[bool] = [False]  # boilerplate/planning turn
        _register_user_message_hook(
            manager_agent=mock_agent,
            fsm=mock_fsm,
            project_id="p",
            epic_id="e",
            run_id="r",
            thread_id="manager",
            pub=published.append,
            human_turn_flag=human_flag,
        )
        callback = mock_agent.add_hook.call_args[0][0]

        # Even a plain user-role text message is silenced when flag is False.
        event = MessageAddedEvent(
            agent=mock_agent,
            message=self._make_strands_message("user", "Current task state:\n..."),
        )
        callback(event)

        assert published == []

    def test_flag_toggle_controls_emission(self) -> None:
        """Toggling human_turn_flag[0] mid-session controls which messages are published."""
        from strands.hooks.events import MessageAddedEvent

        from yukar.agents.orchestrator import _register_user_message_hook

        published: list[Any] = []
        mock_agent = MagicMock()
        mock_agent.agent_id = "manager"
        mock_fsm = MagicMock()
        mock_fsm._latest_agent_message = {"manager": self._make_session_message_mock(1)}
        mock_agent.add_hook = MagicMock()

        human_flag: list[bool] = [False]
        _register_user_message_hook(
            manager_agent=mock_agent,
            fsm=mock_fsm,
            project_id="p",
            epic_id="e",
            run_id="r",
            thread_id="manager",
            pub=published.append,
            human_turn_flag=human_flag,
        )
        callback = mock_agent.add_hook.call_args[0][0]

        # Boilerplate turn — should be suppressed.
        callback(
            MessageAddedEvent(
                agent=mock_agent,
                message=self._make_strands_message("user", "boilerplate"),
            )
        )
        assert published == []

        # Human turn — should be published.
        human_flag[0] = True
        callback(
            MessageAddedEvent(
                agent=mock_agent,
                message=self._make_strands_message("user", "please focus on auth"),
            )
        )
        assert len(published) == 1
        assert published[0].text == "please focus on auth"


# ---------------------------------------------------------------------------
# 4. thread_stream SSE includes user-message backfill
# ---------------------------------------------------------------------------


class TestThreadStreamUserMessageBackfill:
    """Endpoint-level tests for thread SSE user-message backfill.

    Strategy: publish a UserMessageCommittedEvent before the subscriber
    connects, then open the thread stream and verify the event is delivered
    via the backfill path.
    """

    def setup_method(self) -> None:
        from yukar.events import bus as event_bus

        event_bus._thread_user_msg_buffer.clear()
        event_bus._thread_token_buffer.clear()

    def _parse_sse_events(self, sse_body: str) -> list[dict[str, Any]]:
        """Parse SSE body and return JSON payloads."""
        result: list[dict[str, Any]] = []
        for line in sse_body.splitlines():
            if not line.startswith("data:"):
                continue
            raw = line[len("data:") :].strip()
            try:
                result.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return result

    async def test_backfill_delivers_user_message_on_reconnect(
        self, app_client: Any, tmp_workspace: Any
    ) -> None:
        """A UserMessageCommittedEvent published before subscribe is replayed."""
        from yukar.events import bus as event_bus
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        project_id = "p-umb"
        epic_id = "EP-UMB"
        thread_id = "manager"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="ep-umb", title="UMB"))

        # Publish before any subscriber.
        pre_ev = _user_msg_event(
            thread_id=thread_id,
            text="pre-connect message",
            message_id=0,
            project_id=project_id,
            epic_id=epic_id,
        )
        event_bus.publish(project_id, epic_id, pre_ev)

        original_subscribe = event_bus.subscribe

        @asynccontextmanager
        async def _patched_subscribe(
            project_id: str, epic_id: str, maxsize: int = 256
        ) -> AsyncGenerator[asyncio.Queue[Any]]:
            async with original_subscribe(project_id, epic_id, maxsize) as q:
                q.put_nowait(None)  # terminate stream immediately after backfill
                yield q

        event_bus.subscribe = _patched_subscribe  # type: ignore[assignment]
        try:
            resp = await asyncio.wait_for(
                app_client.get(
                    f"/api/projects/{project_id}/epics/{epic_id}/threads/{thread_id}/stream"
                ),
                timeout=5.0,
            )
        finally:
            event_bus.subscribe = original_subscribe  # type: ignore[assignment]

        assert resp.status_code == 200
        events = self._parse_sse_events(resp.text)
        types = [ev.get("type") for ev in events]
        assert "user_message_committed" in types, (
            f"user_message_committed must be in backfill. Events: {events}"
        )
        ume = next(ev for ev in events if ev.get("type") == "user_message_committed")
        assert ume["text"] == "pre-connect message"
        assert ume["message_id"] == 0

    async def test_backfill_does_not_duplicate_live_event(
        self, app_client: Any, tmp_workspace: Any
    ) -> None:
        """A UserMessageCommittedEvent in both backfill and live queue is delivered once."""
        from yukar.events import bus as event_bus
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        project_id = "p-umb2"
        epic_id = "EP-UMB2"
        thread_id = "manager"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="ep-umb2", title="UMB2"))

        original_subscribe = event_bus.subscribe

        @asynccontextmanager
        async def _patched_subscribe(
            project_id: str, epic_id: str, maxsize: int = 256
        ) -> AsyncGenerator[asyncio.Queue[Any]]:
            async with original_subscribe(project_id, epic_id, maxsize) as q:
                # Publish a boundary event after subscribe (goes into both buffer + queue).
                boundary_ev = _user_msg_event(
                    thread_id=thread_id,
                    text="boundary-msg",
                    message_id=1,
                    project_id=project_id,
                    epic_id=epic_id,
                )
                event_bus.publish(project_id, epic_id, boundary_ev)
                q.put_nowait(None)
                yield q

        event_bus.subscribe = _patched_subscribe  # type: ignore[assignment]
        try:
            resp = await asyncio.wait_for(
                app_client.get(
                    f"/api/projects/{project_id}/epics/{epic_id}/threads/{thread_id}/stream"
                ),
                timeout=5.0,
            )
        finally:
            event_bus.subscribe = original_subscribe  # type: ignore[assignment]

        assert resp.status_code == 200
        events = self._parse_sse_events(resp.text)
        ume_events = [ev for ev in events if ev.get("type") == "user_message_committed"]
        assert len(ume_events) == 1, (
            f"boundary user_message_committed must appear exactly once; got {ume_events}"
        )

    async def test_backfill_filtered_to_thread(self, app_client: Any, tmp_workspace: Any) -> None:
        """User messages from a different thread must not appear in the stream."""
        from yukar.events import bus as event_bus
        from yukar.models.epic import Epic
        from yukar.models.project import Project
        from yukar.storage.epic_repo import save_epic
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        project_id = "p-umb3"
        epic_id = "EP-UMB3"

        await save_project(root, Project(id=project_id, name=project_id))
        await save_epic(root, project_id, Epic(id=epic_id, slug="ep-umb3", title="UMB3"))

        # Publish message for "manager" thread.
        ev_manager = _user_msg_event(
            thread_id="manager",
            text="manager-text",
            message_id=0,
            project_id=project_id,
            epic_id=epic_id,
        )
        # Publish message for "other" thread.
        ev_other = _user_msg_event(
            thread_id="other",
            text="other-text",
            message_id=0,
            project_id=project_id,
            epic_id=epic_id,
        )
        event_bus.publish(project_id, epic_id, ev_manager)
        event_bus.publish(project_id, epic_id, ev_other)

        original_subscribe = event_bus.subscribe

        @asynccontextmanager
        async def _patched_subscribe(
            project_id: str, epic_id: str, maxsize: int = 256
        ) -> AsyncGenerator[asyncio.Queue[Any]]:
            async with original_subscribe(project_id, epic_id, maxsize) as q:
                q.put_nowait(None)
                yield q

        event_bus.subscribe = _patched_subscribe  # type: ignore[assignment]
        try:
            # Subscribe to the "manager" thread stream.
            resp = await asyncio.wait_for(
                app_client.get(
                    f"/api/projects/{project_id}/epics/{epic_id}/threads/manager/stream"
                ),
                timeout=5.0,
            )
        finally:
            event_bus.subscribe = original_subscribe  # type: ignore[assignment]

        assert resp.status_code == 200
        events = self._parse_sse_events(resp.text)
        texts = [ev.get("text") for ev in events if ev.get("type") == "user_message_committed"]
        assert "manager-text" in texts
        assert "other-text" not in texts
