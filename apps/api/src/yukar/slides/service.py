"""Deck rendering pipeline — parse, load images, render, preview, warn.

The tool layer stays thin: it supplies the definition text and a path-guarded
``ImageReader`` and receives a ``DeckRender``.  Every recoverable problem
(missing/oversized/broken image, unavailable preview engine, measured text
overflow, out-of-bounds elements) becomes a warning string with a stable
``category:`` prefix; only an invalid definition (``DeckError``) aborts the
render.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from PIL import Image

from yukar.slides.preview import (
    ElementOverflow,
    PreviewUnavailableError,
    render_slide_previews,
)
from yukar.slides.render_html import mime_for, render_html
from yukar.slides.render_pptx import LoadedImage, image_key, render_pptx
from yukar.slides.schema import Deck, ImageElement, bounds_warnings, load_deck

MAX_IMAGE_BYTES = 8 * 1024 * 1024
# Aggregate ceilings across one deck — per-image caps alone leave the total
# unbounded (6000 image elements x 8 MB).  Beyond these, remaining images
# degrade to placeholders + warnings instead of growing host RSS.
MAX_IMAGE_COUNT = 200
MAX_TOTAL_IMAGE_BYTES = 64 * 1024 * 1024

ImageReader = Callable[[str], Awaitable[bytes]]
"""Resolve a definition image path to raw bytes.

Implementations enforce their own sandbox (the tool layer resolves through
``PathGuard``); any exception is converted into an ``image:`` warning and a
placeholder box, never a failed render.
"""


@dataclass(frozen=True, slots=True)
class DeckRender:
    """Everything one render produced."""

    deck: Deck
    pptx_bytes: bytes
    previews: list[bytes]  # one JPEG per slide; empty when preview skipped/failed
    overflows: list[ElementOverflow]
    warnings: list[str]


def _probe_size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as im:
        return im.size


def _image_paths(deck: Deck) -> dict[str, str]:
    """Distinct images as {canonical key: first spelling seen} (ordered).

    Keying on ``image_key`` collapses spelling variants of the same file so
    it is read once and held once, however many elements reference it.
    """
    seen: dict[str, str] = {}
    for slide in deck.slides:
        for el in slide.elements:
            if isinstance(el, ImageElement):
                seen.setdefault(image_key(el.path), el.path)
    return seen


async def _load_images(
    deck: Deck, reader: ImageReader
) -> tuple[dict[str, LoadedImage], list[str]]:
    images: dict[str, LoadedImage] = {}
    warnings: list[str] = []
    paths = _image_paths(deck)
    if len(paths) > MAX_IMAGE_COUNT:
        warnings.append(
            f"image: deck references {len(paths)} distinct images (max "
            f"{MAX_IMAGE_COUNT}); the rest render as placeholders"
        )
    total_bytes = 0
    for key, path in list(paths.items())[:MAX_IMAGE_COUNT]:
        if mime_for(path) is None:
            warnings.append(
                f"image: {path!r} has an unsupported format (use .png / .jpg / .gif)"
            )
            continue
        try:
            data = await reader(path)
        except Exception as exc:
            warnings.append(f"image: {path!r} could not be read: {exc}")
            continue
        if len(data) > MAX_IMAGE_BYTES:
            warnings.append(
                f"image: {path!r} is {len(data) / (1024 * 1024):.1f} MB — "
                f"max {MAX_IMAGE_BYTES / (1024 * 1024):.0f} MB; resize it first"
            )
            continue
        if total_bytes + len(data) > MAX_TOTAL_IMAGE_BYTES:
            warnings.append(
                f"image: {path!r} skipped — total image budget "
                f"({MAX_TOTAL_IMAGE_BYTES / (1024 * 1024):.0f} MB per deck) exceeded"
            )
            continue
        try:
            width, height = await asyncio.to_thread(_probe_size, data)
        except Exception:
            warnings.append(f"image: {path!r} is not a readable image file")
            continue
        total_bytes += len(data)
        images[key] = LoadedImage(data=data, width=width, height=height)
    return images, warnings


async def render_deck(
    definition_text: str,
    *,
    image_reader: ImageReader,
    with_preview: bool = True,
) -> DeckRender:
    """Render a YAML definition to pptx bytes + per-slide previews + warnings.

    Raises:
        DeckError: When the definition does not parse or validate; everything
            else degrades to warnings.
    """
    # Parse + validate off the event loop: ruamel's pure-Python parser and
    # pydantic validation take seconds on a large definition, and this
    # process serves every run and SSE stream on one loop.
    deck = await asyncio.to_thread(load_deck, definition_text)
    warnings = bounds_warnings(deck)
    images, image_warnings = await _load_images(deck, image_reader)
    warnings.extend(image_warnings)

    def _render_both() -> tuple[bytes, str]:
        return render_pptx(deck, images), render_html(deck, images)

    pptx_bytes, html = await asyncio.to_thread(_render_both)

    previews: list[bytes] = []
    overflows: list[ElementOverflow] = []
    if with_preview:
        try:
            previews, overflows = await render_slide_previews(
                html, len(deck.slides), deck.canvas
            )
        except PreviewUnavailableError as exc:
            warnings.append(f"preview: {exc}")
        except Exception as exc:
            warnings.append(f"preview: rendering failed: {exc}")
        for o in overflows:
            warnings.append(
                f"overflow: slide {o.slide} element {o.element} — text is about "
                f"{o.overflow_px}px taller than its box; shrink the text or grow the box"
            )
    return DeckRender(
        deck=deck,
        pptx_bytes=pptx_bytes,
        previews=previews,
        overflows=overflows,
        warnings=warnings,
    )
