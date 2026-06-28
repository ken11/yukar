"""gitignore semantics for the sandbox layer (spec §6.6).

``IgnoreRules`` synthesises gitignore patterns from four sources (in priority order):

1. Global gitignore  — ``git config core.excludesfile`` → ``~/.config/git/ignore`` fallback
2. Repo-root ``.gitignore``
3. Per-directory ``.gitignore`` files — applied only to their own subtree (git-compatible)
4. ``.git/`` is always excluded regardless of any pattern

All matching is done with ``pathspec``'s ``gitwildmatch`` dialect.  Paths are
tested as **relative POSIX strings** from the repo root so that patterns like
``node_modules/`` and ``*.pyc`` work identically to git.

Usage
-----

    # Synchronous (use only when already inside a thread, e.g. to_thread context):
    rules = IgnoreRules.from_repo(repo_root)

    # Async (preferred inside the event loop):
    rules = await IgnoreRules.from_repo_async(repo_root)

    if rules.is_ignored(some_absolute_path):
        ...

    # Efficient directory pruning during os.walk:
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames
                       if not rules.should_prune_dir(Path(dirpath) / d)]

Design notes
------------
- ``from_repo`` reads the filesystem synchronously.  **Callers that run inside
  the event loop must use ``from_repo_async`` instead.**  ``from_repo`` is
  reserved for synchronous contexts (e.g. already inside ``asyncio.to_thread``).
- ``from_repo_async`` delegates to ``from_repo`` inside ``asyncio.to_thread``
  so the event loop is never blocked by filesystem I/O or the subprocess call
  inside ``_resolve_global_excludes``.
- The global excludesfile is resolved by running ``git config --global
  core.excludesfile``; if that fails the XDG default
  ``~/.config/git/ignore`` is tried, then ``~/.gitignore_global``.
- For testing, pass ``global_excludes_path`` directly to bypass git config
  resolution entirely.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from pathlib import Path

import pathspec


class IgnoreRules:
    """Gitignore-compatible filter for a single repository.

    Args:
        repo_root: Absolute path to the repository root.
        global_spec: ``pathspec.PathSpec`` built from the global gitignore.
        root_spec: ``pathspec.PathSpec`` built from the repo-root ``.gitignore``.
        nested: Mapping from subdirectory (relative to repo root) to its
            ``pathspec.PathSpec``.  Applied only to paths under that directory.
    """

    def __init__(
        self,
        repo_root: Path,
        global_spec: pathspec.PathSpec | None,
        root_spec: pathspec.PathSpec | None,
        nested: dict[str, pathspec.PathSpec],
    ) -> None:
        self._root = repo_root.resolve()
        self._global_spec = global_spec
        self._root_spec = root_spec
        self._nested = nested  # reldir -> spec

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_repo(
        cls,
        repo_root: Path,
        *,
        global_excludes_path: Path | None = None,
    ) -> IgnoreRules:
        """Build an ``IgnoreRules`` from the given repo directory.

        Args:
            repo_root: Absolute path to the repository root.
            global_excludes_path: Optional path to the global gitignore file.
                If provided, bypasses git-config resolution (useful for tests).
                If the path does not exist it is silently ignored.

        Returns:
            An initialised ``IgnoreRules`` instance.
        """
        root = repo_root.resolve()

        # 1. Global gitignore
        global_spec: pathspec.PathSpec | None = None
        if global_excludes_path is not None:
            global_spec = _load_spec(global_excludes_path)
        else:
            global_spec = _load_spec(_resolve_global_excludes())

        # 2. Repo-root .gitignore
        root_spec = _load_spec(root / ".gitignore")

        # 3. Nested .gitignore files (walk the tree, skip .git/)
        nested: dict[str, pathspec.PathSpec] = {}
        for gi_path in root.rglob(".gitignore", recurse_symlinks=False):
            # Skip repo-root .gitignore (already handled)
            if gi_path.parent == root:
                continue
            # Skip anything inside .git/
            try:
                rel = gi_path.relative_to(root)
            except ValueError:
                continue
            if ".git" in rel.parts:
                continue
            spec = _load_spec(gi_path)
            if spec is not None:
                # Store relative directory as POSIX string
                nested[gi_path.parent.relative_to(root).as_posix()] = spec

        return cls(root, global_spec, root_spec, nested)

    @classmethod
    async def from_repo_async(
        cls,
        repo_root: Path,
        *,
        global_excludes_path: Path | None = None,
    ) -> IgnoreRules:
        """Build an ``IgnoreRules`` from *repo_root* without blocking the event loop.

        Delegates to ``from_repo`` inside ``asyncio.to_thread``.  This is the
        preferred factory for callers that run on the asyncio event loop.

        Args:
            repo_root: Absolute path to the repository root.
            global_excludes_path: Optional path to the global gitignore file.
                Forwarded verbatim to ``from_repo``; bypasses git-config
                resolution (useful for tests).

        Returns:
            An initialised ``IgnoreRules`` instance.
        """
        return await asyncio.to_thread(
            cls.from_repo, repo_root, global_excludes_path=global_excludes_path
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_ignored(self, path: Path) -> bool:
        """Return ``True`` if *path* should be excluded.

        *path* may be absolute or relative to the repo root.  Absolute paths
        outside the repo root are always treated as **not** ignored (they are
        caught earlier by ``PathGuard``).

        Directory-type gitignore patterns (e.g. ``__pycache__/``) are matched
        by also testing *path* with a trailing ``/`` so that pathspec correctly
        identifies directory entries.  pathspec matches ``src/__pycache__/``
        against ``__pycache__/`` but not ``src/__pycache__`` (no trailing
        slash), so we always test both forms.

        Git uses **last-match-wins** semantics across all applicable .gitignore
        files evaluated in order: global → repo-root → nested (shallowest to
        deepest).  A negation pattern (``!foo``) in a nested .gitignore can
        override a positive match from a parent .gitignore.  To replicate this
        we evaluate each spec and record the ``include`` flag of the last
        pattern that matches, then let a later (deeper) spec override the
        current state.

        Args:
            path: The path to test.

        Returns:
            ``True`` if the path matches any active ignore rule.
        """
        try:
            rel = _to_rel(path, self._root)
        except ValueError:
            return False

        # Always exclude .git/ and anything under it
        if rel == ".git" or rel.startswith(".git/"):
            return True

        # Always check both ``rel`` and ``rel/``.
        #
        # pathspec matches directory-type patterns (``__pycache__/``) only when
        # the path string ends with ``/``.  We do not want to require the caller
        # to know whether the path is a directory on disk — the path may not
        # exist yet, or it may be provided as a relative string.  Checking both
        # forms is safe because the plain ``rel`` form also matches non-directory
        # patterns, and the ``rel/`` form additionally matches directory patterns.
        candidates = [rel, rel + "/"]

        # Evaluate specs in git priority order (global → root → nested
        # shallowest-first).  Each spec that has a matching pattern overwrites
        # the current ``ignored`` state; a deeper nested spec can therefore
        # override (negate) a match established by an earlier spec.
        ignored: bool | None = None

        # Global gitignore
        if self._global_spec is not None:
            m = _last_match_include(self._global_spec, candidates)
            if m is not None:
                ignored = m

        # Repo-root .gitignore
        if self._root_spec is not None:
            m = _last_match_include(self._root_spec, candidates)
            if m is not None:
                ignored = m

        # Nested .gitignore — each one is applied relative to its own directory.
        # Sort by depth (shallowest first) so that a deeper nested spec can
        # override a shallower one, matching git's evaluation order.
        #
        # Git rule: a nested .gitignore can un-ignore a path only when **none of
        # its ancestor directories** are themselves ignored.  When a directory is
        # excluded (e.g. ``build/`` in the root .gitignore), git does not descend
        # into it, so any ``!pattern`` inside ``build/.gitignore`` never fires —
        # the path stays ignored.  We replicate this by checking whether the
        # *directory* that owns the nested spec is itself ignored before allowing
        # its negation patterns to change the result.
        for dir_rel in sorted(self._nested, key=lambda d: d.count("/")):
            if not (rel.startswith(dir_rel + "/") or rel == dir_rel):
                continue
            spec = self._nested[dir_rel]
            # Path relative to the sub-directory
            sub_rel = rel[len(dir_rel) + 1 :]
            sub_candidates = [sub_rel, sub_rel + "/"]
            m = _last_match_include(spec, sub_candidates)
            if m is None:
                continue
            # Before allowing a negation (m=False) from this nested spec to
            # override the current ``ignored`` state, verify that the directory
            # hosting this .gitignore is not itself ignored.  If the directory
            # is excluded, git would never descend into it, so the nested
            # .gitignore has no effect.  Positive matches (m=True) are always
            # applied regardless — they can only make things *more* ignored.
            if not m and self._dir_is_ignored(dir_rel):
                # The directory owning this .gitignore is itself excluded; skip
                # its negation patterns to match git's "don't descend" rule.
                continue
            ignored = m

        return ignored is True

    def _dir_is_ignored(self, dir_rel: str) -> bool:
        """Return True if the directory *dir_rel* (repo-relative POSIX) is ignored.

        Uses only global and root-level specs (and shallower nested specs) to
        avoid infinite recursion.  This mirrors git's rule that a directory
        excluded by a parent .gitignore prevents nested .gitignore files from
        taking effect.

        Args:
            dir_rel: Repo-relative POSIX path of a directory
                (e.g. ``"build"`` or ``"a/b"``).

        Returns:
            True if the directory is ignored by the global or root .gitignore
            (or by a shallower nested .gitignore), False otherwise.
        """
        dir_candidates = [dir_rel, dir_rel + "/"]

        ignored: bool | None = None

        if self._global_spec is not None:
            m = _last_match_include(self._global_spec, dir_candidates)
            if m is not None:
                ignored = m

        if self._root_spec is not None:
            m = _last_match_include(self._root_spec, dir_candidates)
            if m is not None:
                ignored = m

        # Check shallower nested specs only (depth < depth of dir_rel).
        dir_depth = dir_rel.count("/")
        for ancestor_rel in sorted(self._nested, key=lambda d: d.count("/")):
            if ancestor_rel.count("/") >= dir_depth:
                # Only consider specs shallower than dir_rel.
                break
            if not (dir_rel.startswith(ancestor_rel + "/") or dir_rel == ancestor_rel):
                continue
            ancestor_spec = self._nested[ancestor_rel]
            sub = dir_rel[len(ancestor_rel) + 1 :]
            m = _last_match_include(ancestor_spec, [sub, sub + "/"])
            if m is not None:
                ignored = m

        return ignored is True

    def should_prune_dir(self, path: Path) -> bool:
        """Return ``True`` if an ``os.walk`` traversal should skip *path*.

        Equivalent to ``is_ignored``, but makes intent clear at walk sites.

        Args:
            path: Absolute or repo-relative path to a directory.

        Returns:
            ``True`` if the directory and all its contents should be skipped.
        """
        return self.is_ignored(path)

    def make_ignore_fn(self) -> IgnoreFn:
        """Return a ``(Path) -> bool`` callable suitable for ``PathGuard.ignore_fn``.

        The callable accepts absolute paths inside the repo root and returns
        ``True`` if the path is ignored.

        Returns:
            A bound callable ``ignore_fn(abs_path: Path) -> bool``.
        """

        def _fn(abs_path: Path) -> bool:
            return self.is_ignored(abs_path)

        return _fn


# Type alias used in PathGuard.
IgnoreFn = Callable[[Path], bool]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _last_match_include(
    spec: pathspec.PathSpec, cands: list[str]
) -> bool | None:
    """Return the ``include`` flag of the last pattern in *spec* that matches
    any candidate, or ``None`` if no pattern matches at all.

    Using the last-matching pattern (rather than a simple
    ``spec.match_file()`` boolean) lets negation patterns (``!foo``)
    in a .gitignore override a positive match from a parent file.

    Args:
        spec: A compiled ``PathSpec``.
        cands: One or more path strings to test (e.g. ``["rel", "rel/"]``).

    Returns:
        ``True`` if the last matching pattern is a positive include,
        ``False`` if it is a negation (``!``), or ``None`` if no pattern
        matched any candidate.
    """
    result: bool | None = None
    for pattern in spec.patterns:
        if pattern.regex is None:
            continue
        for c in cands:
            if pattern.regex.match(c):
                result = pattern.include
                break
    return result


def _resolve_global_excludes() -> Path | None:
    """Return the global gitignore path, or ``None`` if not configured / found."""
    # Try git config first
    try:
        result = subprocess.run(
            ["git", "config", "--global", "core.excludesfile"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            raw = result.stdout.strip()
            if raw:
                return Path(raw).expanduser()
    except (OSError, subprocess.TimeoutExpired):
        pass

    # XDG default
    xdg_default = Path.home() / ".config" / "git" / "ignore"
    if xdg_default.exists():
        return xdg_default

    # Legacy default
    legacy = Path.home() / ".gitignore_global"
    if legacy.exists():
        return legacy

    return None


def _load_spec(path: Path | None) -> pathspec.PathSpec | None:
    """Load a ``.gitignore``-style file and return a ``PathSpec``.

    Returns ``None`` if *path* is ``None`` or the file cannot be read.
    """
    if path is None:
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return pathspec.PathSpec.from_lines("gitignore", lines)


def _to_rel(path: Path, root: Path) -> str:
    """Convert *path* to a POSIX string relative to *root*.

    Absolute paths are resolved before relativising.  Relative paths are
    interpreted as already relative to *root*.

    Raises:
        ValueError: If an absolute path is outside *root*.
    """
    if path.is_absolute():
        return path.resolve().relative_to(root).as_posix()
    return path.as_posix()
