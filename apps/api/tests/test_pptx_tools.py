"""Manager pptx bundle (pptx_write_definition / pptx_render) — real host stack.

Decks are epic artifacts: everything lives in the epic docs folder, no
worktree or branch is involved.  Preview tests drive the shared headless
Chromium via BrowserSessionManager (same pattern as test_browser_tools);
the no-manager tests verify the fail-soft path where the .pptx is still
produced without previews.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from yukar.agents.tools.pptx_tools import make_manager_pptx_tools
from yukar.config import paths
from yukar.preview.browser import (
    BrowserSessionManager,
    init_browser_session_manager,
)

_DEF = """
slides:
  - elements:
      - type: text
        x: 80
        y: 100
        w: 800
        h: 200
        paragraphs:
          - text: "Hello deck"
            size: 40
      - type: image
        x: 900
        y: 100
        w: 200
        h: 150
        path: "assets/logo.png"
  - elements:
      - type: text
        x: 80
        y: 100
        w: 400
        h: 40
        paragraphs:
          - text: "second slide"
"""


@pytest.fixture
async def browser() -> AsyncIterator[BrowserSessionManager]:
    sessions = BrowserSessionManager()
    init_browser_session_manager(sessions)
    try:
        yield sessions
    finally:
        await sessions.close_all()
        init_browser_session_manager(None)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Any]:
    """Manager bundle + its epic docs directory, with a fixture image."""
    root = str(tmp_path / "workspace")
    tools = {t.tool_name: t for t in make_manager_pptx_tools(root, "p", "e1")}
    docs_dir = paths.epic_docs_dir(root, "p", "e1")
    assert docs_dir.is_dir()  # created by the factory
    (docs_dir / "assets").mkdir()
    buf = io.BytesIO()
    Image.new("RGB", (100, 75), "#3B82F6").save(buf, format="PNG")
    (docs_dir / "assets" / "logo.png").write_bytes(buf.getvalue())
    (docs_dir / "deck.slides.yaml").write_text(_DEF, encoding="utf-8")
    return {"root": root, "tools": tools, "docs": docs_dir}


def _text_of(result: dict[str, Any]) -> str:
    return "\n".join(block.get("text", "") for block in result.get("content", []))


def _images_of(result: dict[str, Any]) -> list[bytes]:
    return [
        block["image"]["source"]["bytes"]
        for block in result.get("content", [])
        if "image" in block
    ]


class TestBundleShape:
    def test_manager_bundle_names(self, env: dict[str, Any]) -> None:
        assert list(env["tools"]) == [
            "pptx_write_definition",
            "pptx_render",
            "pptx_save_template",
            "pptx_list_templates",
            "pptx_load_template",
        ]

    def test_docstrings_carry_format_reference(self, env: dict[str, Any]) -> None:
        write_desc = env["tools"]["pptx_write_definition"].tool_spec["description"]
        assert "Definition format" in write_desc
        assert "'#RRGGBB'" in write_desc
        render_desc = env["tools"]["pptx_render"].tool_spec["description"]
        assert "pptx_write_definition" in render_desc


class TestWriteDefinition:
    async def test_valid_definition(self, env: dict[str, Any]) -> None:
        result = await env["tools"]["pptx_write_definition"](
            filename="report.slides.yaml", content=_DEF
        )
        assert result["status"] == "success"
        assert result["problems"] == []
        assert "valid definition with 2 slide(s)" in _text_of(result)
        assert (env["docs"] / "report.slides.yaml").exists()

    async def test_invalid_definition_written_with_problems(
        self, env: dict[str, Any]
    ) -> None:
        result = await env["tools"]["pptx_write_definition"](
            filename="bad.yaml", content="slides:\n  - elements:\n      - type: text\n"
        )
        assert result["status"] == "success"
        assert result["problems"] != []
        assert "fix them" in _text_of(result)
        assert (env["docs"] / "bad.yaml").exists()

    async def test_filename_rules(self, env: dict[str, Any]) -> None:
        bad_suffix = await env["tools"]["pptx_write_definition"](
            filename="deck.txt", content=_DEF
        )
        assert bad_suffix["status"] == "error"
        escape = await env["tools"]["pptx_write_definition"](
            filename="../../escape.yaml", content=_DEF
        )
        assert escape["status"] == "error"
        too_big = await env["tools"]["pptx_write_definition"](
            filename="big.yaml", content="x" * 1_000_001
        )
        assert too_big["status"] == "error"


class TestRenderWithBrowser:
    async def test_happy_path(self, env: dict[str, Any], browser: Any) -> None:
        result = await env["tools"]["pptx_render"](definition_path="deck.slides.yaml")
        assert result["status"] == "success", _text_of(result)
        out = env["docs"] / "deck.pptx"  # default name strips the .slides suffix
        assert out.exists()
        assert out.read_bytes()[:2] == b"PK"
        assert result["output"] == "deck.pptx"
        assert result["slide_count"] == 2
        assert result["warnings"] == []
        shots = _images_of(result)
        assert len(shots) == 2
        assert all(s[:2] == b"\xff\xd8" for s in shots)

    async def test_previews_persisted_for_docs_page(
        self, env: dict[str, Any], browser: Any
    ) -> None:
        from yukar.storage import decks_repo

        result = await env["tools"]["pptx_render"](definition_path="deck.slides.yaml")
        assert result["status"] == "success"
        directory = decks_repo.previews_dir_for(env["docs"] / "deck.pptx")
        assert sorted(p.name for p in directory.iterdir()) == [
            "slide-01.jpg",
            "slide-02.jpg",
        ]

    async def test_slide_selection(self, env: dict[str, Any], browser: Any) -> None:
        result = await env["tools"]["pptx_render"](
            definition_path="deck.slides.yaml", slides="2"
        )
        assert len(_images_of(result)) == 1
        bad = await env["tools"]["pptx_render"](
            definition_path="deck.slides.yaml", slides="7"
        )
        assert bad["status"] == "error"
        assert "out of range" in _text_of(bad)

    async def test_save_previews_to_gallery(
        self, env: dict[str, Any], browser: Any
    ) -> None:
        result = await env["tools"]["pptx_render"](
            definition_path="deck.slides.yaml", save=True, label="pitch"
        )
        assert result["status"] == "success"
        shots_dir = paths.epic_screenshots_dir(env["root"], "p", "e1")
        files = sorted(p.name for p in shots_dir.glob("*.jpg"))
        assert len(files) == 2
        assert any("pitch-01" in f for f in files)
        assert "Saved 2 slide preview(s)" in _text_of(result)

    async def test_inline_preview_cap_and_paging(
        self, env: dict[str, Any], browser: Any
    ) -> None:
        slide = (
            "  - elements:\n"
            "      - type: text\n"
            "        x: 80\n"
            "        y: 100\n"
            "        w: 400\n"
            "        h: 60\n"
            "        paragraphs:\n"
            '          - text: "s"\n'
        )
        (env["docs"] / "long.yaml").write_text("slides:\n" + slide * 11, encoding="utf-8")
        result = await env["tools"]["pptx_render"](definition_path="long.yaml")
        assert result["status"] == "success"
        assert len(_images_of(result)) == 10  # default cap
        assert 'pass slides="11-11"' in _text_of(result)  # paging note
        rest = await env["tools"]["pptx_render"](definition_path="long.yaml", slides="11")
        assert len(_images_of(rest)) == 1
        too_many = await env["tools"]["pptx_render"](
            definition_path="long.yaml", slides="1-11"
        )
        assert too_many["status"] == "error"
        assert "at most 10" in _text_of(too_many)

    async def test_overflow_warning_surfaces(
        self, env: dict[str, Any], browser: Any
    ) -> None:
        (env["docs"] / "tight.yaml").write_text(
            """
slides:
  - elements:
      - type: text
        x: 0
        y: 0
        w: 200
        h: 30
        paragraphs:
          - text: "long text that will definitely wrap into several lines and overflow"
            size: 20
""",
            encoding="utf-8",
        )
        result = await env["tools"]["pptx_render"](definition_path="tight.yaml")
        assert result["status"] == "success"
        assert any(w.startswith("overflow: slide 1 element 1") for w in result["warnings"])

    async def test_rendering_context_neuters_webrtc(self, browser: Any) -> None:
        context = await browser.rendering_context(width=100, height=100)
        try:
            page = await context.new_page()
            await page.set_content("<html><body></body></html>")
            result = await page.evaluate(
                "() => { try { void window.RTCPeerConnection; return 'open'; }"
                " catch (e) { return 'blocked'; } }"
            )
            assert result == "blocked"
        finally:
            await context.close()


class TestRenderWithoutBrowser:
    async def test_pptx_written_previews_warned(self, env: dict[str, Any]) -> None:
        result = await env["tools"]["pptx_render"](definition_path="deck.slides.yaml")
        assert result["status"] == "success"
        assert (env["docs"] / "deck.pptx").exists()
        assert _images_of(result) == []
        assert any(w.startswith("preview:") for w in result["warnings"])

    async def test_preview_off_skips_engine(self, env: dict[str, Any]) -> None:
        result = await env["tools"]["pptx_render"](
            definition_path="deck.slides.yaml", preview=False
        )
        assert result["status"] == "success"
        assert result["warnings"] == []


class TestErrorPaths:
    async def test_definition_outside_docs(self, env: dict[str, Any]) -> None:
        result = await env["tools"]["pptx_render"](definition_path="../../outside.yaml")
        assert result["status"] == "error"
        assert "Invalid definition path" in _text_of(result)

    async def test_definition_missing(self, env: dict[str, Any]) -> None:
        result = await env["tools"]["pptx_render"](definition_path="nope.yaml")
        assert result["status"] == "error"
        assert "not found" in _text_of(result)

    async def test_invalid_definition_lists_problems(self, env: dict[str, Any]) -> None:
        (env["docs"] / "bad.yaml").write_text(
            "slides:\n  - elements:\n      - type: text\n", encoding="utf-8"
        )
        result = await env["tools"]["pptx_render"](definition_path="bad.yaml")
        assert result["status"] == "error"
        assert "Definition is invalid" in _text_of(result)

    async def test_output_path_rules(self, env: dict[str, Any]) -> None:
        bad_suffix = await env["tools"]["pptx_render"](
            definition_path="deck.slides.yaml", output_path="deck.zip"
        )
        assert bad_suffix["status"] == "error"
        assert "must end in .pptx" in _text_of(bad_suffix)
        escape = await env["tools"]["pptx_render"](
            definition_path="deck.slides.yaml", output_path="../../evil.pptx"
        )
        assert escape["status"] == "error"
        assert "Invalid output path" in _text_of(escape)

    async def test_image_path_escape_becomes_placeholder_warning(
        self, env: dict[str, Any]
    ) -> None:
        (env["docs"] / "esc.yaml").write_text(
            """
slides:
  - elements:
      - type: image
        x: 0
        y: 0
        w: 100
        h: 100
        path: "../../secret.png"
""",
            encoding="utf-8",
        )
        result = await env["tools"]["pptx_render"](definition_path="esc.yaml", preview=False)
        assert result["status"] == "success"
        assert any(
            w.startswith("image:") and "could not be read" in w
            for w in result["warnings"]
        )

    async def test_oversized_image_rejected_before_read(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import yukar.slides.service as service_mod

        monkeypatch.setattr(service_mod, "MAX_IMAGE_BYTES", 10)
        result = await env["tools"]["pptx_render"](
            definition_path="deck.slides.yaml", preview=False
        )
        assert result["status"] == "success"
        assert any(
            w.startswith("image:") and "resize it first" in w
            for w in result["warnings"]
        )
