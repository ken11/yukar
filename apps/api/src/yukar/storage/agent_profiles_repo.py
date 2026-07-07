"""AgentProfile storage — Markdown + YAML frontmatter per profile file (Wave 5 BE-A).

Layout:
  <yukar_dir>/agent_profiles/<name>.md

Frontmatter keys:
  name, description, base_role, skills, mcp_servers

There is intentionally NO per-profile command allowlist: command permissions
come solely from the repo-level allow/deny list.  Any legacy ``allowed_commands``
(or ``commands: {allow, deny}``) key in an old profile file is ignored on read.

Body text = instructions (the system-prompt overlay).

The frontmatter approach mirrors skills_repo for consistency.
Reads and writes are atomic via markdown_io.
"""

from __future__ import annotations

import logging
from io import StringIO
from pathlib import Path
from typing import Any

from yukar.config import paths as p
from yukar.models.agent_profile import AgentProfile
from yukar.storage.frontmatter_io import parse_frontmatter
from yukar.storage.markdown_io import read_markdown, write_markdown
from yukar.storage.yaml_io import load_validated_dir

logger = logging.getLogger(__name__)


def _build_frontmatter(profile: AgentProfile) -> str:
    """Serialise profile metadata to a YAML frontmatter block.

    Returns the complete Markdown file content (frontmatter + body).
    """
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.default_flow_style = False

    meta: dict[str, Any] = {
        "name": profile.name,
        "description": profile.description,
        "base_role": profile.base_role,
        "skills": list(profile.skills),
        "mcp_servers": list(profile.mcp_servers),
    }

    buf = StringIO()
    buf.write("---\n")
    yaml.dump(meta, buf)
    buf.write("---\n")
    if profile.instructions:
        buf.write("\n")
        buf.write(profile.instructions)
    return buf.getvalue()


def _profile_from_parts(meta: dict[str, Any], body: str, fallback_name: str) -> AgentProfile:
    """Construct an AgentProfile from parsed frontmatter and body text.

    Any legacy ``allowed_commands`` (or ``commands``) key is intentionally not
    read: per-profile command control was removed, so command permissions come
    solely from the repo-level allow/deny list.
    """
    return AgentProfile(
        name=str(meta.get("name", fallback_name)),
        description=str(meta.get("description", "")),
        base_role=meta.get("base_role", "worker"),  # type: ignore[arg-type]
        instructions=body,
        skills=list(meta.get("skills", [])),
        mcp_servers=list(meta.get("mcp_servers", [])),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_profiles(root: str, project_id: str) -> list[AgentProfile]:
    """Return all agent profiles for this project.

    Profiles whose files fail to parse are logged and skipped.
    """
    profiles_dir = p.agent_profiles_dir(root, project_id)
    if not profiles_dir.exists():
        return []

    def _load_profile(md_file: Path) -> AgentProfile:
        content = read_markdown(md_file)
        meta, body = parse_frontmatter(content)
        # Stem is the name fallback (e.g. "backend-worker" from backend-worker.md).
        return _profile_from_parts(meta, body, md_file.stem)

    return load_validated_dir(sorted(profiles_dir.glob("*.md")), _load_profile, "agent profile")


def get_profile(root: str, project_id: str, name: str) -> AgentProfile | None:
    """Return the AgentProfile for *name*, or None if not found.

    *name* is validated by ``p.agent_profile_path`` (PathSegmentError on unsafe input).
    """
    md_path = p.agent_profile_path(root, project_id, name)
    if not md_path.exists():
        return None
    try:
        content = read_markdown(md_path)
        meta, body = parse_frontmatter(content)
        return _profile_from_parts(meta, body, name)
    except Exception:
        logger.warning("Failed to parse agent profile %s", name, exc_info=True)
        return None


async def save_profile(root: str, project_id: str, profile: AgentProfile) -> None:
    """Persist *profile* atomically.

    The profile name from *profile.name* is used as the file stem.
    *profile.name* is validated by ``p.agent_profile_path``.
    """
    md_path = p.agent_profile_path(root, project_id, profile.name)
    content = _build_frontmatter(profile)
    await write_markdown(md_path, content)


def delete_profile(root: str, project_id: str, name: str) -> bool:
    """Delete the profile file for *name*.

    Returns True if deleted, False if not found.
    *name* is validated by ``p.agent_profile_path``.
    """
    md_path = p.agent_profile_path(root, project_id, name)
    if not md_path.exists():
        return False
    md_path.unlink()
    return True
