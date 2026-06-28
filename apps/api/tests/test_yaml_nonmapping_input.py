"""Characterization / regression test for yaml_io.read_yaml with non-mapping YAML.

Finding [yaml-nonmapping]:
    storage/yaml_io.py:35 calls ``dict(result)`` unconditionally.
    When the YAML file contains a top-level list or scalar, ``dict()`` raises
    ``ValueError`` (list) or ``TypeError`` (int/str scalar) instead of
    returning a graceful empty dict or a typed StorageError.

    frontmatter_io.py has an explicit ``isinstance(raw, dict)`` guard (line 58)
    and handles non-mappings correctly. yaml_io.py lacks the same guard.

Tests marked ``xfail(strict=True)`` represent the confirmed bug; they currently
FAIL (raise an unexpected exception) and will turn into XPASS once the guard is
added — at that point the ``xfail`` marker must be removed.

Tests without ``xfail`` are passing characterization tests that pin existing
correct behaviour (missing file, None YAML, normal mapping).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_raw(path, text: str) -> None:
    """Write raw bytes to *path* so we can test non-mapping YAML content."""
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Existing correct behaviour (characterization – must stay GREEN)
# ---------------------------------------------------------------------------


class TestReadYamlExistingCorrectBehaviours:
    """Pin the behaviour that already works correctly."""

    def test_missing_file_returns_empty_dict(self, tmp_path):
        from yukar.storage.yaml_io import read_yaml

        result = read_yaml(tmp_path / "does_not_exist.yaml")
        assert result == {}

    def test_empty_file_returns_empty_dict(self, tmp_path):
        from yukar.storage.yaml_io import read_yaml

        path = tmp_path / "empty.yaml"
        _write_raw(path, "")
        result = read_yaml(path)
        assert result == {}

    def test_null_yaml_returns_empty_dict(self, tmp_path):
        """A file containing only ``null`` / ``~`` / empty mapping yields {}."""
        from yukar.storage.yaml_io import read_yaml

        path = tmp_path / "null.yaml"
        _write_raw(path, "null\n")
        result = read_yaml(path)
        # ruamel.yaml parses bare ``null`` as Python None → existing guard covers it
        assert result == {}

    def test_normal_mapping_roundtrips(self, tmp_path):
        from yukar.storage.yaml_io import read_yaml

        path = tmp_path / "normal.yaml"
        _write_raw(path, "key: value\nnumber: 42\n")
        result = read_yaml(path)
        assert result == {"key": "value", "number": 42}


# ---------------------------------------------------------------------------
# Bug: top-level non-mapping → should return {} (or raise StorageError),
#      currently raises ValueError / TypeError.
# ---------------------------------------------------------------------------


class TestReadYamlNonMappingTopLevel:
    """These tests expose the confirmed bug in yaml_io.read_yaml.

    Expected (correct) behaviour: read_yaml returns ``{}`` when the YAML
    file is syntactically valid but has a non-mapping top-level value.

    Actual (current) behaviour: ``dict()`` propagates ValueError or TypeError.
    """

    def test_top_level_list_returns_empty_dict(self, tmp_path):
        """A YAML file whose root is a sequence should yield {}."""
        from yukar.storage.yaml_io import read_yaml

        path = tmp_path / "list.yaml"
        _write_raw(path, "- alpha\n- beta\n- gamma\n")
        result = read_yaml(path)
        assert result == {}

    def test_top_level_string_scalar_returns_empty_dict(self, tmp_path):
        """A YAML file whose root is a bare string should yield {}."""
        from yukar.storage.yaml_io import read_yaml

        path = tmp_path / "scalar_str.yaml"
        _write_raw(path, "hello\n")
        result = read_yaml(path)
        assert result == {}

    def test_top_level_integer_scalar_returns_empty_dict(self, tmp_path):
        """A YAML file whose root is a bare integer should yield {}."""
        from yukar.storage.yaml_io import read_yaml

        path = tmp_path / "scalar_int.yaml"
        _write_raw(path, "42\n")
        result = read_yaml(path)
        assert result == {}

    def test_top_level_bool_scalar_returns_empty_dict(self, tmp_path):
        """A YAML file whose root is a bare boolean should yield {}."""
        from yukar.storage.yaml_io import read_yaml

        path = tmp_path / "scalar_bool.yaml"
        _write_raw(path, "true\n")
        result = read_yaml(path)
        assert result == {}
