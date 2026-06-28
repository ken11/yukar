"""Verify the false-negative in the unset check in git/diff.py's _vet_gitattributes_content.

finding: diff-unset-substring
-------------------------------
``_vet_gitattributes_content`` implements the "skip unset directives (``-filter=`` etc.)
as they are safe" logic for dangerous driver keywords (``filter=``, ``merge=``, ``diff=``)
using substring matching: ``f"-{keyword}" in stripped`` (line 314).

Because this check is a substring match against the **whole line**, it causes false-negatives
for lines like:

    *.py  something-filter=lfs  filter=lfs

The line contains ``-filter=``, so it is treated as an unset directive and ``continue`` is executed,
causing ``filter=lfs`` on the same line to be **skipped without evaluation**.

A true unset directive appears as a token-level attribute ``-keyword``:

    *.py  -filter

or ``-filter=value`` (attribute name ``-filter`` with ``=value`` appended),
while ``something-filter=`` appearing as part of a pattern name is not an unset.

A confirmed bug that violates the fail-closed principle (false-negatives are not acceptable).

Test strategy:
  TC-1 xfail  ─ line with ``-filter=`` in an attribute name (``something-filter=lfs``)
                 hides ``filter=lfs`` on the same line (false-negative)
  TC-2 xfail  ─ line with ``-merge=`` in an attribute name hides ``merge=drivers`` on the same line
  TC-3 pass   ─ true unset directive ``-filter``  is not added to issues (normal)
  TC-4 pass   ─ true unset directive ``-filter=`` is not added to issues (normal)
  TC-5 pass   ─ dangerous ``filter=lfs`` alone is correctly detected (characterization)
  TC-6 pass   ─ comment lines are ignored (characterization)
  TC-7 pass   ─ empty lines are ignored (characterization)
"""

from __future__ import annotations

# _vet_gitattributes_content is private, but imported directly for bug verification.
from yukar.git.diff import _vet_gitattributes_content

# ---------------------------------------------------------------------------
# Helper: thin wrapper that returns the issues list
# ---------------------------------------------------------------------------


def _vet(content: str, label: str = "test") -> list[str]:
    issues: list[str] = []
    _vet_gitattributes_content(content, label, issues)
    return issues


# ---------------------------------------------------------------------------
# Confirmed bug: xfail(strict=True)  ── reproduce the false-negative
# ---------------------------------------------------------------------------


def test_false_negative_filter_hidden_by_something_filter() -> None:
    """``something-filter=lfs`` hides ``filter=lfs`` on the same line: false-negative (TC-1).

    .gitattributes line:
        *.py  something-filter=lfs  filter=lfs

    This line actually contains the dangerous ``filter=lfs``, so it should be added to issues.
    But the current implementation treats the presence of substring ``-filter=`` as an unset,
    and continues without evaluating ``filter=lfs``.
    """
    content = "*.py  something-filter=lfs  filter=lfs\n"
    issues = _vet(content)
    assert len(issues) == 1, (
        f"Line containing 'filter=lfs' should add 1 item to issues, but got {len(issues)}: {issues}"
    )


def test_false_negative_merge_hidden_by_pre_merge() -> None:
    """``pre-merge=strategy`` hides ``merge=drivers`` on the same line: false-negative (TC-2).

    Confirm the bug where merely having a token containing ``-merge=`` in an attribute name
    causes the truly dangerous ``merge=drivers`` to not be evaluated.
    """
    content = "*.md  pre-merge=strategy  merge=drivers\n"
    issues = _vet(content)
    assert len(issues) == 1, (
        f"Line containing 'merge=drivers' should add 1 item to issues, "
        f"but got {len(issues)}: {issues}"
    )


# ---------------------------------------------------------------------------
# Normal cases: characterization tests that PASS
# ---------------------------------------------------------------------------


def test_true_unset_filter_is_safe() -> None:
    """True unset directive ``-filter`` is not added to issues (TC-3).

    In .gitattributes, ``-filter`` (no value) is a legitimate notation to unset an attribute.
    This is safe, so verify that issues is empty.
    """
    content = "*.py  -filter\n"
    issues = _vet(content)
    assert issues == [], f"True unset directive '-filter' is harmless: {issues}"


def test_true_unset_filter_with_value_is_safe() -> None:
    """True unset directive ``-filter=`` (with-value form) is not added to issues (TC-4).

    In git, forms starting with attribute name ``-filter`` (like ``-filter=lfs``) are
    also treated as unset, so they are considered safe; verify issues is empty.
    """
    content = "*.bin  -filter=lfs\n"
    issues = _vet(content)
    assert issues == [], f"Unset form '-filter=lfs' is harmless: {issues}"


def test_dangerous_filter_alone_is_detected() -> None:
    """Dangerous ``filter=lfs`` appearing alone on a line is correctly detected (TC-5).

    Characterization test for the most basic case with no confusing tokens.
    """
    content = "*.bin  filter=lfs\n"
    issues = _vet(content)
    assert len(issues) == 1, f"'filter=lfs' should add 1 item to issues: {issues}"


def test_comment_lines_are_skipped() -> None:
    """Comment lines (starting with ``#``) are not added to issues (TC-6)."""
    content = "# filter=lfs  merge=custom  diff=custom\n"
    issues = _vet(content)
    assert issues == [], f"Comment lines should be ignored: {issues}"


def test_empty_lines_are_skipped() -> None:
    """Empty lines are not added to issues (TC-7)."""
    content = "\n  \n\t\n"
    issues = _vet(content)
    assert issues == [], f"Empty lines should be ignored: {issues}"


def test_multiple_dangerous_keywords_in_separate_lines() -> None:
    """When dangerous keywords are spread across multiple lines, each line is detected
    independently (characterization)."""
    content = (
        "*.py  filter=lfs\n"
        "*.md  merge=custom\n"
        "*.ts  diff=nodiff\n"
    )
    issues = _vet(content)
    assert len(issues) == 3, (
        f"Dangerous keyword should be detected on each of the 3 lines, "
        f"but got {len(issues)}: {issues}"
    )


def test_label_appears_in_issues() -> None:
    """Each entry in issues contains the source_label (characterization)."""
    content = "*.bin  filter=lfs\n"
    issues = _vet(content, label="my-label")
    assert any("my-label" in issue for issue in issues), (
        f"issues should contain source_label 'my-label': {issues}"
    )
