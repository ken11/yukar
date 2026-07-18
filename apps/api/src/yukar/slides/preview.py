"""Slide preview engine — screenshot each slide div with the host Chromium.

Runs the self-contained HTML from ``render_html`` in a network-denied
rendering context (``BrowserSessionManager.rendering_context``), captures one
JPEG per slide, and measures text overflow: for every ``data-measure`` inner
div, content taller than its fixed box means the pptx text will spill out of
its declared box too (same fonts, same exact line heights).  python-pptx
cannot detect this, so the browser measurement is the source of the
``overflow`` warnings.

When no BrowserSessionManager singleton is installed (unit tests, bare
library use) ``PreviewUnavailableError`` is raised; the caller degrades to
"pptx without previews" with a warning rather than failing the render.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from yukar.preview.browser import get_browser_session_manager

_MEASURE_ID_RE = re.compile(r"^s(\d+)e(\d+)$")
_OVERFLOW_TOLERANCE_PX = 2

# Collects (measure-id, content overhang in px) for every text element whose
# paragraphs are taller than the fixed element box.
_MEASURE_JS = f"""
() => {{
  const out = [];
  for (const el of document.querySelectorAll('[data-measure]')) {{
    const box = el.parentElement;
    const dy = Math.round(el.offsetHeight - box.clientHeight);
    if (dy > {_OVERFLOW_TOLERANCE_PX}) out.push({{ id: el.dataset.measure, dy }});
  }}
  return out;
}}
"""

# Wait until system fonts and data-URI images are actually painted before the
# first screenshot — set_content's load event does not cover font swap.
_SETTLE_JS = """
() => Promise.all([
  document.fonts.ready,
  ...[...document.images].map((img) => img.decode().catch(() => null)),
]).then(() => null)
"""


class PreviewUnavailableError(RuntimeError):
    """No shared Chromium in this process — previews cannot be rendered."""


@dataclass(frozen=True, slots=True)
class ElementOverflow:
    """One text element whose content is taller than its declared box."""

    slide: int  # 1-based
    element: int  # 1-based
    overflow_px: int


async def render_slide_previews(
    html: str,
    slide_count: int,
    canvas: tuple[int, int],
    *,
    jpeg_quality: int = 85,
) -> tuple[list[bytes], list[ElementOverflow]]:
    """Screenshot every slide and measure text overflow.

    Args:
        html: Self-contained document from ``render_html``.
        slide_count: Number of ``#slide-N`` divs in the document.
        canvas: (width, height) canvas px — used as the viewport.
        jpeg_quality: Preview JPEG quality.

    Returns:
        (one JPEG per slide in order, overflow measurements).

    Raises:
        PreviewUnavailableError: When no browser manager is installed.
    """
    sessions = get_browser_session_manager()
    if sessions is None:
        raise PreviewUnavailableError(
            "No shared browser is available in this process; previews were skipped."
        )
    width, height = canvas
    context = await sessions.rendering_context(width=width, height=height)
    try:
        page = await context.new_page()
        await page.set_content(html, wait_until="load")
        await page.evaluate(_SETTLE_JS)
        shots: list[bytes] = []
        for i in range(1, slide_count + 1):
            shots.append(
                await page.locator(f"#slide-{i}").screenshot(
                    type="jpeg", quality=jpeg_quality
                )
            )
        raw: list[dict[str, object]] = await page.evaluate(_MEASURE_JS)
    finally:
        await context.close()

    overflows: list[ElementOverflow] = []
    for entry in raw:
        match = _MEASURE_ID_RE.match(str(entry.get("id", "")))
        if match is None:
            continue
        dy = entry.get("dy", 0)
        overflows.append(
            ElementOverflow(
                slide=int(match.group(1)),
                element=int(match.group(2)),
                overflow_px=int(dy) if isinstance(dy, int | float) else 0,
            )
        )
    overflows.sort(key=lambda o: (o.slide, o.element))
    return shots, overflows
