"""Deck definition schema — parsing, validation, and bounds warnings."""

from __future__ import annotations

import pytest

from yukar.slides.schema import (
    MAX_DEFINITION_CHARS,
    MAX_TOTAL_ELEMENTS,
    DeckError,
    bounds_warnings,
    load_deck,
)

_MINIMAL = """
slides:
  - elements:
      - type: text
        x: 10
        y: 10
        w: 100
        h: 50
        paragraphs:
          - text: "hello"
"""


class TestLoadDeck:
    def test_minimal_deck_defaults(self) -> None:
        deck = load_deck(_MINIMAL)
        assert deck.size == "16:9"
        assert deck.canvas == (1280, 720)
        assert deck.background == "#FFFFFF"
        assert len(deck.slides) == 1

    def test_four_three_canvas(self) -> None:
        deck = load_deck('size: "4:3"\n' + _MINIMAL)
        assert deck.canvas == (960, 720)

    def test_color_normalisation(self) -> None:
        deck = load_deck('background: "aabbcc"\ntext_color: "#DDeeFF"\n' + _MINIMAL)
        assert deck.background == "#AABBCC"
        assert deck.text_color == "#DDEEFF"

    def test_invalid_color_mentions_quoting(self) -> None:
        with pytest.raises(DeckError) as exc:
            load_deck('background: "red"\n' + _MINIMAL)
        assert "quote it in YAML" in "\n".join(exc.value.problems)

    def test_unknown_key_reports_location(self) -> None:
        text = """
slides:
  - elements:
      - type: text
        x: 10
        y: 10
        w: 100
        h: 50
        fontsize: 3
        paragraphs:
          - text: "hello"
"""
        with pytest.raises(DeckError) as exc:
            load_deck(text)
        joined = "\n".join(exc.value.problems)
        assert "slides[0]" in joined
        assert "fontsize" in joined

    def test_empty_and_non_mapping(self) -> None:
        with pytest.raises(DeckError):
            load_deck("")
        with pytest.raises(DeckError):
            load_deck("- just\n- a list\n")

    def test_yaml_syntax_error(self) -> None:
        with pytest.raises(DeckError) as exc:
            load_deck("slides: [\n")
        assert "YAML parse error" in exc.value.problems[0]

    def test_table_must_be_rectangular(self) -> None:
        text = """
slides:
  - elements:
      - type: table
        x: 0
        y: 0
        w: 100
        h: 100
        rows:
          - ["a", "b"]
          - ["c"]
"""
        with pytest.raises(DeckError) as exc:
            load_deck(text)
        assert "row 2 has 1 cells" in "\n".join(exc.value.problems)

    def test_col_widths_must_match_column_count(self) -> None:
        text = """
slides:
  - elements:
      - type: table
        x: 0
        y: 0
        w: 100
        h: 100
        col_widths: [1, 2]
        rows:
          - ["a", "b", "c"]
"""
        with pytest.raises(DeckError) as exc:
            load_deck(text)
        assert "col_widths has 2 entries" in "\n".join(exc.value.problems)

    def test_col_widths_reject_nan(self) -> None:
        text = """
slides:
  - elements:
      - type: table
        x: 0
        y: 0
        w: 100
        h: 100
        col_widths: [1, .nan]
        rows:
          - ["a", "b"]
"""
        with pytest.raises(DeckError) as exc:
            load_deck(text)
        assert "must be positive numbers" in "\n".join(exc.value.problems)

    def test_font_rejects_markup_characters(self) -> None:
        with pytest.raises(DeckError) as exc:
            load_deck('font: "x</style><script>alert(1)</script>"\n' + _MINIMAL)
        assert "font" in "\n".join(exc.value.problems)

    def test_font_size_below_one_pt_rejected(self) -> None:
        # python-pptx's ST_TextFontSize floor is 1 pt — sub-1pt sizes must
        # fail validation instead of crashing the renderer.
        text = _MINIMAL.replace('text: "hello"', 'text: "hello"\n            size: 0.5')
        with pytest.raises(DeckError):
            load_deck(text)
        with pytest.raises(DeckError):
            load_deck("font_size: 0.5\n" + _MINIMAL)

    def test_line_spacing_product_capped(self) -> None:
        # size x line_height above 1584 pt exceeds ST_TextSpacingPoint.
        text = _MINIMAL.replace(
            'text: "hello"', 'text: "hello"\n            size: 400\n            line_height: 4'
        )
        with pytest.raises(DeckError) as exc:
            load_deck(text)
        assert "maximum line spacing" in "\n".join(exc.value.problems)

    def test_col_widths_reject_huge_entries(self) -> None:
        text = """
slides:
  - elements:
      - type: table
        x: 0
        y: 0
        w: 100
        h: 100
        col_widths: [1.0e308, 1.0e308]
        rows:
          - ["a", "b"]
"""
        with pytest.raises(DeckError) as exc:
            load_deck(text)
        assert "no larger than" in "\n".join(exc.value.problems)

    def test_definition_size_capped(self) -> None:
        with pytest.raises(DeckError) as exc:
            load_deck("x" * (MAX_DEFINITION_CHARS + 1))
        assert "does not look like a slide definition" in exc.value.problems[0]

    def test_aggregate_element_cap_via_anchors(self) -> None:
        # 100 slides sharing one 21-element anchor = 2100 elements total —
        # every per-field cap passes, only the aggregate cap catches it.
        one = "{type: shape, x: 0, y: 0, w: 10, h: 10}"
        elements = "[" + ", ".join([one] * 21) + "]"
        slides = "\n".join("  - elements: *e" for _ in range(99))
        text = f"slides:\n  - elements: &e {elements}\n{slides}\n"
        with pytest.raises(DeckError) as exc:
            load_deck(text)
        assert f"max {MAX_TOTAL_ELEMENTS}" in "\n".join(exc.value.problems)

    def test_discriminated_element_types(self) -> None:
        text = """
slides:
  - elements:
      - type: line
        x1: 0
        y1: 0
        x2: 10
        y2: 10
      - type: shape
        shape: rounded
        x: 0
        y: 0
        w: 10
        h: 10
"""
        deck = load_deck(text)
        assert [el.type for el in deck.slides[0].elements] == ["line", "shape"]


class TestBoundsWarnings:
    def test_inside_no_warning(self) -> None:
        assert bounds_warnings(load_deck(_MINIMAL)) == []

    def test_outside_flagged(self) -> None:
        text = _MINIMAL.replace("x: 10", "x: 1250")
        warnings = bounds_warnings(load_deck(text))
        assert len(warnings) == 1
        assert warnings[0].startswith("bounds: slide 1 element 1")

    def test_line_extent_flagged(self) -> None:
        text = """
slides:
  - elements:
      - type: line
        x1: 0
        y1: 700
        x2: 1300
        y2: 700
"""
        warnings = bounds_warnings(load_deck(text))
        assert len(warnings) == 1
