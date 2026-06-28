"""Docs CRUD — Markdown files in project/docs/ and epic/docs/.

Path traversal prevention: filenames are validated to be simple names
(no directory separators, no hidden prefix tricks).
"""

from __future__ import annotations

from pathlib import PurePosixPath

from yukar.config import paths
from yukar.storage.markdown_io import list_markdown_files, read_markdown, write_markdown


def _safe_filename(filename: str) -> str:
    """Validate and return a safe filename (no path traversal)."""
    # Reject any path separators or suspicious patterns
    pure = PurePosixPath(filename)
    if len(pure.parts) != 1:
        raise ValueError(f"Invalid doc filename (path traversal): {filename!r}")
    name = pure.name
    if name.startswith(".") or name.startswith("/"):
        raise ValueError(f"Invalid doc filename: {filename!r}")
    if not name.endswith(".md"):
        raise ValueError(f"Doc filename must end with .md: {filename!r}")
    return name


# ---------------------------------------------------------------------------
# Project docs
# ---------------------------------------------------------------------------


def list_project_docs(root: str, project_id: str) -> list[str]:
    directory = paths.project_docs_dir(root, project_id)
    return list_markdown_files(directory)


def get_project_doc(root: str, project_id: str, filename: str) -> str:
    safe = _safe_filename(filename)
    path = paths.project_doc_path(root, project_id, safe)
    if not path.exists():
        raise FileNotFoundError(f"Doc not found: {filename}")
    return read_markdown(path)


async def put_project_doc(root: str, project_id: str, filename: str, content: str) -> None:
    safe = _safe_filename(filename)
    path = paths.project_doc_path(root, project_id, safe)
    await write_markdown(path, content)


# ---------------------------------------------------------------------------
# Epic docs
# ---------------------------------------------------------------------------


def list_epic_docs(root: str, project_id: str, epic_id: str) -> list[str]:
    directory = paths.epic_docs_dir(root, project_id, epic_id)
    return list_markdown_files(directory)


def get_epic_doc(root: str, project_id: str, epic_id: str, filename: str) -> str:
    safe = _safe_filename(filename)
    path = paths.epic_doc_path(root, project_id, epic_id, safe)
    if not path.exists():
        raise FileNotFoundError(f"Doc not found: {filename}")
    return read_markdown(path)


async def put_epic_doc(
    root: str, project_id: str, epic_id: str, filename: str, content: str
) -> None:
    safe = _safe_filename(filename)
    path = paths.epic_doc_path(root, project_id, epic_id, safe)
    await write_markdown(path, content)
