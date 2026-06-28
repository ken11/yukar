"""Agent config storage — per-role instruction overlay (L1).

Stores per-role custom instructions as plain Markdown files under
  <yukar_dir>/agents/{role}.md

Reads:  return raw file content (empty string if absent).
Writes: atomic via markdown_io.
"""

from __future__ import annotations

from yukar.config import paths as p
from yukar.storage.markdown_io import read_markdown, write_markdown


def get_agent_instructions(root: str, project_id: str, role: str) -> str:
    """Return custom instructions for *role*.

    Returns an empty string if the file does not exist.
    role is validated by agent_config_path (PathSegmentError on bad role).
    """
    path = p.agent_config_path(root, project_id, role)
    if not path.exists():
        return ""
    return read_markdown(path)


async def save_agent_instructions(root: str, project_id: str, role: str, content: str) -> None:
    """Persist *content* as the instruction file for *role* atomically.

    role is validated by agent_config_path (PathSegmentError on bad role).
    """
    path = p.agent_config_path(root, project_id, role)
    await write_markdown(path, content)
