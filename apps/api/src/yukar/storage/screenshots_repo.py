"""Epic screenshot storage — browser-verification captures the agent kept.

Unlike docs (markdown text under ``epic/docs/*.md``), screenshots are binary
JPEG files under ``epic/docs/screenshots/``.  A file is written ONLY when an
agent passes ``save=True`` to ``browser_screenshot`` — saving every shot would
waste disk, so the LLM decides which are worth keeping.

Writes go through the atomic bytes helper (temp → fsync → os.replace); reads
and listing are plain synchronous stats, matching ``docs_repo``.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

from yukar.config import paths
from yukar.storage.atomic import atomic_write_bytes

# Screenshot filenames embed a human-readable capture time; JST matches the
# rest of the user-facing timestamps (memory records, usage days).
_JST = ZoneInfo("Asia/Tokyo")

_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")
_LABEL_MAX = 40

# Filename allocation is check-then-act (exists() loop → write); two agents
# saving in the same JST second with the same label would otherwise compute
# the same name and the later os.replace would clobber the earlier capture.
# One process-wide lock serialises allocation+write (saves are rare).
_save_lock = asyncio.Lock()


@dataclass(frozen=True, slots=True)
class ScreenshotMeta:
    """One saved screenshot, as surfaced to the docs page."""

    filename: str
    size_bytes: int
    captured_at: str  # ISO-8601 (JST), from the file's mtime


def _safe_screenshot_name(filename: str) -> str:
    """Validate a caller-supplied screenshot filename (no path traversal)."""
    pure = PurePosixPath(filename)
    if len(pure.parts) != 1:
        raise ValueError(f"Invalid screenshot filename (path traversal): {filename!r}")
    name = pure.name
    if name.startswith(".") or name.startswith("/"):
        raise ValueError(f"Invalid screenshot filename: {filename!r}")
    if pure.suffix.lower() not in _IMAGE_SUFFIXES:
        raise ValueError(
            f"Screenshot filename must end with one of {_IMAGE_SUFFIXES}: {filename!r}"
        )
    return name


def _slugify(label: str) -> str:
    """Collapse an agent-supplied label into a filesystem-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return slug[:_LABEL_MAX] or "shot"


def media_type_for(filename: str) -> str:
    """MIME type for serving a saved screenshot by its suffix."""
    return "image/png" if filename.lower().endswith(".png") else "image/jpeg"


async def save_epic_screenshot(
    root: str,
    project_id: str,
    epic_id: str,
    data: bytes,
    *,
    label: str | None = None,
) -> str:
    """Persist JPEG bytes under the epic docs folder; return the filename.

    The name is ``{YYYYMMDD-HHMMSS}-{label-slug}.jpg`` in JST; a numeric
    suffix is appended when two shots land in the same second so an earlier
    capture is never clobbered.
    """
    slug = _slugify(label or "shot")
    stamp = datetime.now(_JST).strftime("%Y%m%d-%H%M%S")
    directory = paths.epic_screenshots_dir(root, project_id, epic_id)
    async with _save_lock:
        base = f"{stamp}-{slug}"
        filename = f"{base}.jpg"
        counter = 2
        while (directory / filename).exists():
            filename = f"{base}-{counter}.jpg"
            counter += 1
        path = paths.epic_screenshot_path(root, project_id, epic_id, filename)
        await atomic_write_bytes(path, data)
    return filename


def list_epic_screenshots(root: str, project_id: str, epic_id: str) -> list[ScreenshotMeta]:
    """All saved screenshots for the epic, newest first (empty when none)."""
    directory = paths.epic_screenshots_dir(root, project_id, epic_id)
    if not directory.exists():
        return []
    metas: list[ScreenshotMeta] = []
    for p in directory.iterdir():
        if not p.is_file() or p.suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        st = p.stat()
        metas.append(
            ScreenshotMeta(
                filename=p.name,
                size_bytes=st.st_size,
                captured_at=datetime.fromtimestamp(st.st_mtime, tz=_JST).isoformat(),
            )
        )
    metas.sort(key=lambda m: (m.captured_at, m.filename), reverse=True)
    return metas


def read_epic_screenshot(root: str, project_id: str, epic_id: str, filename: str) -> bytes:
    """Raw bytes of one saved screenshot (raises FileNotFoundError if absent)."""
    safe = _safe_screenshot_name(filename)
    path = paths.epic_screenshot_path(root, project_id, epic_id, safe)
    if not path.exists():
        raise FileNotFoundError(f"Screenshot not found: {filename}")
    return path.read_bytes()


def delete_epic_screenshot(root: str, project_id: str, epic_id: str, filename: str) -> bool:
    """Delete one saved screenshot; return False if it was already gone."""
    safe = _safe_screenshot_name(filename)
    path = paths.epic_screenshot_path(root, project_id, epic_id, safe)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
