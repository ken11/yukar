"""Slide template storage — project-level reusable deck design bundles.

A template carries one epic's deck design to future epics: a renderable
definition (image paths rewritten to the bundle's ``assets/``), the images it
references, freeform design notes, and up to two thumbnail previews (the
cover often looks nothing like a body slide, so one is not enough).  Bundles
live under ``<project>/docs/slide-templates/<name>/`` — a subdirectory of the
project docs folder, so they are user-visible on the Docs page but NOT swept
into the Manager prompt by the top-level ``*.md`` glob.

Layout::

    <name>/
      template.slides.yaml   # the definition (assets/-relative image paths)
      template.yaml          # SlideTemplateInfo metadata
      notes.md               # optional design notes
      assets/                # referenced images (flat)
      previews/slide-NN.jpg  # thumbnails (at most MAX_TEMPLATE_THUMBNAILS)

Writes build the bundle in a dot-prefixed staging directory and swap it into
place under a process-wide lock, so a crash never leaves a half-written
template visible (dot-dirs are skipped by listing).
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from yukar.config import paths
from yukar.storage.yaml_io import load_model, save_model

logger = logging.getLogger(__name__)

# JST timestamps, matching screenshots_repo / decks_repo.
_JST = ZoneInfo("Asia/Tokyo")

DEFINITION_FILENAME = "template.slides.yaml"
META_FILENAME = "template.yaml"
NOTES_FILENAME = "notes.md"
ASSETS_DIRNAME = "assets"
PREVIEWS_DIRNAME = "previews"

# Cover + one body slide: the cover often has its own design, so a single
# thumbnail would misrepresent the template.
MAX_TEMPLATE_THUMBNAILS = 2

# ASCII slug: URL-safe path segment, no leading dot/dash (dot-dirs are
# staging/hidden, leading dash risks option injection).  Human-language
# titles belong in the description.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_write_lock = asyncio.Lock()


class SlideTemplateInfo(BaseModel):
    """Metadata stored as template.yaml inside the bundle."""

    description: str = ""
    slide_count: int = 0
    size: str = "16:9"
    created_at: str = ""
    source_epic: str = ""


@dataclass(frozen=True, slots=True)
class SlideTemplateMeta:
    """One template, as surfaced to tools and the Docs page."""

    name: str
    description: str
    slide_count: int
    size: str
    created_at: str
    previews: list[str]  # slide-NN.jpg filenames, in slide order
    has_notes: bool


def validate_template_name(name: str) -> str:
    """Return *name* if it is a safe template slug; raise ValueError otherwise."""
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid template name {name!r} — use 1-64 ASCII letters, digits, "
            "'.', '_' or '-', starting with a letter or digit."
        )
    return name


def _template_dir(root: str, project_id: str, name: str) -> Path:
    validate_template_name(name)
    return paths.slide_template_dir(root, project_id, name)


def _read_meta(directory: Path) -> SlideTemplateInfo | None:
    meta_path = directory / META_FILENAME
    if not meta_path.is_file() or not (directory / DEFINITION_FILENAME).is_file():
        return None
    return load_model(meta_path, SlideTemplateInfo, default=SlideTemplateInfo())


def _preview_names(directory: Path) -> list[str]:
    previews = directory / PREVIEWS_DIRNAME
    if not previews.is_dir():
        return []
    return sorted(p.name for p in previews.iterdir() if p.suffix.lower() == ".jpg")


def list_templates(root: str, project_id: str) -> list[SlideTemplateMeta]:
    """All valid template bundles, newest first (empty when none)."""
    base = paths.slide_templates_dir(root, project_id)
    if not base.is_dir():
        return []
    metas: list[SlideTemplateMeta] = []
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or not _NAME_RE.match(d.name):
            continue
        try:
            info = _read_meta(d)
        except Exception:
            # Bundles live under the user-visible docs tree, so a hand-edited
            # template.yaml is plausible — one corrupt bundle must only hide
            # itself, never abort the whole listing (the "EP-6 disappeared"
            # bug class; same policy as yaml_io.load_validated_dir).
            logger.warning("Skipping unreadable slide template %s", d.name, exc_info=True)
            continue
        if info is None:  # half-formed leftovers never surface
            continue
        metas.append(
            SlideTemplateMeta(
                name=d.name,
                description=info.description,
                slide_count=info.slide_count,
                size=info.size,
                created_at=info.created_at,
                previews=_preview_names(d),
                has_notes=(d / NOTES_FILENAME).is_file(),
            )
        )
    metas.sort(key=lambda m: (m.created_at, m.name), reverse=True)
    return metas


def template_exists(root: str, project_id: str, name: str) -> bool:
    return (_template_dir(root, project_id, name) / DEFINITION_FILENAME).is_file()


def read_template_definition(root: str, project_id: str, name: str) -> str:
    path = _template_dir(root, project_id, name) / DEFINITION_FILENAME
    if not path.is_file():
        raise FileNotFoundError(f"Template not found: {name}")
    return path.read_text("utf-8")


def read_template_meta(root: str, project_id: str, name: str) -> SlideTemplateInfo:
    info = _read_meta(_template_dir(root, project_id, name))
    if info is None:
        raise FileNotFoundError(f"Template not found: {name}")
    return info


def read_template_notes(root: str, project_id: str, name: str) -> str | None:
    path = _template_dir(root, project_id, name) / NOTES_FILENAME
    if not path.is_file():
        return None
    return path.read_text("utf-8")


def list_template_assets(root: str, project_id: str, name: str) -> list[Path]:
    """Files directly under the bundle's assets/ directory (flat by design)."""
    assets = _template_dir(root, project_id, name) / ASSETS_DIRNAME
    if not assets.is_dir():
        return []
    return sorted(p for p in assets.iterdir() if p.is_file() and not p.name.startswith("."))


def list_template_previews(root: str, project_id: str, name: str) -> list[str]:
    """Thumbnail filenames of one bundle, in slide order (empty when none)."""
    return _preview_names(_template_dir(root, project_id, name))


def read_template_preview(root: str, project_id: str, name: str, filename: str) -> bytes:
    """One thumbnail JPEG (FileNotFoundError / ValueError on bad names)."""
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise ValueError(f"Invalid preview name: {filename!r}")
    if not filename.lower().endswith(".jpg"):
        raise ValueError(f"Invalid preview name: {filename!r}")
    path = _template_dir(root, project_id, name) / PREVIEWS_DIRNAME / filename
    if not path.is_file():
        raise FileNotFoundError(f"Preview not found: {filename}")
    return path.read_bytes()


def _build_bundle_sync(
    staging: Path,
    definition_text: str,
    notes: str,
    asset_files: list[tuple[Path, str]],
    previews: list[bytes],
) -> None:
    """Assemble everything except template.yaml in *staging* (runs in a thread).

    The metadata file is written last by the caller: list_templates treats a
    bundle without template.yaml as half-formed, so even a torn staging swap
    can never surface a partial template.
    """
    shutil.rmtree(staging, ignore_errors=True)
    (staging / ASSETS_DIRNAME).mkdir(parents=True, exist_ok=True)
    (staging / PREVIEWS_DIRNAME).mkdir(parents=True, exist_ok=True)
    (staging / DEFINITION_FILENAME).write_text(definition_text, encoding="utf-8")
    if notes:
        (staging / NOTES_FILENAME).write_text(notes, encoding="utf-8")
    for src, dest_name in asset_files:
        shutil.copyfile(src, staging / ASSETS_DIRNAME / dest_name)
    for i, shot in enumerate(previews[:MAX_TEMPLATE_THUMBNAILS], start=1):
        (staging / PREVIEWS_DIRNAME / f"slide-{i:02d}.jpg").write_bytes(shot)


async def save_template(
    root: str,
    project_id: str,
    name: str,
    *,
    definition_text: str,
    info: SlideTemplateInfo,
    notes: str = "",
    asset_files: list[tuple[Path, str]] | None = None,
    previews: list[bytes] | None = None,
    overwrite: bool = False,
) -> None:
    """Write (or with *overwrite* replace) a template bundle atomically-ish.

    Raises:
        ValueError: Invalid template name.
        FileExistsError: Name taken and *overwrite* is False.
    """
    final = _template_dir(root, project_id, name)
    staging = final.parent / f".staging-{name}"
    async with _write_lock:
        if final.exists() and not overwrite:
            raise FileExistsError(
                f"Template {name!r} already exists — pass overwrite=True to replace it."
            )
        try:
            await asyncio.to_thread(
                _build_bundle_sync,
                staging,
                definition_text,
                notes,
                asset_files or [],
                previews or [],
            )
            # Metadata last within staging, then the swap: a bundle missing
            # template.yaml is treated as half-formed by list_templates.
            await save_model(staging / META_FILENAME, info)

            def _swap() -> None:
                # Move the old bundle aside (atomic) BEFORE installing the new
                # one: destroying it first would lose BOTH versions if the
                # install rename then failed.
                trash = final.parent / f".trash-{name}"
                shutil.rmtree(trash, ignore_errors=True)
                had_old = final.exists()
                if had_old:
                    final.replace(trash)
                try:
                    staging.replace(final)
                except OSError:
                    if had_old:
                        trash.replace(final)  # restore the previous version
                    raise
                if had_old:
                    shutil.rmtree(trash, ignore_errors=True)

            await asyncio.to_thread(_swap)
        finally:
            await asyncio.to_thread(shutil.rmtree, staging, True)


async def delete_template(root: str, project_id: str, name: str) -> bool:
    """Remove a template bundle; returns False when it did not exist."""
    directory = _template_dir(root, project_id, name)
    async with _write_lock:
        if not directory.is_dir():
            return False
        await asyncio.to_thread(shutil.rmtree, directory)
        return True


def fresh_created_at() -> str:
    """ISO-8601 JST timestamp for new bundles (kept here so tools don't import zoneinfo)."""
    return datetime.now(tz=_JST).isoformat(timespec="seconds")
