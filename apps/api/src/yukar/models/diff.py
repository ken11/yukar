"""Diff-related models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class FileStat(BaseModel):
    path: str
    added: int = 0
    deleted: int = 0
    status: str = ""  # M/A/D/R etc.


class DiffResult(BaseModel):
    mode: Literal["working", "epic"]
    repo: str
    branch: str | None = None
    files: list[FileStat]
    unified_diff: str  # Full unified diff text
    total_added: int = 0
    total_deleted: int = 0


# ---------------------------------------------------------------------------
# Multi-repo diff summary (spec §5.3)
# ---------------------------------------------------------------------------


class RepoDiffSummary(BaseModel):
    """Lightweight per-repo diff statistics (no unified text)."""

    repo: str
    files: int = 0
    added: int = 0
    deleted: int = 0


class DiffSummary(BaseModel):
    """Aggregated diff summary across all touched repos (spec §5.3)."""

    repos: list[RepoDiffSummary]
    total_files: int = 0
    total_added: int = 0
    total_deleted: int = 0


# ---------------------------------------------------------------------------
# Prune result models (spec §5.2)
# ---------------------------------------------------------------------------


class RepoPruneResult(BaseModel):
    """Per-repo result for a prune operation."""

    repo: str
    worktree_removed: bool = False
    branch_deleted: bool = False
    error: str | None = None
