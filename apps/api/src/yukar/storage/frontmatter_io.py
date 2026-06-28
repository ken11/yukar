"""YAML frontmatter parser shared by skills_repo and agent_profiles_repo.

Both repos store Markdown files with optional YAML frontmatter delimited by
``---`` blocks.  ``parse_frontmatter`` is the single implementation.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter from Markdown content.

    Returns ``(meta_dict, body_text)``.
    If no frontmatter is present, ``meta_dict`` is ``{}`` and ``body_text``
    is the full content unchanged.

    Frontmatter format::

        ---
        key: value
        ---
        body text

    Args:
        content: Raw file content to parse.

    Returns:
        A ``(meta_dict, body_text)`` 2-tuple.
    """
    if not content.startswith("---"):
        return {}, content

    # Find closing ---
    end = content.find("\n---", 3)
    if end == -1:
        return {}, content

    frontmatter_text = content[3:end].strip()
    body = content[end + 4 :].lstrip("\n")

    try:
        from ruamel.yaml import YAML

        yaml = YAML()
        raw = yaml.load(frontmatter_text)
    except Exception:  # noqa: BLE001
        # Malformed YAML in the frontmatter block. Return empty metadata and
        # the body *after* the closing '---' — never leak the raw '---...---'
        # block back as prose body.
        logger.warning("Failed to parse frontmatter; treating as no metadata", exc_info=True)
        return {}, body

    if isinstance(raw, dict):
        return dict(raw), body

    # Well-formed YAML that is not a mapping (e.g. a bare scalar or list):
    # there is no metadata to surface, but the body still follows the block.
    return {}, body
