"""Structured edits on deck definition YAML — subset slides, remap image paths.

The slide-template feature moves a definition between an epic's docs folder
and the project-level template store, which requires two mechanical edits:
keeping only the exemplar slides, and rewriting image paths to the bundle's
``assets/`` layout (and back).  Both edits operate on the YAML text with a
ruamel round-trip so the author's comments, anchors, and scalar styles
survive; the caller re-validates the result with ``load_deck`` afterwards.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from yukar.slides.schema import DeckError


def _make_rt_yaml() -> YAML:
    yaml = YAML()  # round-trip
    yaml.default_flow_style = False
    yaml.preserve_quotes = True
    # Never wrap long lines: ruamel's fold can land right after an escaped
    # backslash and read back as a phantom space (same bug class fixed in
    # storage/yaml_io.py).  Content fidelity beats pretty wrapping.
    yaml.width = 2**31
    return yaml


def _rewrite_images(slides: Any, rewrite: Callable[[str], str | None]) -> None:
    """Apply *rewrite* to every image element path in-place (None = keep)."""
    if not isinstance(slides, list):
        return
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        elements = slide.get("elements")
        if not isinstance(elements, list):
            continue
        for el in elements:
            if not isinstance(el, dict) or el.get("type") != "image":
                continue
            path = el.get("path")
            if not isinstance(path, str):
                continue
            new = rewrite(path)
            if new is not None and new != path:
                el["path"] = new


def transform_definition(
    text: str,
    *,
    keep_slides: list[int] | None = None,
    rewrite_image_path: Callable[[str], str | None] | None = None,
) -> str:
    """Return *text* with only *keep_slides* (1-based, in the given order) and
    image paths passed through *rewrite_image_path* (return None to keep one).

    Raises:
        DeckError: When the text does not parse as a deck-shaped YAML mapping,
            or an index in *keep_slides* is out of range.  Callers validate
            the definition beforehand, so these indicate a caller bug — but
            they must not corrupt the stored bundle.
    """
    yaml = _make_rt_yaml()
    try:
        data = yaml.load(text)
    except YAMLError as exc:  # pragma: no cover - callers pre-validate
        raise DeckError([f"YAML parse error: {exc}"]) from exc
    if not isinstance(data, dict) or not isinstance(data.get("slides"), list):
        raise DeckError(["top level must be a mapping with a 'slides:' key"])

    slides = data["slides"]
    if keep_slides is not None:
        if any(i < 1 or i > len(slides) for i in keep_slides):
            raise DeckError(
                [f"slide selection out of range (deck has {len(slides)} slides)"]
            )
        data["slides"] = [slides[i - 1] for i in keep_slides]

    if rewrite_image_path is not None:
        _rewrite_images(data["slides"], rewrite_image_path)

    buf = io.StringIO()
    yaml.dump(data, buf)
    return buf.getvalue()
