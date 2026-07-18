"""Unit tests for the epic screenshot storage layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from yukar.config import paths
from yukar.storage import screenshots_repo

_JPEG = b"\xff\xd8\xff\xe0body"


async def test_save_lands_under_epic_docs(tmp_path: Path) -> None:
    root = str(tmp_path)
    name = await screenshots_repo.save_epic_screenshot(root, "p", "e1", _JPEG, label="Login Page")
    directory = paths.epic_screenshots_dir(root, "p", "e1")
    assert directory == paths.epic_docs_dir(root, "p", "e1") / "screenshots"
    assert (directory / name).read_bytes() == _JPEG
    # Label is slugified into the filename.
    assert "login-page" in name
    assert name.endswith(".jpg")


async def test_save_never_clobbers_same_second(tmp_path: Path) -> None:
    root = str(tmp_path)
    first = await screenshots_repo.save_epic_screenshot(root, "p", "e1", _JPEG, label="x")
    second = await screenshots_repo.save_epic_screenshot(
        root, "p", "e1", b"\xff\xd8other", label="x"
    )
    assert first != second
    metas = screenshots_repo.list_epic_screenshots(root, "p", "e1")
    assert {m.filename for m in metas} == {first, second}


def test_list_empty_when_no_directory(tmp_path: Path) -> None:
    assert screenshots_repo.list_epic_screenshots(str(tmp_path), "p", "e1") == []


def test_read_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        screenshots_repo.read_epic_screenshot(str(tmp_path), "p", "e1", "missing.jpg")


@pytest.mark.parametrize("bad", ["../secret.jpg", "sub/dir.jpg", "notes.txt", ".hidden.jpg"])
def test_unsafe_or_non_image_names_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        screenshots_repo.read_epic_screenshot(str(tmp_path), "p", "e1", bad)


def test_delete_reports_absence(tmp_path: Path) -> None:
    assert screenshots_repo.delete_epic_screenshot(str(tmp_path), "p", "e1", "gone.jpg") is False


def test_media_type_by_suffix() -> None:
    assert screenshots_repo.media_type_for("a.jpg") == "image/jpeg"
    assert screenshots_repo.media_type_for("a.JPEG") == "image/jpeg"
    assert screenshots_repo.media_type_for("a.png") == "image/png"
