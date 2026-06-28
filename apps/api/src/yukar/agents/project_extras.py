"""Per-project agent extras — L1/L2 injection helpers (Wave 4a).

Provides:
  - build_skills_plugin(root, project_id, names=None): L2 AgentSkills plugin or None
  - overlay_system_prompt(base, root, project_id, role): L1 instruction overlay
  - overlay_profile_instructions(base, profile): per-agent profile instruction overlay

These helpers are pure readers; they do not write any state.
"""

from __future__ import annotations

import logging
from typing import Any

from yukar.config import paths as p

logger = logging.getLogger(__name__)


def build_skills_plugin(
    root: str,
    project_id: str,
    names: list[str] | None = None,
) -> Any | None:
    """Return an AgentSkills plugin for the project's skills directory, or None.

    Args:
        root: Workspace root path.
        project_id: Project identifier.
        names: Optional list of skill names to include.  When ``None`` or empty,
            all project skills are included (full set — current behaviour).
            When non-empty, only the named skills are included (profile subset).

    Returns None when:
    - No skills directory exists.
    - The directory exists but contains no matching SKILL.md files.
    - strands.AgentSkills is unavailable (logged as warning).
    """
    try:
        from strands import AgentSkills
    except ImportError:
        logger.warning(
            "project_extras: strands.AgentSkills is not available — skills plugin disabled"
        )
        return None

    skills_dir = p.project_skills_dir(root, project_id)
    if not skills_dir.exists():
        return None

    if names:
        # Profile subset: pass individual skill directories.
        # AgentSkills treats a directory containing SKILL.md as a single skill,
        # so we can enumerate exact subdirectories to filter the set.
        skill_paths: list[str] = []
        for name in names:
            skill_subdir = skills_dir / name
            if (skill_subdir / "SKILL.md").exists():
                skill_paths.append(str(skill_subdir))
            else:
                logger.warning(
                    "project_extras: skill %r not found at %s — skipping", name, skill_subdir
                )
        if not skill_paths:
            logger.debug(
                "project_extras: no skills from profile subset %r found in %s", names, skills_dir
            )
            return None
        logger.debug("project_extras: loading AgentSkills subset %r from %s", names, skills_dir)
        return AgentSkills(skills=skill_paths)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    else:
        # Full set: pass the parent directory so all skill subdirectories are loaded.
        has_skills = any(
            (sub / "SKILL.md").exists() for sub in skills_dir.iterdir() if sub.is_dir()
        )
        if not has_skills:
            return None
        logger.debug("project_extras: loading AgentSkills from %s", skills_dir)
        return AgentSkills(skills=[str(skills_dir)])


def overlay_profile_instructions(base: str, profile_instructions: str) -> str:
    """Append profile-specific instructions to *base* system prompt.

    Stacking order:
      1. base (built-in role prompt)
      2. project-level role overlay (via overlay_system_prompt)
      3. profile-level instructions (this function)

    Returns *base* unchanged when *profile_instructions* is empty.
    """
    if not profile_instructions:
        return base
    return base + "\n\n# Profile-specific instructions\n" + profile_instructions


def overlay_system_prompt(base: str, root: str, project_id: str, role: str) -> str:
    """Append per-role custom instructions to *base* system prompt.

    Returns the original *base* unchanged if no custom instructions exist.
    """
    from yukar.storage.agent_config_repo import get_agent_instructions

    overlay = get_agent_instructions(root, project_id, role)
    if not overlay:
        return base
    return base + f"\n\n# Project-specific instructions ({role})\n" + overlay
