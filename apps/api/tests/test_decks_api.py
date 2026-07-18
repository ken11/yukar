"""Decks repo + router — listing, download, previews, and path validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient

from yukar.config import paths
from yukar.storage import decks_repo


async def _setup_epic(client: AsyncClient) -> str:
    await client.post("/api/projects", json={"id": "proj", "name": "Proj", "repos": []})
    r = await client.post("/api/projects/proj/epics", json={"title": "Epic"})
    return r.json()["id"]


def _write_deck(
    root: Path, epic_id: str, rel: str, *, previews: int = 0
) -> Path:
    docs = paths.epic_docs_dir(str(root), "proj", epic_id)
    deck = docs / rel
    deck.parent.mkdir(parents=True, exist_ok=True)
    deck.write_bytes(b"PK-fake-pptx")
    directory = decks_repo.previews_dir_for(deck)
    for i in range(1, previews + 1):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"slide-{i:02d}.jpg").write_bytes(b"\xff\xd8fake")
    return deck


class TestDecksRepo:
    async def test_save_previews_replaces_stale_slides(self, tmp_path: Path) -> None:
        deck = tmp_path / "deck.pptx"
        deck.write_bytes(b"PK")
        names = await decks_repo.save_deck_previews(deck, [b"a", b"b", b"c"])
        assert names == ["slide-01.jpg", "slide-02.jpg", "slide-03.jpg"]
        # A shrunken deck must not leave slide-03 behind.
        names = await decks_repo.save_deck_previews(deck, [b"x"])
        assert names == ["slide-01.jpg"]
        directory = decks_repo.previews_dir_for(deck)
        assert sorted(p.name for p in directory.iterdir()) == ["slide-01.jpg"]

    def test_resolve_rejects_escape_and_non_pptx(self, tmp_path: Path) -> None:
        root = str(tmp_path)
        with pytest.raises(ValueError):
            decks_repo.read_epic_deck(root, "proj", "e", "../../../etc/passwd.pptx")
        with pytest.raises(ValueError):
            decks_repo.read_epic_deck(root, "proj", "e", "notes.md")


class TestDecksApi:
    async def test_list_empty(self, app_client: AsyncClient) -> None:
        epic_id = await _setup_epic(app_client)
        r = await app_client.get(f"/api/projects/proj/epics/{epic_id}/decks")
        assert r.status_code == 200
        assert r.json() == []

    async def test_list_download_and_previews(
        self, app_client: AsyncClient, tmp_workspace: Path
    ) -> None:
        epic_id = await _setup_epic(app_client)
        _write_deck(tmp_workspace, epic_id, "report.pptx", previews=2)
        _write_deck(tmp_workspace, epic_id, "sub/other.pptx")

        r = await app_client.get(f"/api/projects/proj/epics/{epic_id}/decks")
        assert r.status_code == 200
        decks = {d["path"]: d for d in r.json()}
        assert set(decks) == {"report.pptx", "sub/other.pptx"}
        assert decks["report.pptx"]["previews"] == ["slide-01.jpg", "slide-02.jpg"]
        assert decks["sub/other.pptx"]["previews"] == []

        dl = await app_client.get(
            f"/api/projects/proj/epics/{epic_id}/decks/content",
            params={"path": "report.pptx"},
        )
        assert dl.status_code == 200
        assert dl.content == b"PK-fake-pptx"
        assert dl.headers["content-type"].startswith(decks_repo.PPTX_MEDIA_TYPE)
        assert "attachment" in dl.headers["content-disposition"]

        pv = await app_client.get(
            f"/api/projects/proj/epics/{epic_id}/decks/preview",
            params={"path": "report.pptx", "name": "slide-01.jpg"},
        )
        assert pv.status_code == 200
        assert pv.content.startswith(b"\xff\xd8")
        assert pv.headers["content-type"] == "image/jpeg"

    async def test_validation_and_missing(
        self, app_client: AsyncClient, tmp_workspace: Path
    ) -> None:
        epic_id = await _setup_epic(app_client)
        _write_deck(tmp_workspace, epic_id, "report.pptx", previews=1)

        escape = await app_client.get(
            f"/api/projects/proj/epics/{epic_id}/decks/content",
            params={"path": "../../../../secret.pptx"},
        )
        assert escape.status_code == 422
        not_pptx = await app_client.get(
            f"/api/projects/proj/epics/{epic_id}/decks/content",
            params={"path": "plan.md"},
        )
        assert not_pptx.status_code == 422
        missing = await app_client.get(
            f"/api/projects/proj/epics/{epic_id}/decks/content",
            params={"path": "nope.pptx"},
        )
        assert missing.status_code == 404
        bad_name = await app_client.get(
            f"/api/projects/proj/epics/{epic_id}/decks/preview",
            params={"path": "report.pptx", "name": "../../x.jpg"},
        )
        assert bad_name.status_code == 422
        missing_preview = await app_client.get(
            f"/api/projects/proj/epics/{epic_id}/decks/preview",
            params={"path": "report.pptx", "name": "slide-09.jpg"},
        )
        assert missing_preview.status_code == 404
