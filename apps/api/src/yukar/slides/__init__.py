"""Slide-deck rendering — agent-authored YAML definition → .pptx + previews.

The agent writes a plain YAML definition file with the normal fs tools; the
``pptx_render`` / ``pptx_preview`` tools (agents/tools/pptx_tools.py) hand it
to this package, which

1. validates it against the pydantic schema (``schema.py``),
2. renders an *editable* PowerPoint file in-process with python-pptx
   (``render_pptx.py`` — no subprocess, no external binary),
3. renders the same definition to self-contained HTML (``render_html.py``)
   and screenshots each slide with the host's shared headless Chromium
   (``preview.py``) so the agent can see what it made, and
4. returns artifacts plus structured warnings (``service.py``): schema
   problems, missing images, out-of-bounds elements, and text overflow
   measured in the browser (python-pptx cannot detect overflow itself).

Both renderers consume the same geometry (a 1280x720 / 960x720 px virtual
canvas mapped 1:1 to EMU at 96 dpi) and the same explicit line heights /
spacings, so the preview tracks the pptx layout closely.  The preview is an
approximation of PowerPoint's renderer, not a bit-exact copy — a
LibreOffice-based high-fidelity preview backend can slot in behind
``service.py`` later without touching the tool surface.
"""
