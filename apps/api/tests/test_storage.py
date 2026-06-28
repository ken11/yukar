"""Tests for storage layer: yaml round-trip, atomic writes, session_store."""

from __future__ import annotations

from pathlib import Path


class TestYamlRoundTrip:
    async def test_write_and_read(self, tmp_path: Path) -> None:
        from yukar.storage.yaml_io import read_yaml, write_yaml

        path = tmp_path / "test.yaml"
        data = {"key": "value", "number": 42, "nested": {"a": [1, 2, 3]}}
        await write_yaml(path, data)
        result = read_yaml(path)
        assert result == data

    async def test_read_nonexistent_returns_empty(self, tmp_path: Path) -> None:
        from yukar.storage.yaml_io import read_yaml

        result = read_yaml(tmp_path / "nonexistent.yaml")
        assert result == {}

    async def test_atomic_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        from yukar.storage.atomic import atomic_write_text

        path = tmp_path / "a" / "b" / "c" / "file.txt"
        await atomic_write_text(path, "hello")
        assert path.read_text() == "hello"

    async def test_atomic_write_overwrites(self, tmp_path: Path) -> None:
        from yukar.storage.atomic import atomic_write_text

        path = tmp_path / "file.txt"
        await atomic_write_text(path, "first")
        await atomic_write_text(path, "second")
        assert path.read_text() == "second"


class TestSessionStore:
    async def test_ensure_session_creates_dirs(self, tmp_workspace: Path) -> None:
        from yukar.config import paths
        from yukar.storage.session_store import ensure_session

        root = str(tmp_workspace)
        await ensure_session(root, "proj1", "EP-1")

        assert paths.session_dir(root, "proj1", "EP-1").exists()
        assert paths.session_json(root, "proj1", "EP-1").exists()

    async def test_append_and_list_messages(self, tmp_workspace: Path) -> None:
        from yukar.storage.session_store import append_message, list_messages

        root = str(tmp_workspace)
        await append_message(root, "proj1", "EP-1", "th-001", "user", "Hello")
        await append_message(root, "proj1", "EP-1", "th-001", "assistant", "Hi")

        messages = list_messages(root, "proj1", "EP-1", "th-001")
        assert len(messages) == 2
        assert messages[0].message.role == "user"
        assert messages[0].message.content[0].text == "Hello"
        assert messages[1].message.role == "assistant"
        assert messages[1].message_id == 1

    async def test_ensure_agent_creates_agent_dir(self, tmp_workspace: Path) -> None:
        from yukar.config import paths
        from yukar.storage.session_store import ensure_agent

        root = str(tmp_workspace)
        await ensure_agent(root, "proj1", "EP-1", "manager")
        await ensure_agent(root, "proj1", "EP-1", "worker-1")

        assert paths.agent_dir(root, "proj1", "EP-1", "manager").exists()
        assert paths.agent_dir(root, "proj1", "EP-1", "worker-1").exists()

    async def test_ensure_agent_persists_state(self, tmp_workspace: Path) -> None:
        import json

        from yukar.config import paths
        from yukar.storage.session_store import ensure_agent

        root = str(tmp_workspace)
        state = {"role": "worker", "repo": "my-repo"}
        await ensure_agent(root, "proj1", "EP-1", "th-abc", state)

        a_json = paths.agent_json(root, "proj1", "EP-1", "th-abc")
        raw = json.loads(a_json.read_text(encoding="utf-8"))
        assert raw["state"] == state
