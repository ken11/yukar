"""Deck → self-contained preview HTML, mirrored 1:1 from the pptx geometry.

One document holds every slide as a fixed-size ``#slide-N`` div; the preview
engine screenshots each div and evaluates the measurement hook.  The page is
fully self-contained: images are data: URIs, styles are inline, there are no
scripts and no external references (the rendering context aborts all network
anyway).

Parity rules shared with render_pptx:
- geometry in the same canvas px, ``box-sizing: border-box``;
- exact line height ``size * line_height`` (CSS unitless == pptx exact Pt);
- bullet hanging indent 24 px per level;
- identical placeholder/zebra/rounded-corner constants.

Every text element wraps its paragraphs in an inner div tagged
``data-measure="s{slide}e{element}"``; comparing the inner content height to
the fixed box height in the browser yields the text-overflow warnings that
python-pptx cannot compute.
"""

from __future__ import annotations

import base64
import html
import re
from collections.abc import Mapping

from yukar.slides.render_pptx import (
    PLACEHOLDER_FILL,
    PLACEHOLDER_FONT_PT,
    PLACEHOLDER_LINE,
    PLACEHOLDER_TEXT_COLOR,
    ROUNDED_CORNER_FRACTION,
    TABLE_BODY_FILL,
    LoadedImage,
    _cell_fill,
    image_key,
)
from yukar.slides.schema import (
    Deck,
    ImageElement,
    LineElement,
    ShapeElement,
    TableElement,
    TextElement,
)

_BULLET_INDENT_PX = 24

_FALLBACK_FONTS = (
    '"Hiragino Sans", "Hiragino Kaku Gothic ProN", "Yu Gothic", "Meiryo", sans-serif'
)

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
}


def _px(v: float) -> str:
    return f"{v:g}px"


def _escaped(text: str) -> str:
    """HTML-escape *text*, keeping trailing-newline parity with the pptx.

    The pptx writes a trailing ``\\n`` as ``<a:br/>`` + empty run, which
    occupies one line; CSS ``pre-wrap`` gives a trailing newline no line box.
    A zero-width space after the final newline makes the browser render that
    last empty line too, so heights (and overflow measurement) match.
    """
    escaped = html.escape(text)
    if text.endswith("\n"):
        escaped += "\u200b"
    return escaped


def _pt(v: float) -> str:
    return f"{v:g}pt"


def mime_for(path: str) -> str | None:
    """MIME type for a supported image suffix, else None (unsupported)."""
    dot = path.rfind(".")
    return _MIME_BY_SUFFIX.get(path[dot:].lower()) if dot >= 0 else None


def _font_family(deck: Deck) -> str:
    if deck.font:
        # Schema validation already rejects <>&"'{};/\ in font names; strip
        # them here too so this function is safe on any Deck instance.
        cleaned = re.sub(r"[<>&\"'{};/\\]", "", deck.font)
        return f'"{cleaned}", {_FALLBACK_FONTS}'
    return _FALLBACK_FONTS


def _box_style(x: float, y: float, w: float, h: float) -> str:
    return f"left:{_px(x)};top:{_px(y)};width:{_px(w)};height:{_px(h)}"


def _paragraph_html(el: TextElement, deck: Deck) -> str:
    parts: list[str] = []
    for para in el.paragraphs:
        size = para.size if para.size is not None else deck.font_size
        styles = [
            f"font-size:{_pt(size)}",
            f"line-height:{para.line_height:g}",
            f"min-height:{_pt(size * para.line_height)}",
            f"color:{para.color or deck.text_color}",
            f"text-align:{para.align or el.align}",
        ]
        if para.bold:
            styles.append("font-weight:700")
        if para.italic:
            styles.append("font-style:italic")
        if para.space_before:
            styles.append(f"margin-top:{_pt(para.space_before)}")
        classes = "bp" if para.bullet else ""
        if para.bullet:
            styles.append(f"padding-left:{_BULLET_INDENT_PX * (para.level + 1)}px")
            styles.append(f"text-indent:-{_BULLET_INDENT_PX}px")
        parts.append(
            f'<p class="{classes}" style="{";".join(styles)}">{_escaped(para.text)}</p>'
        )
    return "".join(parts)


def _text_html(el: TextElement, deck: Deck, measure_id: str) -> str:
    justify = {"top": "flex-start", "middle": "center", "bottom": "flex-end"}[el.valign]
    return (
        f'<div class="el" style="{_box_style(el.x, el.y, el.w, el.h)};'
        f'display:flex;flex-direction:column;justify-content:{justify}">'
        f'<div data-measure="{measure_id}">{_paragraph_html(el, deck)}</div>'
        "</div>"
    )


def _image_html(el: ImageElement, images: Mapping[str, LoadedImage]) -> str:
    loaded = images.get(image_key(el.path))
    if loaded is None:
        return (
            f'<div class="el missing" style="{_box_style(el.x, el.y, el.w, el.h)}">'
            f"image not found: {html.escape(el.path)}</div>"
        )
    mime = mime_for(el.path) or "image/png"
    data = base64.b64encode(loaded.data).decode("ascii")
    fit = {"contain": "contain", "cover": "cover", "stretch": "fill"}[el.fit]
    return (
        f'<img class="el" style="{_box_style(el.x, el.y, el.w, el.h)};object-fit:{fit}" '
        f'src="data:{mime};base64,{data}" alt="">'
    )


def _shape_html(el: ShapeElement) -> str:
    # PowerPoint strokes are centred on the geometry edge (default algn=ctr):
    # the visual outline spans line_width/2 beyond the declared box on every
    # side.  CSS borders draw inside the border-box, so grow the box by one
    # line_width and shift by half to reproduce the centred stroke exactly.
    x, y, w, h = el.x, el.y, el.w, el.h
    if el.line_color is not None:
        half = el.line_width / 2
        x, y, w, h = x - half, y - half, w + el.line_width, h + el.line_width
    styles = [_box_style(x, y, w, h)]
    if el.fill is not None:
        styles.append(f"background:{el.fill}")
    if el.line_color is not None:
        styles.append(f"border:{_px(el.line_width)} solid {el.line_color}")
    if el.shape == "ellipse":
        styles.append("border-radius:50%")
    elif el.shape == "rounded":
        radius = ROUNDED_CORNER_FRACTION * min(el.w, el.h)
        if el.line_color is not None:
            radius += el.line_width / 2  # outer-edge curvature of a centred stroke
        styles.append(f"border-radius:{_px(radius)}")
    return f'<div class="el" style="{";".join(styles)}"></div>'


def _line_html(el: LineElement, canvas: tuple[int, int]) -> str:
    width, height = canvas
    return (
        f'<svg class="el" style="left:0;top:0" width="{width}" height="{height}">'
        f'<line x1="{el.x1:g}" y1="{el.y1:g}" x2="{el.x2:g}" y2="{el.y2:g}" '
        f'stroke="{el.color}" stroke-width="{el.width:g}"/></svg>'
    )


def _table_html(el: TableElement, deck: Deck) -> str:
    n_rows, n_cols = len(el.rows), len(el.rows[0])
    weights = el.col_widths or [1.0] * n_cols
    total = sum(weights)
    cols = "".join(f'<col style="width:{100 * w / total:g}%">' for w in weights)
    size = el.font_size if el.font_size is not None else deck.font_size
    row_height = el.h / n_rows
    body: list[str] = []
    for r, row in enumerate(el.rows):
        is_header = el.header and r == 0
        cells: list[str] = []
        for text in row:
            styles = [
                f"background:{_cell_fill(el, r)}",
                f"color:{el.header_color if is_header else deck.text_color}",
                f"height:{_px(row_height)}",
            ]
            if is_header:
                styles.append("font-weight:700")
            cells.append(f'<td style="{";".join(styles)}">{_escaped(text)}</td>')
        body.append(f"<tr>{''.join(cells)}</tr>")
    return (
        f'<table class="el" style="{_box_style(el.x, el.y, el.w, el.h)};'
        f'font-size:{_pt(size)}"><colgroup>{cols}</colgroup>{"".join(body)}</table>'
    )


def _slide_html(
    deck: Deck, slide_index: int, images: Mapping[str, LoadedImage]
) -> str:
    slide = deck.slides[slide_index - 1]
    parts: list[str] = []
    for ei, el in enumerate(slide.elements, start=1):
        if isinstance(el, TextElement):
            parts.append(_text_html(el, deck, f"s{slide_index}e{ei}"))
        elif isinstance(el, ImageElement):
            parts.append(_image_html(el, images))
        elif isinstance(el, ShapeElement):
            parts.append(_shape_html(el))
        elif isinstance(el, LineElement):
            parts.append(_line_html(el, deck.canvas))
        elif isinstance(el, TableElement):
            parts.append(_table_html(el, deck))
    background = slide.background or deck.background
    return (
        f'<div class="slide" id="slide-{slide_index}" '
        f'style="background:{background}">{"".join(parts)}</div>'
    )


def render_html(deck: Deck, images: Mapping[str, LoadedImage]) -> str:
    """Full preview document for the deck (self-contained, script-free)."""
    width, height = deck.canvas
    css = f"""
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ background: #808080; font-family: {_font_family(deck)}; }}
.slide {{ position: relative; overflow: hidden;
  width: {width}px; height: {height}px; margin-bottom: 8px; }}
.el {{ position: absolute; }}
p {{ white-space: pre-wrap; overflow-wrap: break-word; }}
p.bp::before {{ content: "•"; display: inline-block;
  width: {_BULLET_INDENT_PX}px; text-indent: 0; }}
.missing {{ background: {PLACEHOLDER_FILL}; border: 1px solid {PLACEHOLDER_LINE};
  color: {PLACEHOLDER_TEXT_COLOR}; font-size: {PLACEHOLDER_FONT_PT}pt;
  display: flex; align-items: center; justify-content: center; text-align: center; }}
table {{ border-collapse: collapse; table-layout: fixed; }}
td {{ padding: 4px 8px; vertical-align: middle; border: 1px solid {TABLE_BODY_FILL};
  overflow-wrap: break-word; white-space: pre-wrap; }}
"""
    slides = "".join(
        _slide_html(deck, i, images) for i in range(1, len(deck.slides) + 1)
    )
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<style>{css}</style></head><body>{slides}</body></html>"
    )
