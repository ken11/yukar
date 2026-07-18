"""Deck storage — Manager-rendered .pptx files plus their slide previews.

Decks are epic artifacts living inside the epic docs directory (any
subdirectory), written by the Manager's ``pptx_render`` tool.  Next to each
``<name>.pptx`` the tool keeps a ``<name>.previews/`` directory holding one
``slide-NN.jpg`` per slide from the last render that produced previews —
that is what the Docs page shows, and it is refreshed (cleared + rewritten)
on every such render.

Layout knowledge (which previews belong to which deck) lives here so the
tool that writes and the router that serves cannot drift apart.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from yukar.config import paths
from yukar.storage.atomic import atomic_write_bytes

# JST mtimes, matching screenshots_repo / user-facing timestamps.
_JST = ZoneInfo("Asia/Tokyo")

PPTX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

_PREVIEWS_SUFFIX = ".previews"


@dataclass(frozen=True, slots=True)
class DeckMeta:
    """One rendered deck, as surfaced to the docs page."""

    path: str  # docs-relative POSIX path of the .pptx
    size_bytes: int
    updated_at: str  # ISO-8601 (JST), from the file's mtime
    previews: list[str]  # slide-NN.jpg filenames, in slide order


def previews_dir_for(pptx_path: Path) -> Path:
    """The sibling directory holding this deck's slide previews."""
    return pptx_path.parent / (pptx_path.stem + _PREVIEWS_SUFFIX)


def _resolve_deck(root: str, project_id: str, epic_id: str, rel_path: str) -> Path:
    """Validate a docs-relative deck path; raises ValueError on bad input."""
    if not rel_path.endswith(".pptx"):
        raise ValueError(f"Not a deck path: {rel_path!r}")
    docs_dir = paths.epic_docs_dir(root, project_id, epic_id).resolve()
    resolved = (docs_dir / rel_path).resolve()
    if not resolved.is_relative_to(docs_dir):
        raise ValueError(f"Invalid deck path (escapes docs): {rel_path!r}")
    return resolved


def _preview_names(pptx_path: Path) -> list[str]:
    directory = previews_dir_for(pptx_path)
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.suffix.lower() == ".jpg")


def list_epic_decks(root: str, project_id: str, epic_id: str) -> list[DeckMeta]:
    """All rendered decks for the epic, newest first (empty when none)."""
    docs_dir = paths.epic_docs_dir(root, project_id, epic_id)
    if not docs_dir.exists():
        return []
    metas: list[DeckMeta] = []
    for p in sorted(docs_dir.rglob("*.pptx")):
        if not p.is_file():
            continue
        st = p.stat()
        metas.append(
            DeckMeta(
                path=str(PurePosixPath(p.relative_to(docs_dir))),
                size_bytes=st.st_size,
                updated_at=datetime.fromtimestamp(st.st_mtime, tz=_JST).isoformat(),
                previews=_preview_names(p),
            )
        )
    metas.sort(key=lambda m: (m.updated_at, m.path), reverse=True)
    return metas


def read_epic_deck(root: str, project_id: str, epic_id: str, rel_path: str) -> bytes:
    """Raw .pptx bytes (FileNotFoundError if absent, ValueError on bad path)."""
    resolved = _resolve_deck(root, project_id, epic_id, rel_path)
    if not resolved.is_file():
        raise FileNotFoundError(f"Deck not found: {rel_path}")
    return resolved.read_bytes()


def read_deck_preview(
    root: str, project_id: str, epic_id: str, rel_path: str, name: str
) -> bytes:
    """One slide preview JPEG of a deck (FileNotFoundError / ValueError)."""
    resolved = _resolve_deck(root, project_id, epic_id, rel_path)
    pure = PurePosixPath(name)
    if len(pure.parts) != 1 or pure.suffix.lower() != ".jpg" or name.startswith("."):
        raise ValueError(f"Invalid preview name: {name!r}")
    path = previews_dir_for(resolved) / pure.name
    if not path.is_file():
        raise FileNotFoundError(f"Preview not found: {name}")
    return path.read_bytes()


async def save_deck_previews(pptx_path: Path, shots: list[bytes]) -> list[str]:
    """Replace the deck's preview directory with fresh slide JPEGs.

    Called by ``pptx_render`` after a successful render that produced
    previews; clearing first means a shrunken deck never leaves stale
    trailing slides behind.  Returns the written filenames in slide order.
    """
    directory = previews_dir_for(pptx_path)
    await asyncio.to_thread(shutil.rmtree, directory, True)
    names: list[str] = []
    for i, shot in enumerate(shots, start=1):
        name = f"slide-{i:02d}.jpg"
        await atomic_write_bytes(directory / name, shot)
        names.append(name)
    return names
