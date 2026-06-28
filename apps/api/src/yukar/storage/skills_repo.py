"""Skills storage — project-level SKILL.md entries (L2).

Layout:
  <project_dir>/skills/<name>/SKILL.md

Each SKILL.md may have YAML frontmatter with ``name`` and ``description`` keys.
If frontmatter is absent, the directory name is used as the skill name.

Reads and writes are atomic via markdown_io.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from yukar.config import paths as p
from yukar.models.skill import Skill, SkillMeta
from yukar.storage.frontmatter_io import parse_frontmatter
from yukar.storage.markdown_io import read_markdown, write_markdown
from yukar.storage.yaml_io import load_validated_dir

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_skills(root: str, project_id: str) -> list[SkillMeta]:
    """Return metadata for all skills in the project's skills directory."""
    skills_dir = p.project_skills_dir(root, project_id)
    if not skills_dir.exists():
        return []

    # Collect candidate SKILL.md paths (same traversal as before: sorted iterdir,
    # is_dir filter, SKILL.md existence check).
    candidate_mds: list[Path] = [
        skill_subdir / "SKILL.md"
        for skill_subdir in sorted(skills_dir.iterdir())
        if skill_subdir.is_dir() and (skill_subdir / "SKILL.md").exists()
    ]

    def _load_skill_meta(md_path: Path) -> SkillMeta:
        content = read_markdown(md_path)
        meta, _ = parse_frontmatter(content)
        name = str(meta.get("name", md_path.parent.name))
        description = str(meta.get("description", ""))
        return SkillMeta(name=name, description=description)

    return load_validated_dir(candidate_mds, _load_skill_meta, "skill")


def get_skill(root: str, project_id: str, name: str) -> Skill:
    """Return the full skill (meta + content) for *name*.

    Raises FileNotFoundError if the skill does not exist.
    name is validated by p.skill_md_path (PathSegmentError on unsafe name).
    """
    md_path = p.skill_md_path(root, project_id, name)
    if not md_path.exists():
        raise FileNotFoundError(f"Skill not found: {name}")
    content = read_markdown(md_path)
    meta, _ = parse_frontmatter(content)
    return Skill(
        name=str(meta.get("name", name)),
        description=str(meta.get("description", "")),
        content=content,
    )


async def save_skill(root: str, project_id: str, name: str, content: str) -> None:
    """Persist *content* as the SKILL.md for *name*.

    name is validated by p.skill_md_path (PathSegmentError on unsafe name).
    Creates the skill directory if needed.
    """
    md_path = p.skill_md_path(root, project_id, name)
    await write_markdown(md_path, content)


def delete_skill(root: str, project_id: str, name: str) -> bool:
    """Delete the SKILL.md for *name*.

    Returns True if deleted, False if not found.
    name is validated by p.skill_md_path (PathSegmentError on unsafe name).
    """
    md_path = p.skill_md_path(root, project_id, name)
    if not md_path.exists():
        return False
    md_path.unlink()
    # Remove the skill directory if it is now empty.
    with contextlib.suppress(OSError):
        md_path.parent.rmdir()
    return True
