"""Slide templates — definition transform, bundle storage, Manager tools, router.

A template carries one epic's deck design to future epics as a renderable
bundle under the project docs.  These tests cover the YAML surgery
(subset/rewrite via ruamel round-trip), the staging-swap storage, the
save → list → load tool flow across two epics (including that the loaded
definition actually renders in the destination epic), and the REST surface
for the project Docs page.
"""

from __future__ import annotations

import io
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from PIL import Image

from yukar.agents.tools.pptx_tools import make_manager_pptx_tools
from yukar.config import paths
from yukar.preview.browser import (
    BrowserSessionManager,
    init_browser_session_manager,
)
from yukar.slides.definition_edit import transform_definition
from yukar.slides.schema import DeckError, ImageElement, load_deck
from yukar.storage import slide_templates_repo as repo
from yukar.storage.slide_templates_repo import SlideTemplateInfo

_DEF = """\
background: '#0B1220'  # dark canvas
text_color: '#F8FAFC'
slides:
  - elements:
      - type: text
        x: 80
        y: 100
        w: 800
        h: 200
        paragraphs:
          - text: "Cover"
            size: 40
  - elements:
      - type: image
        x: 80
        y: 100
        w: 200
        h: 150
        path: "screenshots/shot.png"
  - elements:
      - type: text
        x: 80
        y: 100
        w: 400
        h: 40
        paragraphs:
          - text: "body"
"""


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), "#3B82F6").save(buf, format="PNG")
    return buf.getvalue()


def _info(**overrides: Any) -> SlideTemplateInfo:
    base: dict[str, Any] = {
        "description": "corporate look",
        "slide_count": 1,
        "size": "16:9",
        "created_at": "2026-07-20T10:00:00+09:00",
        "source_epic": "e1",
    }
    base.update(overrides)
    return SlideTemplateInfo(**base)


def _text_of(result: dict[str, Any]) -> str:
    return "\n".join(block.get("text", "") for block in result.get("content", []))


def _images_of(result: dict[str, Any]) -> list[bytes]:
    return [
        block["image"]["source"]["bytes"]
        for block in result.get("content", [])
        if "image" in block
    ]


def _yaml_of(result: dict[str, Any]) -> str:
    match = re.search(r"```yaml\n(.*?)```", _text_of(result), flags=re.DOTALL)
    assert match, "load result should embed the definition in a yaml fence"
    return match.group(1)


# ---------------------------------------------------------------------------
# definition_edit
# ---------------------------------------------------------------------------


class TestTransformDefinition:
    def test_subset_and_rewrite_stay_valid(self) -> None:
        out = transform_definition(
            _DEF,
            keep_slides=[2, 3],
            rewrite_image_path=lambda p: "assets/shot.png" if "shot" in p else None,
        )
        deck = load_deck(out)
        assert len(deck.slides) == 2
        assert deck.background == "#0B1220"  # quoted color survives the round-trip
        image = deck.slides[0].elements[0]
        assert isinstance(image, ImageElement)
        assert image.path == "assets/shot.png"
        assert "# dark canvas" in out  # comments survive too

    def test_no_subset_keeps_all_slides(self) -> None:
        out = transform_definition(_DEF, rewrite_image_path=lambda p: None)
        assert len(load_deck(out).slides) == 3

    def test_out_of_range_selection_raises(self) -> None:
        with pytest.raises(DeckError):
            transform_definition(_DEF, keep_slides=[4])

    def test_non_deck_yaml_raises(self) -> None:
        with pytest.raises(DeckError):
            transform_definition("- just\n- a list\n")


# ---------------------------------------------------------------------------
# storage repo
# ---------------------------------------------------------------------------


class TestTemplatesRepo:
    async def _save(self, root: str, name: str = "corp", **kw: Any) -> None:
        args: dict[str, Any] = {"definition_text": _DEF, "info": _info()}
        args.update(kw)
        await repo.save_template(root, "p", name, **args)

    async def test_bundle_structure_and_listing(self, tmp_path: Path) -> None:
        root = str(tmp_path)
        src = tmp_path / "shot.png"
        src.write_bytes(_png_bytes())
        await self._save(
            root,
            notes="axis line on every slide",
            asset_files=[(src, "shot.png")],
            previews=[b"\xff\xd8a", b"\xff\xd8b", b"\xff\xd8c"],
        )
        bundle = paths.slide_template_dir(root, "p", "corp")
        assert (bundle / repo.DEFINITION_FILENAME).is_file()
        assert (bundle / repo.META_FILENAME).is_file()
        assert (bundle / repo.NOTES_FILENAME).read_text("utf-8") == "axis line on every slide"
        assert (bundle / repo.ASSETS_DIRNAME / "shot.png").is_file()
        # Thumbnails are capped at 2 — the cover alone misrepresents a design,
        # but a template is not a full preview gallery either.
        assert repo.list_template_previews(root, "p", "corp") == [
            "slide-01.jpg",
            "slide-02.jpg",
        ]
        metas = repo.list_templates(root, "p")
        assert [m.name for m in metas] == ["corp"]
        assert metas[0].description == "corporate look"
        assert metas[0].has_notes is True

    async def test_overwrite_semantics(self, tmp_path: Path) -> None:
        root = str(tmp_path)
        src = tmp_path / "old.png"
        src.write_bytes(_png_bytes())
        await self._save(root, asset_files=[(src, "old.png")])
        with pytest.raises(FileExistsError):
            await self._save(root)
        await self._save(root, overwrite=True, definition_text="slides: []\n")
        bundle = paths.slide_template_dir(root, "p", "corp")
        assert (bundle / repo.DEFINITION_FILENAME).read_text("utf-8") == "slides: []\n"
        # Replaced wholesale: the old bundle's asset must not linger.
        assert not (bundle / repo.ASSETS_DIRNAME / "old.png").exists()

    @pytest.mark.parametrize(
        "bad", ["", "..", "../x", ".hidden", "-dash", "a/b", "a\\b", "x" * 65, "日本語"]
    )
    async def test_invalid_names_rejected(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(ValueError):
            repo.validate_template_name(bad)
        with pytest.raises(ValueError):
            await self._save(str(tmp_path), name=bad)

    async def test_listing_skips_half_formed_and_hidden(self, tmp_path: Path) -> None:
        root = str(tmp_path)
        await self._save(root)
        base = paths.slide_templates_dir(root, "p")
        # Definition without metadata = half-formed leftovers.
        broken = base / "broken"
        broken.mkdir()
        (broken / repo.DEFINITION_FILENAME).write_text("slides: []\n", encoding="utf-8")
        # Dot-dirs (e.g. crashed staging) never surface.
        staging = base / ".staging-x"
        staging.mkdir()
        assert [m.name for m in repo.list_templates(root, "p")] == ["corp"]

    async def test_listing_survives_corrupt_meta(self, tmp_path: Path) -> None:
        """One hand-corrupted bundle must only hide itself, never the listing."""
        root = str(tmp_path)
        await self._save(root)

        def _bad_bundle(name: str, meta_text: str) -> None:
            d = paths.slide_templates_dir(root, "p") / name
            d.mkdir(parents=True)
            (d / repo.DEFINITION_FILENAME).write_text("slides: []\n", encoding="utf-8")
            (d / repo.META_FILENAME).write_text(meta_text, encoding="utf-8")

        _bad_bundle("syntax", "description: [unclosed\n")
        _bad_bundle("types", "slide_count: three\n")
        assert [m.name for m in repo.list_templates(root, "p")] == ["corp"]

    async def test_preview_read_validation(self, tmp_path: Path) -> None:
        root = str(tmp_path)
        await self._save(root, previews=[b"\xff\xd8a"])
        assert repo.read_template_preview(root, "p", "corp", "slide-01.jpg")
        with pytest.raises(ValueError):
            repo.read_template_preview(root, "p", "corp", "../secret.jpg")
        with pytest.raises(ValueError):
            repo.read_template_preview(root, "p", "corp", "shot.png")
        with pytest.raises(FileNotFoundError):
            repo.read_template_preview(root, "p", "corp", "slide-09.jpg")

    async def test_delete(self, tmp_path: Path) -> None:
        root = str(tmp_path)
        await self._save(root)
        assert await repo.delete_template(root, "p", "corp") is True
        assert await repo.delete_template(root, "p", "corp") is False
        assert repo.list_templates(root, "p") == []


# ---------------------------------------------------------------------------
# Manager tools (save → list → load across two epics)
# ---------------------------------------------------------------------------


@pytest.fixture
def envs(tmp_path: Path) -> dict[str, Any]:
    """Two epics' Manager bundles in one project; e1 has a deck + screenshot."""
    root = str(tmp_path / "ws")
    e1 = {t.tool_name: t for t in make_manager_pptx_tools(root, "p", "e1")}
    e2 = {t.tool_name: t for t in make_manager_pptx_tools(root, "p", "e2")}
    docs1 = paths.epic_docs_dir(root, "p", "e1")
    (docs1 / "screenshots").mkdir()
    (docs1 / "screenshots" / "shot.png").write_bytes(_png_bytes())
    (docs1 / "deck.slides.yaml").write_text(_DEF, encoding="utf-8")
    return {
        "root": root,
        "e1": e1,
        "e2": e2,
        "docs1": docs1,
        "docs2": paths.epic_docs_dir(root, "p", "e2"),
    }


class TestTemplateTools:
    async def test_save_list_load_roundtrip(self, envs: dict[str, Any]) -> None:
        saved = await envs["e1"]["pptx_save_template"](
            definition_path="deck.slides.yaml",
            name="corp",
            description="corporate look",
            notes="keep the axis line",
        )
        assert saved["status"] == "success"
        assert "Saved template 'corp'" in _text_of(saved)
        bundle = paths.slide_template_dir(envs["root"], "p", "corp")
        assert (bundle / repo.ASSETS_DIRNAME / "shot.png").is_file()

        listed = await envs["e2"]["pptx_list_templates"]()
        assert listed["templates"] == ["corp"]
        assert "corporate look" in _text_of(listed)

        loaded = await envs["e2"]["pptx_load_template"](name="corp")
        assert loaded["status"] == "success"
        text = _text_of(loaded)
        assert "keep the axis line" in text
        assert (envs["docs2"] / "template-assets" / "corp" / "shot.png").is_file()

        # The returned definition must be valid and point at the copied asset…
        definition = _yaml_of(loaded)
        deck = load_deck(definition)
        images = [
            el
            for slide in deck.slides
            for el in slide.elements
            if isinstance(el, ImageElement)
        ]
        assert [i.path for i in images] == ["template-assets/corp/shot.png"]

        # …and actually render inside the destination epic (no image warnings).
        wrote = await envs["e2"]["pptx_write_definition"](
            filename="deck.slides.yaml", content=definition
        )
        assert wrote["problems"] == []
        rendered = await envs["e2"]["pptx_render"](
            definition_path="deck.slides.yaml", preview=False
        )
        assert rendered["status"] == "success"
        assert not any("image:" in w for w in rendered["warnings"])

    async def test_save_subset_drops_unused_assets(self, envs: dict[str, Any]) -> None:
        result = await envs["e1"]["pptx_save_template"](
            definition_path="deck.slides.yaml",
            name="text-only",
            description="no images",
            slides="1,3",
        )
        assert result["status"] == "success"
        meta = repo.read_template_meta(envs["root"], "p", "text-only")
        assert meta.slide_count == 2
        assert repo.list_template_assets(envs["root"], "p", "text-only") == []

    async def test_save_duplicate_needs_overwrite(self, envs: dict[str, Any]) -> None:
        first = await envs["e1"]["pptx_save_template"](
            definition_path="deck.slides.yaml", name="corp", description="v1"
        )
        assert first["status"] == "success"
        second = await envs["e1"]["pptx_save_template"](
            definition_path="deck.slides.yaml", name="corp", description="v2"
        )
        assert second["status"] == "error"
        assert "overwrite=True" in _text_of(second)
        third = await envs["e1"]["pptx_save_template"](
            definition_path="deck.slides.yaml", name="corp", description="v2", overwrite=True
        )
        assert third["status"] == "success"
        assert repo.read_template_meta(envs["root"], "p", "corp").description == "v2"

    async def test_save_invalid_name(self, envs: dict[str, Any]) -> None:
        result = await envs["e1"]["pptx_save_template"](
            definition_path="deck.slides.yaml", name="../evil", description="x"
        )
        assert result["status"] == "error"

    async def test_save_missing_image_warns_but_saves(self, envs: dict[str, Any]) -> None:
        (envs["docs1"] / "screenshots" / "shot.png").unlink()
        result = await envs["e1"]["pptx_save_template"](
            definition_path="deck.slides.yaml", name="corp", description="x"
        )
        assert result["status"] == "success"
        assert any("could not be read" in w for w in result["warnings"])
        assert repo.list_template_assets(envs["root"], "p", "corp") == []

    async def test_asset_name_collisions_stay_distinct(self, envs: dict[str, Any]) -> None:
        """Same basename in two directories → two assets; spelling variants of
        one file → one asset.  Case-only differences must also stay distinct
        (a case-insensitive filesystem would otherwise silently merge them)."""
        from yukar.agents.tools.pptx_tools import _unique_asset_name

        used: set[str] = set()
        assert _unique_asset_name("x/Logo.png", used) == "Logo.png"
        assert _unique_asset_name("y/logo.png", used) == "logo-2.png"

        docs1 = envs["docs1"]
        red, blue = io.BytesIO(), io.BytesIO()
        Image.new("RGB", (10, 10), "#FF0000").save(red, format="PNG")
        Image.new("RGB", (10, 10), "#0000FF").save(blue, format="PNG")
        (docs1 / "a").mkdir()
        (docs1 / "b").mkdir()
        (docs1 / "a" / "shot.png").write_bytes(red.getvalue())
        (docs1 / "b" / "shot.png").write_bytes(blue.getvalue())
        img = 'type: image\n        x: 0\n        y: 0\n        w: 100\n        h: 100'
        definition = (
            "slides:\n"
            "  - elements:\n"
            f'      - {img}\n        path: "a/shot.png"\n'
            f'      - {img}\n        path: "b/shot.png"\n'
            f'      - {img}\n        path: "./a/shot.png"\n'
        )
        (docs1 / "collide.slides.yaml").write_text(definition, encoding="utf-8")
        result = await envs["e1"]["pptx_save_template"](
            definition_path="collide.slides.yaml", name="collide", description="x"
        )
        assert result["status"] == "success"
        assets = {
            p.name: p.read_bytes()
            for p in repo.list_template_assets(envs["root"], "p", "collide")
        }
        assert assets["shot.png"] == red.getvalue()
        assert assets["shot-2.png"] == blue.getvalue()
        stored = load_deck(repo.read_template_definition(envs["root"], "p", "collide"))
        paths_in_order = [
            el.path for el in stored.slides[0].elements if isinstance(el, ImageElement)
        ]
        # a/shot.png and its ./a/shot.png spelling share one asset; b/ gets its own.
        assert paths_in_order == ["assets/shot.png", "assets/shot-2.png", "assets/shot.png"]

    async def test_load_unknown_lists_available(self, envs: dict[str, Any]) -> None:
        await envs["e1"]["pptx_save_template"](
            definition_path="deck.slides.yaml", name="corp", description="x"
        )
        result = await envs["e2"]["pptx_load_template"](name="nope")
        assert result["status"] == "error"
        assert "corp" in _text_of(result)


class TestTemplateThumbnailsWithBrowser:
    @pytest.fixture
    async def browser(self) -> AsyncIterator[BrowserSessionManager]:
        sessions = BrowserSessionManager()
        init_browser_session_manager(sessions)
        try:
            yield sessions
        finally:
            await sessions.close_all()
            init_browser_session_manager(None)

    async def test_two_thumbnails_saved_and_returned(
        self, envs: dict[str, Any], browser: BrowserSessionManager
    ) -> None:
        saved = await envs["e1"]["pptx_save_template"](
            definition_path="deck.slides.yaml", name="corp", description="x"
        )
        assert saved["status"] == "success"
        # Three slides in the deck, but exactly two thumbnails: the cover
        # alone often misrepresents the design, and more is gallery bloat.
        assert repo.list_template_previews(envs["root"], "p", "corp") == [
            "slide-01.jpg",
            "slide-02.jpg",
        ]
        loaded = await envs["e2"]["pptx_load_template"](name="corp")
        assert len(_images_of(loaded)) == 2


# ---------------------------------------------------------------------------
# router
# ---------------------------------------------------------------------------


class TestSlideTemplatesApi:
    async def _seed(self, root: Path) -> None:
        await repo.save_template(
            str(root),
            "proj",
            "corp",
            definition_text=_DEF,
            info=_info(),
            previews=[b"\xff\xd8a", b"\xff\xd8b"],
        )

    async def test_list_empty(self, app_client: AsyncClient) -> None:
        r = await app_client.get("/api/projects/proj/slide-templates")
        assert r.status_code == 200
        assert r.json() == []

    async def test_list_and_preview(
        self, app_client: AsyncClient, tmp_workspace: Path
    ) -> None:
        await self._seed(tmp_workspace)
        r = await app_client.get("/api/projects/proj/slide-templates")
        assert r.status_code == 200
        (meta,) = r.json()
        assert meta["name"] == "corp"
        assert meta["previews"] == ["slide-01.jpg", "slide-02.jpg"]

        p = await app_client.get(
            "/api/projects/proj/slide-templates/corp/previews/slide-01.jpg"
        )
        assert p.status_code == 200
        assert p.headers["content-type"] == "image/jpeg"
        assert p.headers["cache-control"] == "no-store"

    async def test_preview_validation(
        self, app_client: AsyncClient, tmp_workspace: Path
    ) -> None:
        await self._seed(tmp_workspace)
        r = await app_client.get(
            "/api/projects/proj/slide-templates/corp/previews/shot.png"
        )
        assert r.status_code == 422
        r = await app_client.get(
            "/api/projects/proj/slide-templates/corp/previews/slide-09.jpg"
        )
        assert r.status_code == 404
        r = await app_client.get(
            "/api/projects/proj/slide-templates/-bad/previews/slide-01.jpg"
        )
        assert r.status_code == 422

    async def test_delete(self, app_client: AsyncClient, tmp_workspace: Path) -> None:
        await self._seed(tmp_workspace)
        r = await app_client.delete("/api/projects/proj/slide-templates/corp")
        assert r.status_code == 204
        assert (await app_client.get("/api/projects/proj/slide-templates")).json() == []
        r = await app_client.delete("/api/projects/proj/slide-templates/corp")
        assert r.status_code == 404
        r = await app_client.delete("/api/projects/proj/slide-templates/-bad")
        assert r.status_code == 422
