"""Validate nested .gitignore negation patterns in sandbox/ignore.py.

finding: ignore-nested-negation
  When root/.gitignore has ``*.log`` and sub/.gitignore has ``!keep.log``,
  git does *not* ignore sub/keep.log, but IgnoreRules.is_ignored short-circuits
  (returns True as soon as root_spec matches) without evaluating sub's negation pattern,
  incorrectly returning True (ignored). This is a confirmed bug.

Test strategy:
  - test_negation_in_nested_gitignore          -- after fix: PASS
  - test_non_negated_sibling_still_ignored     -- normal case (characterization): PASS
  - test_global_excludes_bypass                -- use global_excludes_path=nonexistent
                                                  to remove noise
  - test_dir_excluded_negation_stays_ignored   -- regression: !keep.txt under build/ exclusion
                                                  is ineffective
  - test_file_pattern_allows_nested_negation   -- regression: *.log + sub/!keep.log is effective
  - test_sibling_still_ignored_under_dir_excl  -- regression: other files under build/ exclusion
                                                  are ignored
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from yukar.sandbox.ignore import IgnoreRules

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create a minimal git repo and return (root, sub)."""
    root = tmp_path / "repo"
    root.mkdir()

    # git init (for actual check-ignore verification; IgnoreRules itself does not use git)
    subprocess.run(["git", "init", "-q", str(root)], check=True)

    # root/.gitignore: *.log
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")

    # sub/.gitignore: !keep.log  (negation = overrides parent rule)
    sub = root / "sub"
    sub.mkdir()
    (sub / ".gitignore").write_text("!keep.log\n", encoding="utf-8")

    # Actually create the files (for git check-ignore verification)
    (sub / "keep.log").write_text("data\n", encoding="utf-8")
    (sub / "other.log").write_text("data\n", encoding="utf-8")

    return root, sub


# ---------------------------------------------------------------------------
# Characterization tests (normal cases) — expected to PASS
# ---------------------------------------------------------------------------


def test_non_negated_sibling_still_ignored(tmp_path: Path) -> None:
    """sub/other.log not covered by the negation pattern is still ignored (normal behavior)."""
    root, sub = _make_repo(tmp_path)

    rules = IgnoreRules.from_repo(root, global_excludes_path=Path("/nonexistent"))

    assert rules.is_ignored(sub / "other.log") is True, (
        "sub/other.log matches *.log in root .gitignore "
        "and is not rescued by a negation pattern, so ignore=True"
    )


def test_repo_root_gitignore_is_applied(tmp_path: Path) -> None:
    """Simple case with only root .gitignore: *.log is applied (characterization)."""
    root = tmp_path / "simple_repo"
    root.mkdir()
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (root / "debug.log").write_text("", encoding="utf-8")

    rules = IgnoreRules.from_repo(root, global_excludes_path=Path("/nonexistent"))

    assert rules.is_ignored(root / "debug.log") is True


def test_nested_gitignore_loaded(tmp_path: Path) -> None:
    """IgnoreRules stores nested .gitignore in the _nested dict (characterization)."""
    root, _sub = _make_repo(tmp_path)

    rules = IgnoreRules.from_repo(root, global_excludes_path=Path("/nonexistent"))

    assert "sub" in rules._nested, (
        "If sub/.gitignore exists, _nested['sub'] should be set"
    )


# ---------------------------------------------------------------------------
# Fixed — regression guard
# ---------------------------------------------------------------------------


def test_negation_in_nested_gitignore(tmp_path: Path) -> None:
    """Git actual behavior: sub/.gitignore's !keep.log overrides root's *.log.

    git check-ignore verification (supplementary):
      $ git check-ignore -v sub/keep.log
      sub/.gitignore:1:!keep.log  sub/keep.log   # last match=negation → not ignored
      $ git add sub/keep.log                      # succeeds (proof that it is not ignored)

    Fixed — regression guard: IgnoreRules.is_ignored now evaluates nested negation
    patterns correctly; sub/keep.log returns False (not ignored) as git expects.
    """
    root, sub = _make_repo(tmp_path)

    rules = IgnoreRules.from_repo(root, global_excludes_path=Path("/nonexistent"))

    # Correct git behavior: !keep.log negates *.log → not ignored
    assert rules.is_ignored(sub / "keep.log") is False, (
        "sub/.gitignore's !keep.log negates root .gitignore's *.log, "
        "so is_ignored should return False (git compatible)"
    )


# ---------------------------------------------------------------------------
# Cross-reference with actual git behavior (supplementary, informational only)
# ---------------------------------------------------------------------------


def test_git_check_ignore_reference(tmp_path: Path) -> None:
    """Record actual git judgment in pytest output (characterization, reference).

    git check-ignore reports "the last matched pattern" with exit=0.
    If the last match for sub/keep.log is !keep.log (include=False),
    git does not actually ignore that file.
    """
    root, sub = _make_repo(tmp_path)

    result = subprocess.run(
        ["git", "check-ignore", "-v", str(sub / "keep.log")],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    # git check-ignore: exit=0 means "matched some pattern".
    # Last match is a negation pattern (!keep.log) so the file is not actually ignored.
    # Only verify no crash here; leave output via print.
    print(f"git check-ignore stdout: {result.stdout!r}")
    print(f"git check-ignore returncode: {result.returncode}")

    # Verify actual git add succeeds (would be fatal if ignored)
    add_result = subprocess.run(
        ["git", "add", str(sub / "keep.log")],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert add_result.returncode == 0, (
        f"git add sub/keep.log failed: {add_result.stderr} "
        "(if git is ignoring it, it should be fatal → that itself is a bug)"
    )


# ---------------------------------------------------------------------------
# Regression tests: #1 G5 review fix — nested negation is ineffective when ancestor dir is ignored
# ---------------------------------------------------------------------------


def _make_dir_exclude_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Minimal repo that excludes build/ and has !keep.txt in build/.gitignore.

    git behavior:
        root/.gitignore: build/  → excludes the entire directory
        build/.gitignore: !keep.txt  → ineffective because git does not descend into build/
        → build/keep.txt remains ignored (True)
    """
    root = tmp_path / "dir_excl_repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)

    (root / ".gitignore").write_text("build/\n", encoding="utf-8")

    build_dir = root / "build"
    build_dir.mkdir()
    (build_dir / ".gitignore").write_text("!keep.txt\n", encoding="utf-8")
    (build_dir / "keep.txt").write_text("data\n", encoding="utf-8")
    (build_dir / "other.txt").write_text("data\n", encoding="utf-8")

    return root, build_dir


def test_dir_excluded_negation_stays_ignored(tmp_path: Path) -> None:
    """When build/ is excluded, build/.gitignore's !keep.txt has no effect.

    git check-ignore verification:
        $ git check-ignore -v build/keep.txt
        .gitignore:1:build/  build/keep.txt   # directory exclusion wins → ignored

    IgnoreRules.is_ignored(build/keep.txt) should return True.
    """
    root, build_dir = _make_dir_exclude_repo(tmp_path)

    # Verify actual git behavior
    git_result = subprocess.run(
        ["git", "check-ignore", "-v", "build/keep.txt"],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    # git check-ignore: exit=0 means matched a pattern (= ignored)
    assert git_result.returncode == 0, (
        f"git should treat build/keep.txt as ignored. "
        f"stdout={git_result.stdout!r} stderr={git_result.stderr!r}"
    )

    rules = IgnoreRules.from_repo(root, global_excludes_path=Path("/nonexistent"))
    assert rules.is_ignored(build_dir / "keep.txt") is True, (
        "build/keep.txt remains ignored because build/ is excluded. "
        "build/.gitignore's !keep.txt is ineffective because git does not descend into build/."
    )


def test_file_pattern_allows_nested_negation(tmp_path: Path) -> None:
    """File pattern exclusion (*.log) allows sub/.gitignore's !keep.log to take effect.

    root/.gitignore: *.log  → file pattern (does not exclude the directory)
    sub/.gitignore: !keep.log
    → sub/ itself is not ignored so git descends → sub/keep.log is not ignored

    This behavior has been PASS as a normal case before the G5 fix and is maintained after.
    """
    root, sub = _make_repo(tmp_path)

    rules = IgnoreRules.from_repo(root, global_excludes_path=Path("/nonexistent"))

    assert rules.is_ignored(sub / "keep.log") is False, (
        "sub/.gitignore's !keep.log negates *.log, "
        "so sub/keep.log must be ignored=False."
    )


def test_sibling_still_ignored_under_dir_excl(tmp_path: Path) -> None:
    """Under build/ exclusion, build/other.txt (not negated) also remains ignored (True)."""
    root, build_dir = _make_dir_exclude_repo(tmp_path)

    rules = IgnoreRules.from_repo(root, global_excludes_path=Path("/nonexistent"))
    assert rules.is_ignored(build_dir / "other.txt") is True, (
        "build/other.txt also remains ignored because build/ is excluded."
    )
