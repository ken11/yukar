"""Worker exact-text edit tools — scoped to the assigned worktree.

``make_fs_edit_tools(ctx)`` returns three Strands tools that give agents a way
to make surgical text edits without rewriting entire files.  All three tools
use exact-string matching rather than line-number offsets.

Tools
-----
- ``fs_replace_exact``     — replace a unique block of text with new text.
- ``fs_insert_after_exact``  — insert text immediately after a unique anchor.
- ``fs_insert_before_exact`` — insert text immediately before a unique anchor.

Design constraints
------------------
- Every path argument is validated through ``ctx.path_guard.resolve()`` before
  any I/O, so Workers cannot reach outside their assigned worktree.
- Gitignored paths are blocked via the ignore hook wired into ``PathGuard``.
- All file I/O uses UTF-8.  Files with other encodings are not supported; the
  tool returns an error message rather than silently mangling the content.
- ``old_text`` / ``anchor_text`` must match exactly one location.  Zero matches
  and N>1 matches both return an actionable error message with retry guidance.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from strands import tool

from yukar.agents.context import AgentContext
from yukar.agents.tools.response_builder import make_error, make_success
from yukar.sandbox.path_guard import PathGuardError

logger = logging.getLogger(__name__)

# Maximum file size for exact-edit reads; mirrors fs.py._MAX_READ_BYTES.
# A1-02: _read_file previously had NO size cap at all — this closes that gap.
_MAX_EDIT_READ_BYTES: int = 1 * 1024 * 1024  # 1 MiB


def _locate_exact(content: str, search_text: str, label: str) -> tuple[int, str]:
    """Find *search_text* in *content*, enforcing exactly one match.

    Args:
        content: Full file content.
        search_text: The string to locate.
        label: Human-readable label used in error messages (``"old_text"`` or
            ``"anchor_text"``).

    Returns:
        ``(start_index, "")`` on success, or ``(-1, error_message)`` on failure.
    """
    if not search_text:
        return -1, f"Error: {label} must not be empty"

    count = content.count(search_text)
    if count == 0:
        return -1, (
            f"Error: {label} not found. "
            "Re-read the target block with fs_read and copy it verbatim. "
            "Whitespace or line-ending differences may be the cause."
        )
    if count > 1:
        return -1, (
            f"Error: {label} matched multiple locations ({count}). "
            "Extend the text to include more surrounding lines to make it unambiguous."
        )

    return content.index(search_text), ""


def make_fs_edit_tools(ctx: AgentContext) -> list[Any]:
    """Return [fs_replace_exact, fs_insert_after_exact, fs_insert_before_exact] tools.

    All tools are closed over *ctx* and validate every path through
    ``ctx.path_guard.resolve()`` before performing any I/O.

    Args:
        ctx: The agent context that determines the allowed worktree root.

    Returns:
        A list of three Strands ``AgentTool`` objects.
    """

    def _read_file(path: str) -> tuple[Path, str] | tuple[None, str]:
        """Validate path and read UTF-8 content.

        Returns ``(abs_path, content)`` on success or ``(None, error_str)`` on
        failure.
        """
        try:
            resolved = ctx.path_guard.resolve(path)
        except PathGuardError:
            # Treat PathGuardError (outside-worktree or gitignored) as "not found",
            # matching the behaviour of fs.py (spec §6.6).
            return None, f"Error: file not found: {path}"

        if not resolved.exists():
            return None, f"Error: file not found: {path}"
        if not resolved.is_file():
            return None, f"Error: not a file: {path}"

        # Bounded read: open binary, cap at _MAX_EDIT_READ_BYTES+1 bytes.
        # This closes the A1-02 TOCTOU gap that _read_file had (no cap at all).
        try:
            with resolved.open("rb") as _fh:
                raw = _fh.read(_MAX_EDIT_READ_BYTES + 1)
        except OSError as exc:
            return None, f"Error reading file: {exc}"
        if len(raw) > _MAX_EDIT_READ_BYTES:
            return None, (
                f"Error: {path} exceeds the {_MAX_EDIT_READ_BYTES}-byte read limit "
                "for exact-edit tools; use fs_read for large files."
            )
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, (
                f"Error: {path} is not UTF-8 encoded. "
                "Only UTF-8 files are supported by the exact-edit tools."
            )

        return resolved, content

    @tool
    def fs_replace_exact(path: str, old_text: str, new_text: str) -> dict[str, Any]:
        """Replace an exact block of text in a file inside the worktree.

        ``old_text`` must match exactly one location in the file.  The tool
        fails with a descriptive error message when there are zero or multiple
        matches, so the agent can retry with more context.

        Use ``fs_read`` to obtain the verbatim text before calling this tool.
        Only UTF-8 encoded files are supported.

        Line numbers in search results are 1-indexed.

        Args:
            path: Path to the file (relative to worktree or absolute within it).
            old_text: Exact text to find (must match exactly once).
            new_text: Replacement text.

        Returns:
            A dict with ``"status"`` (``"success"`` or ``"error"``) and a
            ``"content"`` list containing a single ``{"text": ...}`` message.
        """
        abs_path, payload = _read_file(path)
        if abs_path is None:
            return make_error(payload)

        content: str = payload
        idx, err = _locate_exact(content, old_text, "old_text")
        if err:
            logger.debug("fs_replace_exact %s: %s", path, err)
            return make_error(err)

        new_content = content[:idx] + new_text + content[idx + len(old_text) :]
        try:
            abs_path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return make_error(f"Error writing file: {exc}")

        msg = (
            f"fs_replace_exact: replaced {len(old_text)} chars with {len(new_text)} chars in {path}"
        )
        return make_success(msg, path=str(abs_path))

    @tool
    def fs_insert_after_exact(path: str, anchor_text: str, new_text: str) -> dict[str, Any]:
        """Insert text immediately after an exact anchor block in a file.

        ``anchor_text`` must match exactly one location in the file.  The
        existing content is preserved; *new_text* is spliced in right after the
        anchor ends.

        Use ``fs_read`` to obtain the verbatim anchor before calling this tool.
        Only UTF-8 encoded files are supported.

        Line numbers in search results are 1-indexed.

        Args:
            path: Path to the file (relative to worktree or absolute within it).
            anchor_text: Exact text to locate (must match exactly once).
            new_text: Text to insert after the anchor.

        Returns:
            A dict with ``"status"`` and a ``"content"`` message.
        """
        abs_path, payload = _read_file(path)
        if abs_path is None:
            return make_error(payload)

        content: str = payload
        idx, err = _locate_exact(content, anchor_text, "anchor_text")
        if err:
            logger.debug("fs_insert_after_exact %s: %s", path, err)
            return make_error(err)

        insert_pos = idx + len(anchor_text)
        new_content = content[:insert_pos] + new_text + content[insert_pos:]
        try:
            abs_path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return make_error(f"Error writing file: {exc}")

        msg = f"fs_insert_after_exact: inserted {len(new_text)} chars after anchor in {path}"
        return make_success(msg, path=str(abs_path))

    @tool
    def fs_insert_before_exact(path: str, anchor_text: str, new_text: str) -> dict[str, Any]:
        """Insert text immediately before an exact anchor block in a file.

        ``anchor_text`` must match exactly one location in the file.  The
        existing content is preserved; *new_text* is spliced in right before
        the anchor starts.

        Use ``fs_read`` to obtain the verbatim anchor before calling this tool.
        Only UTF-8 encoded files are supported.

        Line numbers in search results are 1-indexed.

        Args:
            path: Path to the file (relative to worktree or absolute within it).
            anchor_text: Exact text to locate (must match exactly once).
            new_text: Text to insert before the anchor.

        Returns:
            A dict with ``"status"`` and a ``"content"`` message.
        """
        abs_path, payload = _read_file(path)
        if abs_path is None:
            return make_error(payload)

        content: str = payload
        idx, err = _locate_exact(content, anchor_text, "anchor_text")
        if err:
            logger.debug("fs_insert_before_exact %s: %s", path, err)
            return make_error(err)

        new_content = content[:idx] + new_text + content[idx:]
        try:
            abs_path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return make_error(f"Error writing file: {exc}")

        msg = f"fs_insert_before_exact: inserted {len(new_text)} chars before anchor in {path}"
        return make_success(msg, path=str(abs_path))

    return [fs_replace_exact, fs_insert_after_exact, fs_insert_before_exact]
