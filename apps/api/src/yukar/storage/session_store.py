"""Strands-compatible session store.

Reads and writes the FileSessionManager layout directly,
matching the on-disk format produced by strands.session.FileSessionManager.

Layout (spec §4.1, confirmed against strands-agents==1.42.0):
  sessions/session_{epic-id}/
    session.json          — {"session_id", "session_type", "created_at", "updated_at"}
    agents/
      agent_{agent-id}/
        agent.json        — {"agent_id", "state", "conversation_manager_state",
                             "_internal_state", "created_at", "updated_at"}
        messages/
          message_0.json  — {"message": {"role", "content"}, "message_id",
                             "redact_message", "created_at", "updated_at"}
          ...

Compatibility notes:
- Session.from_dict / SessionAgent.from_dict / SessionMessage.from_dict all use
  inspect.signature(cls).parameters and ignore unknown keys, so extra keys in our
  files are silently dropped when Strands reads them.
- Our reader (list_messages) likewise uses .get() with defaults,
  so Strands-written files with extra fields are safe to read here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from yukar.config import paths
from yukar.models.message import ContentPart, Message, MessagePayload
from yukar.storage.atomic import _lock_for, atomic_write_text

# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


async def ensure_session(root: str, project_id: str, epic_id: str) -> None:
    """Create session directory and session.json if not present.

    Writes a format compatible with strands.types.session.Session.from_dict.
    """
    s_dir = paths.session_dir(root, project_id, epic_id)
    s_dir.mkdir(parents=True, exist_ok=True)
    # Also create the agents/ subdir that FileSessionManager expects.
    # (No multi_agents/ — the Agent-as-a-Tool design uses a single Manager
    # Agent session, not a Strands Graph/Swarm; spec §4.1/§6.5.)
    (s_dir / "agents").mkdir(exist_ok=True)
    s_json = paths.session_json(root, project_id, epic_id)
    if not s_json.exists():
        now = datetime.now(UTC).isoformat()
        data: dict[str, Any] = {
            "session_id": epic_id,
            "session_type": "AGENT",
            "created_at": now,
            "updated_at": now,
        }
        await atomic_write_text(s_json, json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


async def ensure_agent(
    root: str,
    project_id: str,
    epic_id: str,
    agent_id: str,
    state: dict[str, Any] | None = None,
) -> None:
    """Create agent directory and agent.json if not present.

    Writes a format compatible with strands.types.session.SessionAgent.from_dict.
    """
    await ensure_session(root, project_id, epic_id)
    a_dir = paths.agent_dir(root, project_id, epic_id, agent_id)
    a_dir.mkdir(parents=True, exist_ok=True)
    msg_dir = paths.messages_dir(root, project_id, epic_id, agent_id)
    msg_dir.mkdir(parents=True, exist_ok=True)
    a_json = paths.agent_json(root, project_id, epic_id, agent_id)
    if not a_json.exists():
        now = datetime.now(UTC).isoformat()
        data: dict[str, Any] = {
            "agent_id": agent_id,
            "state": state or {},
            "conversation_manager_state": {},
            "_internal_state": {},
            "created_at": now,
            "updated_at": now,
        }
        await atomic_write_text(a_json, json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _next_message_index(msg_dir: Path) -> int:
    """Return the next message index (count of existing message_*.json files)."""
    existing = list(msg_dir.glob("message_*.json"))
    if not existing:
        return 0
    indices = []
    for f in existing:
        try:
            idx = int(f.stem.split("_", 1)[1])
            indices.append(idx)
        except (ValueError, IndexError):
            pass
    return max(indices) + 1 if indices else 0


async def append_message(
    root: str,
    project_id: str,
    epic_id: str,
    agent_id: str,
    role: Literal["user", "assistant"],
    text: str,
) -> Message:
    """Append a new message to an agent's message list.

    The index probe and the write are serialised under a per-messages_dir lock
    so that two concurrent callers cannot claim the same index.
    """
    await ensure_agent(root, project_id, epic_id, agent_id)
    msg_dir = paths.messages_dir(root, project_id, epic_id, agent_id)
    lock = _lock_for(msg_dir)

    async with lock:
        idx = _next_message_index(msg_dir)
        now = datetime.now(UTC)
        msg = Message(
            message=MessagePayload(
                role=role,
                content=[ContentPart(text=text)],
            ),
            message_id=idx,
            created_at=now,
        )
        # Write in Strands SessionMessage.to_dict() compatible format.
        # SessionMessage.from_dict ignores unknown keys, so extra fields in stored
        # files are safe to read back.
        now_str = now.isoformat()
        raw: dict[str, Any] = {
            "message": {
                "role": role,
                "content": [{"text": text}],
            },
            "message_id": idx,
            "redact_message": None,
            "created_at": now_str,
            "updated_at": now_str,
        }
        msg_path = paths.message_json(root, project_id, epic_id, agent_id, idx)
        await atomic_write_text(msg_path, json.dumps(raw, indent=2))
    return msg


_MAX_PERSIST_BLOCK_CHARS = 8192


def _truncate_text(s: str, limit: int) -> str:
    """Truncate *s* to *limit* chars with a marker (keeps session files bounded)."""
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n…[truncated {len(s) - limit} chars]"


def _sanitize_input(value: Any, limit: int) -> Any:
    """Recursively truncate long string values inside a toolUse input."""
    if isinstance(value, str):
        return _truncate_text(value, limit)
    if isinstance(value, dict):
        return {k: _sanitize_input(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_input(v, limit) for v in value]
    return value


def _sanitize_block(block: dict[str, Any], limit: int) -> dict[str, Any]:
    """Sanitize one Strands content block (text / toolUse / toolResult), bounding size.

    Keeps the raw Strands camelCase shape so ``list_messages`` / the frontend
    (``strandsMessagesToThreadMessageLikes``) can render it unchanged.
    """
    if isinstance(block.get("text"), str):
        return {"text": _truncate_text(block["text"], limit)}
    if isinstance(block.get("toolUse"), dict):
        tu = block["toolUse"]
        return {
            "toolUse": {
                "toolUseId": tu.get("toolUseId", ""),
                "name": tu.get("name", ""),
                "input": _sanitize_input(tu.get("input", {}), limit),
            }
        }
    if isinstance(block.get("toolResult"), dict):
        tr = block["toolResult"]
        raw_content = tr.get("content", [])
        new_content: list[Any] = []
        if isinstance(raw_content, list):
            for c in raw_content:
                if isinstance(c, dict) and isinstance(c.get("text"), str):
                    new_content.append({"text": _truncate_text(c["text"], limit)})
                else:
                    new_content.append(c)
        return {
            "toolResult": {
                "toolUseId": tr.get("toolUseId", ""),
                "status": tr.get("status"),
                "content": new_content,
            }
        }
    return block


async def persist_agent_messages(
    root: str,
    project_id: str,
    epic_id: str,
    agent_id: str,
    messages: list[Any],  # Strands agent.messages (list of Message TypedDicts)
    *,
    max_block_chars: int = _MAX_PERSIST_BLOCK_CHARS,
) -> None:
    """Persist an agent's full conversation (Strands ``agent.messages``) verbatim.

    Worker/Evaluator agents have no ``FileSessionManager`` (§6.4), so their tool-use
    activity (toolUse / toolResult blocks and reasoning text) otherwise survives only
    on the live SSE stream.  Writing ``agent.messages`` after the run lets the thread
    retain the full activity log on reload — rendered as one bubble per utterance by
    the same path the Manager uses.

    Each message becomes ``message_<N>.json`` (N = list index), in the Strands format
    read by :func:`list_messages`.  Text / toolResult content is truncated to
    *max_block_chars* to bound file size.  A no-op when *messages* is empty.
    """
    if not messages:
        return
    await ensure_agent(root, project_id, epic_id, agent_id)
    msg_dir = paths.messages_dir(root, project_id, epic_id, agent_id)
    lock = _lock_for(msg_dir)
    async with lock:
        for idx, raw in enumerate(messages):
            role = raw.get("role", "assistant")
            if role not in ("user", "assistant"):
                role = "assistant"
            raw_content = raw.get("content", [])
            sanitized = (
                [_sanitize_block(b, max_block_chars) for b in raw_content if isinstance(b, dict)]
                if isinstance(raw_content, list)
                else []
            )
            now_str = datetime.now(UTC).isoformat()
            data: dict[str, Any] = {
                "message": {"role": role, "content": sanitized},
                "message_id": idx,
                "redact_message": None,
                "created_at": now_str,
                "updated_at": now_str,
            }
            msg_path = paths.message_json(root, project_id, epic_id, agent_id, idx)
            await atomic_write_text(msg_path, json.dumps(data, indent=2))


def list_messages(root: str, project_id: str, epic_id: str, agent_id: str) -> list[Message]:
    """Return messages in order (synchronous read)."""
    msg_dir = paths.messages_dir(root, project_id, epic_id, agent_id)
    if not msg_dir.exists():
        return []
    files = sorted(
        msg_dir.glob("message_*.json"),
        key=lambda f: int(f.stem.split("_", 1)[1]),
    )
    result: list[Message] = []
    for f in files:
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            result.append(Message.model_validate(raw))
        except Exception:
            continue
    return result


