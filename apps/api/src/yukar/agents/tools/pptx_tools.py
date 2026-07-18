"""PowerPoint rendering tools — Manager-only, scoped to the epic docs folder.

Deck building is the Manager's job: decks are epic artifacts (reports,
summaries, presentations for the user), not repository work products, so
Workers and Evaluators never see these tools and no worktree or branch is
involved.  ``make_manager_pptx_tools`` builds the bundle:

- ``pptx_write_definition`` — writes the YAML definition into the epic docs
  folder (the Manager has no generic file tools) and validates it
  immediately so authoring problems surface before a render.
- ``pptx_render`` — renders the definition to an editable ``.pptx`` next to
  it, returns per-slide preview images plus structured warnings, and can
  save the previews to the epic screenshots gallery (opt-in, mirroring
  ``browser_screenshot``).

Everything is confined to the epic docs directory by a PathGuard rooted
there: the definition, the output, and image references (saved browser
screenshots under ``screenshots/`` are the natural image source).  The
renderers are fixed internals (python-pptx in-process + the host's shared
headless Chromium); composition and design stay in the definition file.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from strands import tool

from yukar.agents.tools.response_builder import make_error, make_success
from yukar.config import paths
from yukar.sandbox.path_guard import PathGuard, PathGuardError
from yukar.slides import service
from yukar.slides.schema import MAX_DEFINITION_CHARS, DeckError, load_deck
from yukar.slides.service import DeckRender, ImageReader, render_deck
from yukar.storage.atomic import atomic_write_bytes, atomic_write_text
from yukar.storage.decks_repo import save_deck_previews
from yukar.storage.screenshots_repo import save_epic_screenshot

_MAX_INLINE_PREVIEWS = 10
_MAX_PROBLEMS_SHOWN = 30

_FORMAT_DOC = """
Definition format (YAML).  Coordinates are px on a fixed canvas — 16:9 is
1280x720, 4:3 is 960x720 — with (0,0) at the top-left.  Font sizes are pt.
Colors are '#RRGGBB' and MUST be quoted (# starts a YAML comment).  '\\n'
inside text makes a line break; styling is per paragraph.

    size: "16:9"              # or "4:3"
    background: "#FFFFFF"     # deck-wide slide background
    font: "Hiragino Sans"     # optional; used in the pptx AND the preview
    text_color: "#111111"     # default text color
    font_size: 18             # default font size in pt
    slides:
      - background: "#0B1220" # optional per-slide override
        notes: "speaker notes"
        elements:
          - type: text
            x: 80
            y: 200
            w: 1120
            h: 160
            align: left       # left | center | right
            valign: top       # top | middle | bottom
            paragraphs:
              - text: "Title"
                size: 40      # pt; also: bold, italic, color, align,
                bold: true    # space_before (pt), line_height (default 1.25)
              - text: "First bullet"
                bullet: true
                level: 0      # 0-4, indents 24px per level
                space_before: 8
          - type: image
            x: 700
            y: 120
            w: 480
            h: 360
            path: "screenshots/login-page.jpg"  # relative to the epic docs
            fit: contain      # folder — saved browser screenshots live under
                              # screenshots/.  contain | cover | stretch
          - type: shape
            shape: rect       # rect | rounded | ellipse
            x: 0
            y: 0
            w: 1280
            h: 8
            fill: "#F59E0B"   # optional; also line_color, line_width (px)
          - type: line
            x1: 80
            y1: 640
            x2: 1200
            y2: 640
            color: "#333333"
            width: 2
          - type: table
            x: 80
            y: 220
            w: 1120
            h: 320
            rows:             # first row is the header (header: false to disable)
              - ["Item", "Q1", "Q2"]
              - ["Sales", "10", "20"]
            col_widths: [2, 1, 1]  # optional relative widths
            font_size: 14     # also: header_fill, header_color, zebra
"""

_WRITE_DOC = f"""Write (or overwrite) a slide-deck YAML definition in the epic docs folder.

The definition is validated immediately: the result lists any schema
problems so you can fix them before calling pptx_render.  Writing always
replaces the whole file — send the complete definition each time.
{_FORMAT_DOC}
Args:
    filename: Definition filename relative to the epic docs folder; must
        end in .yaml or .yml (e.g. "deck.slides.yaml").
    content: The complete YAML definition text.
"""

_RENDER_DOC = f"""Render a slide-deck YAML definition into an editable .pptx file.

Write the definition with pptx_write_definition first, then call this
tool: it renders the .pptx next to the definition in the epic docs folder
(in-process, no shell) and returns one preview image per slide plus
warnings — schema problems, missing images, elements outside the canvas,
and text that overflows its box (measured in a real browser render).
Iterate by rewriting the definition and re-rendering; check the previews
before presenting the deck to the user.  The definition format is
documented on pptx_write_definition.

The user sees the deck on the epic's Docs page: the .pptx is downloadable
there and the slide previews from the last previewed render are shown as a
gallery, so a finished deck needs no extra delivery step.

Args:
    definition_path: Definition path relative to the epic docs folder.
    output_path: Where to write the .pptx (must end in .pptx). Defaults to
        the definition path with its extension replaced
        (deck.slides.yaml → deck.pptx).
    slides: Which slide previews to return, e.g. "3", "2-5", "1,4-6"
        (max {_MAX_INLINE_PREVIEWS} per call). Default: all, capped at
        {_MAX_INLINE_PREVIEWS}.
    preview: Set False to skip preview rendering (faster when you only
        need the .pptx and already checked the previews).
    save: Also save the previews to the epic screenshots gallery so the
        user can review them on the Docs page. Save meaningful
        checkpoints, not every iteration.
    label: Short slug for saved preview filenames; defaults to the
        definition file name. Ignored unless save=True.
"""


@dataclass(frozen=True, slots=True)
class _PptxScope:
    """Where the bundle may read and write: one epic's docs directory."""

    guard: PathGuard
    workspace_root: str
    project_id: str
    epic_id: str


def _parse_slide_selection(spec: str, count: int) -> list[int] | str:
    """Parse '3', '2-5', '1,4-6' into 1-based slide numbers, or an error string."""
    numbers: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        lo_s, sep, hi_s = part.partition("-")
        try:
            lo = int(lo_s)
            hi = int(hi_s) if sep else lo
        except ValueError:
            return f"Invalid slides selection {spec!r} — use forms like \"3\", \"2-5\", \"1,4-6\"."
        if lo > hi or lo < 1 or hi > count:
            return f"Slides selection {part!r} is out of range (deck has {count} slides)."
        numbers.update(range(lo, hi + 1))
    if len(numbers) > _MAX_INLINE_PREVIEWS:
        return (
            f"Selection covers {len(numbers)} slides — at most {_MAX_INLINE_PREVIEWS} "
            "previews can be returned per call; narrow the range."
        )
    return sorted(numbers)


def _default_selection(count: int) -> tuple[list[int], str]:
    """All slides up to the inline cap, plus a note when the deck is longer."""
    if count <= _MAX_INLINE_PREVIEWS:
        return list(range(1, count + 1)), ""
    return (
        list(range(1, _MAX_INLINE_PREVIEWS + 1)),
        f"\nDeck has {count} slides; previews attached for 1-{_MAX_INLINE_PREVIEWS} only "
        f'— pass slides="{_MAX_INLINE_PREVIEWS + 1}-{count}" to see the rest.',
    )


def _warnings_text(warnings: list[str]) -> str:
    if not warnings:
        return "\nNo warnings."
    lines = [f"{i}. {w}" for i, w in enumerate(warnings, start=1)]
    return "\nWarnings:\n" + "\n".join(lines)


def _problems_text(problems: list[str]) -> str:
    shown = problems[:_MAX_PROBLEMS_SHOWN]
    listing = "\n".join(f"{i}. {p}" for i, p in enumerate(shown, start=1))
    more = len(problems) - len(shown)
    if more > 0:
        listing += f"\n… and {more} more problem(s)"
    return listing


def _make_image_reader(scope: _PptxScope) -> ImageReader:
    async def _read(path: str) -> bytes:
        resolved = scope.guard.resolve(path)
        if not resolved.is_file():
            raise FileNotFoundError("no such file in the epic docs folder")
        # Reject on file size BEFORE reading, so an oversized file is never
        # pulled into memory (the service re-checks after read as a backstop
        # for readers without a stat).
        size = resolved.stat().st_size
        if size > service.MAX_IMAGE_BYTES:
            raise ValueError(
                f"file is {size / (1024 * 1024):.1f} MB — "
                f"max {service.MAX_IMAGE_BYTES / (1024 * 1024):.0f} MB; resize it first"
            )
        return await asyncio.to_thread(resolved.read_bytes)

    return _read


def _definition_stem(definition: Path) -> str:
    stem = definition.stem
    return stem.removesuffix(".slides") or stem


async def _render_from_definition(
    scope: _PptxScope, definition_path: str, *, with_preview: bool
) -> tuple[DeckRender, Path] | dict[str, Any]:
    """Load + render, or a ready-to-return error dict."""
    try:
        resolved = scope.guard.resolve(definition_path)
    except PathGuardError as exc:
        return make_error(f"Invalid definition path: {exc}")
    if not resolved.is_file():
        return make_error(f"Definition file not found: {definition_path}")
    # Size gate BEFORE reading — load_deck re-checks characters, but a huge
    # file should never be pulled into memory in the first place.
    size = resolved.stat().st_size
    if size > 4 * MAX_DEFINITION_CHARS:
        return make_error(
            f"Definition file is {size / (1024 * 1024):.1f} MB — this does not "
            "look like a slide definition (YAML decks are far smaller)."
        )
    text = await asyncio.to_thread(resolved.read_text, "utf-8")
    try:
        render = await render_deck(
            text, image_reader=_make_image_reader(scope), with_preview=with_preview
        )
    except DeckError as exc:
        return make_error(f"Definition is invalid:\n{_problems_text(exc.problems)}")
    return render, resolved


def _preview_blocks(render: DeckRender, selection: list[int]) -> list[dict[str, Any]]:
    return [
        {"image": {"format": "jpeg", "source": {"bytes": render.previews[i - 1]}}}
        for i in selection
        if i <= len(render.previews)
    ]


async def _save_previews_to_docs(
    scope: _PptxScope, render: DeckRender, label: str
) -> str:
    if not render.previews:
        return "\n(No previews were rendered, nothing saved to epic docs.)"
    try:
        first = ""
        for i, shot in enumerate(render.previews, start=1):
            filename = await save_epic_screenshot(
                scope.workspace_root,
                scope.project_id,
                scope.epic_id,
                shot,
                label=f"{label}-{i:02d}",
            )
            if not first:
                first = filename
        return (
            f"\nSaved {len(render.previews)} slide preview(s) to epic docs "
            f"(docs/screenshots/{first} …)."
        )
    except (OSError, ValueError) as exc:
        return f"\n(Could not save previews to epic docs: {exc})"


def _selection_or_error(
    render: DeckRender, slides: str
) -> tuple[list[int], str] | dict[str, Any]:
    count = len(render.deck.slides)
    if not slides:
        return _default_selection(count)
    parsed = _parse_slide_selection(slides, count)
    if isinstance(parsed, str):
        return make_error(parsed)
    return parsed, ""


def make_manager_pptx_tools(
    workspace_root: str, project_id: str, epic_id: str
) -> list[Any]:
    """Build the Manager's pptx bundle, scoped to the epic docs directory.

    The docs directory is created if missing (PathGuard requires an existing
    root, and a fresh epic has no docs yet).
    """
    docs_dir = paths.epic_docs_dir(workspace_root, project_id, epic_id)
    docs_dir.mkdir(parents=True, exist_ok=True)
    scope = _PptxScope(
        guard=PathGuard(docs_dir),
        workspace_root=workspace_root,
        project_id=project_id,
        epic_id=epic_id,
    )

    async def pptx_write_definition(filename: str, content: str) -> dict[str, Any]:
        if not filename.endswith((".yaml", ".yml")):
            return make_error("filename must end in .yaml or .yml")
        if len(content) > MAX_DEFINITION_CHARS:
            return make_error(
                f"Definition is {len(content)} characters — max {MAX_DEFINITION_CHARS}."
            )
        try:
            resolved = scope.guard.resolve(filename)
        except PathGuardError as exc:
            return make_error(f"Invalid filename: {exc}")
        await atomic_write_text(resolved, content)
        try:
            deck = await asyncio.to_thread(load_deck, content)
        except DeckError as exc:
            return make_success(
                f"Wrote {filename}, but the definition has problems — fix them "
                f"before rendering:\n{_problems_text(exc.problems)}",
                filename=filename,
                problems=exc.problems,
            )
        return make_success(
            f"Wrote {filename} — valid definition with {len(deck.slides)} slide(s).",
            filename=filename,
            problems=[],
        )

    async def pptx_render(
        definition_path: str,
        output_path: str = "",
        slides: str = "",
        preview: bool = True,
        save: bool = False,
        label: str = "",
    ) -> dict[str, Any]:
        result = await _render_from_definition(scope, definition_path, with_preview=preview)
        if isinstance(result, dict):
            return result
        render, resolved = result

        if output_path:
            if not output_path.endswith(".pptx"):
                return make_error("output_path must end in .pptx")
            try:
                out = scope.guard.resolve(output_path)
            except PathGuardError as exc:
                return make_error(f"Invalid output path: {exc}")
        else:
            out = resolved.with_name(_definition_stem(resolved) + ".pptx")

        selection = _selection_or_error(render, slides)
        if isinstance(selection, dict):
            return selection
        chosen, note = selection

        await atomic_write_bytes(out, render.pptx_bytes)
        # Refresh the deck's Docs-page slide gallery whenever this render
        # produced previews; with preview=False the previous gallery stays
        # (it reflects the last previewed render).
        if render.previews:
            await save_deck_previews(out, render.previews)
        rel_out = out.relative_to(scope.guard.root)

        text = (
            f"Rendered docs/{rel_out} — {len(render.deck.slides)} slide(s), "
            f"{len(render.pptx_bytes) / 1024:.1f} KB."
        )
        text += _warnings_text(render.warnings)
        if render.previews and chosen:
            text += f"\nPreviews attached for slide(s) {', '.join(map(str, chosen))}."
        text += note
        if save:
            text += await _save_previews_to_docs(
                scope, render, label or _definition_stem(resolved)
            )
        return {
            "status": "success",
            "content": [{"text": text}, *_preview_blocks(render, chosen)],
            "output": str(rel_out),
            "slide_count": len(render.deck.slides),
            "warnings": render.warnings,
        }

    # Docstrings carry the (shared, sizeable) format reference, so they are
    # assigned from module constants before @tool snapshots them.
    pptx_write_definition.__doc__ = _WRITE_DOC
    pptx_render.__doc__ = _RENDER_DOC
    return [tool(pptx_write_definition), tool(pptx_render)]
