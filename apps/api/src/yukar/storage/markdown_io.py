"""Markdown file read/write.

All writes go through atomic.py.
Path traversal prevention: callers must validate paths before calling these.
"""

from __future__ import annotations

from pathlib import Path

from yukar.storage.atomic import atomic_write_text


def read_markdown(path: Path) -> str:
    """Read a Markdown file and return its content."""
    return path.read_text(encoding="utf-8")


async def write_markdown(path: Path, content: str) -> None:
    """Write Markdown content atomically.

    Guarantees that *path*'s parent directory exists (delegates to
    ``atomic_write_text`` → ``atomic_write_bytes``).
    """
    await atomic_write_text(path, content)


def list_markdown_files(directory: Path) -> list[str]:
    """List .md filenames in a directory (non-recursive)."""
    if not directory.exists():
        return []
    return sorted(p.name for p in directory.iterdir() if p.suffix == ".md")
