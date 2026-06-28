"""Skill models — project-level SKILL.md entries (L2)."""

from __future__ import annotations

from pydantic import BaseModel


class SkillMeta(BaseModel):
    """Lightweight skill descriptor for list responses."""

    name: str
    description: str = ""


class Skill(BaseModel):
    """Full skill including SKILL.md content."""

    name: str
    description: str = ""
    content: str
