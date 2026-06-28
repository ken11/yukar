"""Regression tests for path-segment validation (config/paths.py).

Finding: ``_validate_segment`` accepted an embedded NUL byte, so a bad id was
deferred to an opaque ``ValueError`` (HTTP 500) deep in the filesystem layer
instead of failing cleanly here as a ``PathSegmentError`` (HTTP 422).  NUL and
other control characters are now rejected up front.
"""

from __future__ import annotations

import pytest

from yukar.config.paths import PathSegmentError, _validate_segment


class TestValidateSegmentRejectsControlChars:
    def test_rejects_embedded_nul(self) -> None:
        with pytest.raises(PathSegmentError):
            _validate_segment("epic\x00id", "epic_id")

    def test_rejects_leading_nul(self) -> None:
        with pytest.raises(PathSegmentError):
            _validate_segment("\x00", "segment")

    def test_rejects_trailing_nul(self) -> None:
        with pytest.raises(PathSegmentError):
            _validate_segment("proj\x00", "project_id")

    def test_rejects_newline(self) -> None:
        with pytest.raises(PathSegmentError):
            _validate_segment("a\nb", "segment")

    def test_rejects_tab(self) -> None:
        with pytest.raises(PathSegmentError):
            _validate_segment("a\tb", "segment")

    def test_rejects_del_char(self) -> None:
        with pytest.raises(PathSegmentError):
            _validate_segment("a\x7fb", "segment")

    def test_error_message_mentions_control(self) -> None:
        with pytest.raises(PathSegmentError, match="control"):
            _validate_segment("x\x00y", "epic_id")


class TestValidateSegmentStillAcceptsValid:
    """The existing accepted-charset behaviour must be unchanged."""

    @pytest.mark.parametrize(
        "value",
        ["EP-1", "proj", "my_repo", "feature-x", "EP-123_v2", "a.b", "UPPER", "数字"],
    )
    def test_accepts_normal_ids(self, value: str) -> None:
        # Must not raise.
        _validate_segment(value, "segment")

    def test_still_rejects_empty(self) -> None:
        with pytest.raises(PathSegmentError):
            _validate_segment("", "segment")

    def test_still_rejects_dotdot(self) -> None:
        with pytest.raises(PathSegmentError):
            _validate_segment("..", "segment")

    def test_still_rejects_slash(self) -> None:
        with pytest.raises(PathSegmentError):
            _validate_segment("a/b", "segment")

    def test_still_rejects_backslash(self) -> None:
        with pytest.raises(PathSegmentError):
            _validate_segment("a\\b", "segment")


class TestNulRejectedViaPathHelpers:
    """End-to-end: a NUL in an id is rejected at the public path-builder level
    (a clean PathSegmentError, not an opaque ValueError from the fs layer)."""

    def test_epic_dir_rejects_nul(self) -> None:
        from yukar.config.paths import epic_dir

        with pytest.raises(PathSegmentError):
            epic_dir("/tmp/ws", "proj", "EP\x001")

    def test_project_dir_rejects_nul(self) -> None:
        from yukar.config.paths import project_dir

        with pytest.raises(PathSegmentError):
            project_dir("/tmp/ws", "pr\x00oj")
