"""Manager docs tools — read and write project/epic documentation.

The Manager can persist important decisions, design choices, and
discovered facts to Markdown docs so they survive across turns.
Worker and Evaluator do NOT receive these tools.

Reads call the synchronous docs_repo helpers directly (no I/O bypass;
path validation lives in docs_repo._safe_filename).
Writes use docs_repo.put_* which route through storage/markdown_io.py
(atomic temp→os.replace) satisfying the YAML/Markdown write invariant.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _read_docs_collection(
    list_fn: Callable[[], list[str]],
    get_fn: Callable[[str], str],
) -> dict[str, Any]:
    """Read all Markdown files from a docs collection into a dict.

    Iterates over filenames returned by *list_fn*, reads each via *get_fn*,
    and silently substitutes an empty string for any file that raises
    ``FileNotFoundError`` or ``OSError``.

    Args:
        list_fn: Zero-argument callable that returns a list of filenames.
        get_fn: Single-argument callable that accepts a filename and returns
            the file content as a string.

    Returns:
        ``{"files": [...], "docs": {filename: content, ...}}``
    """
    files = list_fn()
    docs: dict[str, str] = {}
    for filename in files:
        try:
            docs[filename] = get_fn(filename)
        except (FileNotFoundError, OSError):
            docs[filename] = ""
    return {"files": files, "docs": docs}


def make_manager_docs_tools(
    root: str,
    project_id: str,
    epic_id: str,
) -> list[Any]:
    """Return [read_project_docs, write_project_doc, read_epic_docs, write_epic_doc].

    Args:
        root: Workspace root (from settings).
        project_id: The current project.
        epic_id: The current epic.

    Returns:
        Four Strands ``AgentTool`` objects for Manager use only.
    """
    from strands import tool

    from yukar.agents.tools.response_builder import make_error, make_success
    from yukar.storage import docs_repo

    @tool
    def read_project_docs() -> dict[str, Any]:
        """Read all project-level Markdown documentation.

        Returns the content of every .md file in the project docs directory
        as a combined string, preceded by a filename header.

        Returns:
            A dict with ``"docs"`` (dict mapping filename→content) and
            ``"files"`` (list of filenames).  Returns an empty dict if no
            docs exist yet.
        """
        return _read_docs_collection(
            lambda: docs_repo.list_project_docs(root, project_id),
            lambda fn: docs_repo.get_project_doc(root, project_id, fn),
        )

    @tool
    async def write_project_doc(filename: str, content: str) -> dict[str, Any]:
        """Write or overwrite a project-level Markdown document.

        Use this to persist important decisions, design choices, or
        architecture notes at the project level so they are available
        across epics and turns.

        Args:
            filename: Document filename (must end with .md, e.g. ``"decisions.md"``).
            content: Full Markdown content to write.

        Returns:
            Confirmation dict with ``"status"``, ``"content"``, and ``"filename"``.
        """
        try:
            await docs_repo.put_project_doc(root, project_id, filename, content)
        except ValueError as exc:
            return make_error(str(exc), ok=False)
        return make_success(f"Wrote {filename}", ok=True, filename=filename)

    @tool
    def read_epic_docs() -> dict[str, Any]:
        """Read all epic-level Markdown documentation.

        Returns the content of every .md file in this epic's docs directory.

        Returns:
            A dict with ``"docs"`` (dict mapping filename→content) and
            ``"files"`` (list of filenames).  Returns an empty dict if no
            docs exist yet.
        """
        return _read_docs_collection(
            lambda: docs_repo.list_epic_docs(root, project_id, epic_id),
            lambda fn: docs_repo.get_epic_doc(root, project_id, epic_id, fn),
        )

    @tool
    async def write_epic_doc(filename: str, content: str) -> dict[str, Any]:
        """Write or overwrite an epic-level Markdown document.

        Use this to persist epic-specific decisions, task analysis,
        or intermediate findings so they survive across manager turns.

        Args:
            filename: Document filename (must end with .md, e.g. ``"plan.md"``).
            content: Full Markdown content to write.

        Returns:
            Confirmation dict with ``"status"``, ``"content"``, and ``"filename"``.
        """
        try:
            await docs_repo.put_epic_doc(root, project_id, epic_id, filename, content)
        except ValueError as exc:
            return make_error(str(exc), ok=False)
        return make_success(f"Wrote {filename}", ok=True, filename=filename)

    return [read_project_docs, write_project_doc, read_epic_docs, write_epic_doc]
