"""Slide deck definition schema — the contract between agent and renderer.

The agent authors a YAML file; ``load_deck`` parses and validates it into a
``Deck``.  The schema is deliberately a small set of explicit primitives
(text / image / shape / line / table on a fixed pixel canvas): composition
and design stay with the agent, the renderer only draws what is declared.

Geometry is in CSS pixels on a virtual canvas (96 dpi): 1280x720 for 16:9,
960x720 for 4:3.  Both map exactly to PowerPoint's native slide sizes
(13.333x7.5 in / 10x7.5 in), so the same numbers drive python-pptx (EMU)
and the HTML preview (px) without rounding drift.  Font sizes and paragraph
spacings are in points, matching both PowerPoint and CSS ``pt`` units.

Colors accept ``"#RRGGBB"`` (quoted!) or bare ``RRGGBB`` — unquoted ``#…``
would be a YAML comment, so the bare form is allowed as a footgun guard;
values are normalised to ``#RRGGBB``.
"""

from __future__ import annotations

import math
import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

# Hard caps — a runaway definition should fail validation, not exhaust the
# renderer.  Generous for real decks.
MAX_SLIDES = 100
MAX_ELEMENTS_PER_SLIDE = 60
MAX_PARAGRAPHS = 100
MAX_TABLE_ROWS = 200
MAX_TABLE_COLS = 20
MAX_TEXT_CHARS = 4_000
MAX_CELL_CHARS = 2_000
MAX_NOTES_CHARS = 8_000

# Aggregate caps.  Per-field caps alone leave the PRODUCT unbounded (100
# slides x 60 elements x 100 paragraphs x 4000 chars), and YAML anchors can
# reach that product from a small file — so load_deck also enforces totals
# across the whole deck, plus a size cap on the definition text itself.
MAX_DEFINITION_CHARS = 1_000_000
MAX_TOTAL_ELEMENTS = 2_000
MAX_TOTAL_TEXT_CHARS = 2_000_000
MAX_TOTAL_TABLE_CELLS = 20_000

# python-pptx rejects exact line spacing above 1584 pt (ST_TextSpacingPoint)
# and font sizes below 1 pt (ST_TextFontSize); the schema enforces both so a
# valid deck can never crash the renderer mid-way.
MAX_LINE_SPACING_PT = 1584

_COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")

# Canvas sizes per aspect ratio, in CSS px at 96 dpi.  These are exactly
# PowerPoint's default slide dimensions (12192000x6858000 / 9144000x6858000
# EMU at 9525 EMU per px), so px→EMU conversion is integral.
CANVAS_PX: dict[str, tuple[int, int]] = {
    "16:9": (1280, 720),
    "4:3": (960, 720),
}


class DeckError(ValueError):
    """Definition could not be parsed or validated; ``problems`` is per-issue."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _normalize_color(value: str) -> str:
    match = _COLOR_RE.match(value)
    if match is None:
        raise ValueError(
            f"invalid color {value!r} — expected '#RRGGBB' (quote it in YAML: '#1A2B3C')"
        )
    return f"#{match.group(1).upper()}"


Color = Annotated[str, Field(description="'#RRGGBB' color")]


class _Model(BaseModel):
    """Base config: unknown keys are errors so typos surface as validation problems."""

    model_config = ConfigDict(extra="forbid")


class Paragraph(_Model):
    """One paragraph of a text element (uniform styling per paragraph)."""

    text: str = Field(max_length=MAX_TEXT_CHARS)
    size: float | None = Field(default=None, ge=1, le=400, description="font size in pt")
    bold: bool = False
    italic: bool = False
    color: Color | None = None
    align: Literal["left", "center", "right"] | None = None
    bullet: bool = False
    level: int = Field(default=0, ge=0, le=4, description="bullet indent level")
    space_before: float = Field(default=0, ge=0, le=400, description="space above, in pt")
    line_height: float = Field(default=1.25, ge=0.5, le=4, description="multiple of font size")

    @field_validator("color")
    @classmethod
    def _color(cls, v: str | None) -> str | None:
        return None if v is None else _normalize_color(v)


class _Box(_Model):
    """Common placement for box-shaped elements, in canvas px."""

    x: float = Field(ge=-10_000, le=10_000)
    y: float = Field(ge=-10_000, le=10_000)
    w: float = Field(gt=0, le=10_000)
    h: float = Field(gt=0, le=10_000)


class TextElement(_Box):
    type: Literal["text"]
    align: Literal["left", "center", "right"] = "left"
    valign: Literal["top", "middle", "bottom"] = "top"
    paragraphs: list[Paragraph] = Field(min_length=1, max_length=MAX_PARAGRAPHS)


class ImageElement(_Box):
    type: Literal["image"]
    path: str = Field(min_length=1, description="image path inside the worktree")
    fit: Literal["contain", "cover", "stretch"] = "contain"


class ShapeElement(_Box):
    type: Literal["shape"]
    shape: Literal["rect", "rounded", "ellipse"] = "rect"
    fill: Color | None = None
    line_color: Color | None = None
    line_width: float = Field(default=1, gt=0, le=100, description="border width in px")

    @field_validator("fill", "line_color")
    @classmethod
    def _color(cls, v: str | None) -> str | None:
        return None if v is None else _normalize_color(v)


class LineElement(_Model):
    type: Literal["line"]
    x1: float = Field(ge=-10_000, le=10_000)
    y1: float = Field(ge=-10_000, le=10_000)
    x2: float = Field(ge=-10_000, le=10_000)
    y2: float = Field(ge=-10_000, le=10_000)
    color: Color = "#000000"
    width: float = Field(default=2, gt=0, le=100, description="stroke width in px")

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        return _normalize_color(v)


class TableElement(_Box):
    type: Literal["table"]
    rows: list[list[str]] = Field(min_length=1, max_length=MAX_TABLE_ROWS)
    header: bool = True
    col_widths: list[float] | None = Field(
        default=None, description="relative column widths (any positive numbers)"
    )
    font_size: float | None = Field(default=None, ge=1, le=400)
    header_fill: Color = "#4472C4"
    header_color: Color = "#FFFFFF"
    zebra: bool = True

    @field_validator("header_fill", "header_color")
    @classmethod
    def _color(cls, v: str) -> str:
        return _normalize_color(v)

    @field_validator("rows")
    @classmethod
    def _rectangular(cls, rows: list[list[str]]) -> list[list[str]]:
        width = len(rows[0]) if rows else 0
        if width == 0 or width > MAX_TABLE_COLS:
            raise ValueError(f"table must have 1–{MAX_TABLE_COLS} columns")
        for i, row in enumerate(rows):
            if len(row) != width:
                raise ValueError(f"row {i + 1} has {len(row)} cells, expected {width}")
            for cell in row:
                if len(cell) > MAX_CELL_CHARS:
                    raise ValueError(f"cell text exceeds {MAX_CELL_CHARS} characters")
        return rows

    @field_validator("col_widths")
    @classmethod
    def _widths(cls, v: list[float] | None) -> list[float] | None:
        # The upper bound keeps sums and products (el.w * weight) finite in
        # the renderers — unbounded entries overflow float64 there even when
        # each entry is individually finite.
        if v is not None and any(
            not math.isfinite(w) or w <= 0 or w > 1_000_000 for w in v
        ):
            raise ValueError(
                "col_widths entries must be positive numbers no larger than 1000000"
            )
        return v

    @model_validator(mode="after")
    def _widths_match_columns(self) -> TableElement:
        if self.col_widths is not None and len(self.col_widths) != len(self.rows[0]):
            raise ValueError(
                f"col_widths has {len(self.col_widths)} entries but the table has "
                f"{len(self.rows[0])} columns"
            )
        return self


Element = Annotated[
    TextElement | ImageElement | ShapeElement | LineElement | TableElement,
    Field(discriminator="type"),
]


class Slide(_Model):
    background: Color | None = None
    notes: str = Field(default="", max_length=MAX_NOTES_CHARS)
    elements: list[Element] = Field(default_factory=list, max_length=MAX_ELEMENTS_PER_SLIDE)

    @field_validator("background")
    @classmethod
    def _color(cls, v: str | None) -> str | None:
        return None if v is None else _normalize_color(v)


class Deck(_Model):
    size: Literal["16:9", "4:3"] = "16:9"
    background: Color = "#FFFFFF"
    font: str | None = Field(
        default=None,
        max_length=100,
        description="font family name used in both the pptx and the preview",
    )
    text_color: Color = "#000000"
    font_size: float = Field(default=18, ge=1, le=400, description="default font size in pt")
    slides: list[Slide] = Field(min_length=1, max_length=MAX_SLIDES)

    @model_validator(mode="after")
    def _line_spacing_within_pptx_limit(self) -> Deck:
        # Exact spacing is written as size * line_height pt; python-pptx
        # rejects values above MAX_LINE_SPACING_PT, so catch it here with a
        # location instead of crashing mid-render.
        for si, slide in enumerate(self.slides, start=1):
            for ei, el in enumerate(slide.elements, start=1):
                if not isinstance(el, TextElement):
                    continue
                for pi, para in enumerate(el.paragraphs, start=1):
                    size = para.size if para.size is not None else self.font_size
                    if size * para.line_height > MAX_LINE_SPACING_PT:
                        raise ValueError(
                            f"slide {si} element {ei} paragraph {pi}: size x line_height "
                            f"({size:g} x {para.line_height:g}) exceeds "
                            f"{MAX_LINE_SPACING_PT} pt, the maximum line spacing "
                            "PowerPoint accepts"
                        )
        return self

    @field_validator("background", "text_color")
    @classmethod
    def _color(cls, v: str) -> str:
        return _normalize_color(v)

    @field_validator("font")
    @classmethod
    def _font_name(cls, v: str | None) -> str | None:
        # The name flows into the preview's <style> block (raw-text context
        # where html.escape cannot help) and into pptx typeface attributes —
        # allow only characters that are inert in both.
        if v is not None and re.search(r"[<>&\"'{};/\\]", v):
            raise ValueError("font contains characters that are not allowed in a font name")
        return v

    @property
    def canvas(self) -> tuple[int, int]:
        """(width, height) of the virtual canvas in px."""
        return CANVAS_PX[self.size]


def _format_validation_error(exc: ValidationError) -> list[str]:
    problems: list[str] = []
    for err in exc.errors():
        loc = ""
        for part in err["loc"]:
            loc += f"[{part}]" if isinstance(part, int) else (f".{part}" if loc else str(part))
        problems.append(f"{loc or '(root)'}: {err['msg']}")
    return problems


def _check_aggregates(deck: Deck) -> None:
    """Enforce whole-deck totals that per-field caps cannot express."""
    total_elements = 0
    total_chars = 0
    total_cells = 0
    for slide in deck.slides:
        total_chars += len(slide.notes)
        total_elements += len(slide.elements)
        for el in slide.elements:
            if isinstance(el, TextElement):
                total_chars += sum(len(p.text) for p in el.paragraphs)
            elif isinstance(el, TableElement):
                total_cells += len(el.rows) * len(el.rows[0])
                total_chars += sum(len(c) for row in el.rows for c in row)
    problems: list[str] = []
    if total_elements > MAX_TOTAL_ELEMENTS:
        problems.append(f"deck has {total_elements} elements (max {MAX_TOTAL_ELEMENTS})")
    if total_chars > MAX_TOTAL_TEXT_CHARS:
        problems.append(
            f"deck has {total_chars} characters of text (max {MAX_TOTAL_TEXT_CHARS})"
        )
    if total_cells > MAX_TOTAL_TABLE_CELLS:
        problems.append(f"deck has {total_cells} table cells (max {MAX_TOTAL_TABLE_CELLS})")
    if problems:
        raise DeckError(problems)


def load_deck(text: str) -> Deck:
    """Parse a YAML deck definition into a validated ``Deck``.

    Raises:
        DeckError: On YAML syntax errors, schema violations, or whole-deck
            aggregate limits, with one human-readable problem per issue.
    """
    if len(text) > MAX_DEFINITION_CHARS:
        raise DeckError(
            [
                f"definition is {len(text)} characters — max {MAX_DEFINITION_CHARS}; "
                "this does not look like a slide definition"
            ]
        )
    try:
        raw = YAML(typ="safe").load(text)
    except YAMLError as exc:
        raise DeckError([f"YAML parse error: {exc}"]) from exc
    if raw is None:
        raise DeckError(["definition file is empty"])
    if not isinstance(raw, dict):
        raise DeckError(["top level must be a mapping with a 'slides:' key"])
    try:
        deck = Deck.model_validate(raw)
    except ValidationError as exc:
        raise DeckError(_format_validation_error(exc)) from exc
    _check_aggregates(deck)
    return deck


def bounds_warnings(deck: Deck) -> list[str]:
    """Elements that stick out of the canvas — worth a warning, not an error."""
    width, height = deck.canvas
    problems: list[str] = []
    for si, slide in enumerate(deck.slides, start=1):
        for ei, el in enumerate(slide.elements, start=1):
            if isinstance(el, LineElement):
                x0, x1 = sorted((el.x1, el.x2))
                y0, y1 = sorted((el.y1, el.y2))
            else:
                x0, y0, x1, y1 = el.x, el.y, el.x + el.w, el.y + el.h
            if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
                problems.append(
                    f"bounds: slide {si} element {ei} ({el.type}) extends outside the "
                    f"{width}x{height} canvas"
                )
    return problems
