"""Worker filesystem tools — scoped to the assigned worktree.

``make_fs_tools(ctx)`` returns three Strands tools whose closures capture an
``AgentContext``.  Every path argument is validated through
``ctx.path_guard.resolve()`` before any I/O occurs.

Tools
-----
- ``fs_read``  — read the text content of a file
- ``fs_write`` — write (create or overwrite) a file; parent dirs created automatically
- ``fs_list``  — list the entries of a directory

All three raise a ``PermissionError`` (via ``PathGuardError``) if a path
resolves outside the worktree.
"""

from __future__ import annotations

from typing import Any

from strands import tool

from yukar.agents.context import AgentContext
from yukar.agents.tools.response_builder import make_error, make_success
from yukar.sandbox.path_guard import PathGuardError

# Maximum file read / output size to prevent runaway memory use.
_MAX_READ_BYTES = 1 * 1024 * 1024  # 1 MiB


def make_fs_tools(ctx: AgentContext) -> list[Any]:
    """Return [fs_read, fs_write, fs_list] tools bound to *ctx*'s worktree.

    Args:
        ctx: The agent context that determines the allowed root.

    Returns:
        A list of Strands ``AgentTool`` objects ready to pass to ``Agent(tools=...)``.
    """

    @tool
    def fs_read(path: str) -> dict[str, Any]:
        """Read the text content of a file inside the worktree.

        Gitignored files are treated as non-existent (spec §6.6).

        Args:
            path: Path to read.  Relative paths are resolved against the
                worktree root.  Absolute paths must still be inside the
                worktree.

        Returns:
            A dict with ``"content"`` (text) and ``"path"`` (resolved absolute).
        """
        try:
            resolved = ctx.path_guard.resolve(path)
        except PathGuardError:
            # Treat PathGuardError (including ignore-blocked) as "not found"
            # so that gitignored files appear non-existent to agents (spec §6.6).
            return make_error(f"File not found: {path}")

        if not resolved.exists():
            return make_error(f"File not found: {resolved}")
        if not resolved.is_file():
            return make_error(f"Not a file: {resolved}")

        size = resolved.stat().st_size
        if size > _MAX_READ_BYTES:
            return make_error(
                f"File too large to read ({size} bytes > "
                f"{_MAX_READ_BYTES} byte limit): {resolved}"
            )

        # Bounded read: open in binary mode and read at most cap+1 bytes so
        # that a file that grows between stat() and read() cannot consume
        # unbounded memory (A1-02 TOCTOU mitigation).
        try:
            with resolved.open("rb") as _fh:
                raw = _fh.read(_MAX_READ_BYTES + 1)
        except OSError as _exc:
            return make_error(f"Error reading file: {_exc}")
        if len(raw) > _MAX_READ_BYTES:
            return make_error(
                f"File exceeded size limit during read ({len(raw)} bytes > "
                f"{_MAX_READ_BYTES} byte limit): {resolved}"
            )
        text = raw.decode("utf-8", errors="replace")
        return make_success(text, path=str(resolved))

    @tool
    def fs_write(path: str, content: str) -> dict[str, Any]:
        """Write *content* to a file inside the worktree.

        Writing to gitignored paths is rejected (spec §6.6).

        The file is created if it does not exist.  Parent directories are
        created automatically.  Existing files are overwritten.

        Args:
            path: Destination path (relative to worktree or absolute within it).
            content: Text content to write (UTF-8).

        Returns:
            A dict with ``"path"`` (resolved absolute) on success.
        """
        try:
            resolved = ctx.path_guard.resolve(path)
        except PathGuardError as exc:
            return make_error(str(exc))

        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return make_success(f"Written {len(content)} characters to {resolved}", path=str(resolved))

    @tool
    def fs_list(path: str = ".") -> dict[str, Any]:
        """List the entries of a directory inside the worktree.

        Gitignored entries are excluded from the listing (spec §6.6).

        Args:
            path: Directory to list.  Defaults to the worktree root (``"."``).

        Returns:
            A dict with ``"entries"`` (list of names) and ``"path"``
            (resolved absolute directory).
        """
        try:
            resolved = ctx.path_guard.resolve(path)
        except PathGuardError as exc:
            return make_error(str(exc))

        if not resolved.exists():
            return make_error(f"Directory not found: {resolved}")
        if not resolved.is_dir():
            return make_error(f"Not a directory: {resolved}")

        all_entries: list[str] = []
        for child in sorted(resolved.iterdir(), key=lambda p: p.name):
            # Try to resolve through PathGuard so ignored entries are filtered.
            try:
                ctx.path_guard.resolve(child)
                all_entries.append(child.name)
            except PathGuardError:
                pass  # gitignored or outside sandbox — exclude silently

        return make_success("\n".join(all_entries), entries=all_entries, path=str(resolved))

    return [fs_read, fs_write, fs_list]
