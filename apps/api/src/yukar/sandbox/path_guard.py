"""PathGuard — structural path traversal protection for Worker sandboxing.

Every path passed to Worker fs/run_command/git tools goes through a
``PathGuard`` instance.  The guard resolves the path to an absolute,
symlink-free form and verifies it is strictly inside the designated root
(the worker's worktree).  Attempts to escape via ``../`` or symlinks raise
``PathGuardError``.

Design
------
- ``PathGuard(root)`` is cheap to construct and immutable after creation.
- ``resolve(path)`` is the single entry point; it returns the safe absolute
  ``Path`` or raises ``PathGuardError``.
- The optional ignore hook (``ignore_fn``) is a placeholder for M3's
  gitignore integration.  Pass a callable ``(Path) -> bool`` that returns
  ``True`` if the resolved path should be treated as blocked (e.g. because
  it is gitignored).  When triggered, ``PathGuardError`` is raised.

Security
--------
``Path.resolve()`` follows symlinks before the ``is_relative_to`` check, so
a symlink that points outside the root is caught.  The root itself is also
resolved so that a root containing a symlink component does not create a
false-sense-of-safety.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


class PathGuardError(PermissionError):
    """Raised when a path would escape the sandbox root or is blocked by the ignore filter."""


class PathGuard:
    """Confines filesystem access to a single root directory.

    Args:
        root: The allowed root directory.  Must exist as a directory.
        ignore_fn: Optional hook for M3 gitignore integration.  Called with
            the resolved absolute path; should return ``True`` if the path is
            blocked.  Defaults to ``None`` (no ignore filtering).

    Raises:
        ValueError: If ``root`` is not an existing directory.
    """

    def __init__(
        self,
        root: Path,
        ignore_fn: Callable[[Path], bool] | None = None,
    ) -> None:
        if not root.exists():
            raise ValueError(f"PathGuard root does not exist: {root}")
        if not root.is_dir():
            raise ValueError(f"PathGuard root is not a directory: {root}")
        # Resolve once; all checks are done against this.
        self._root: Path = root.resolve()
        self._ignore_fn = ignore_fn

    @property
    def root(self) -> Path:
        """Resolved absolute root path."""
        return self._root

    def resolve(self, path: str | Path) -> Path:
        """Resolve *path* relative to the root and verify it stays inside.

        Relative paths are interpreted relative to ``root``.
        Absolute paths are accepted but must still resolve inside ``root``.

        Args:
            path: The path to resolve.  May be relative or absolute, may
                contain ``..`` components, and may contain symlinks.

        Returns:
            The resolved absolute ``Path`` inside the sandbox.

        Raises:
            PathGuardError: If the resolved path is outside the root, or if
                the ignore hook returns ``True`` for the path.
        """
        p = Path(path)
        if not p.is_absolute():
            p = self._root / p
        # resolve() follows all symlinks → symlink escape is caught here.
        resolved = p.resolve()

        # is_relative_to checks that resolved is root itself or a descendant.
        if not self._is_inside(resolved):
            raise PathGuardError(
                f"Path {str(path)!r} resolves to {resolved!r} "
                f"which is outside sandbox root {self._root!r}"
            )

        # Optional ignore hook (M3 placeholder).
        if self._ignore_fn is not None and self._ignore_fn(resolved):
            raise PathGuardError(f"Path {resolved!r} is blocked by the ignore filter")

        return resolved

    def _is_inside(self, resolved: Path) -> bool:
        """Return True if *resolved* is the root or a descendant of it."""
        try:
            resolved.relative_to(self._root)
            return True
        except ValueError:
            return False

    def check_cwd(self, cwd: str | Path) -> Path:
        """Validate a proposed working directory for a subprocess.

        Convenience wrapper around ``resolve`` — identical behaviour but
        communicates intent clearly at call sites.

        Raises:
            PathGuardError: If *cwd* escapes the sandbox.
        """
        return self.resolve(cwd)
