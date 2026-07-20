"""Workspace layout — the ONLY place that knows the yukar-projects/ directory structure.

All paths are functions taking a workspace_root (str) from settings,
plus project/epic identifiers. No other module should construct these paths.

Layout follows spec §4.1.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

from yukar.models.roles import ConfigurableAgentRole

# ---------------------------------------------------------------------------
# Path segment validation
# ---------------------------------------------------------------------------

_FORBIDDEN_CHARS = frozenset({"/", "\\"})


class PathSegmentError(ValueError):
    """Raised when a path segment fails traversal-safety validation.

    This is the *only* ValueError subclass that maps to HTTP 422 in app.py.
    Other ValueErrors (e.g. from pydantic, epic/project lookups) propagate
    as 500 so they do not leak internal details to clients.
    """


def _validate_segment(value: str, label: str = "segment") -> None:
    """Raise PathSegmentError if *value* is not a safe single path segment.

    Rules (in order):
    - Must not be empty.
    - Must not be '.' or '..'.
    - Must not contain '/' or '\\'.
    - Must not contain a NUL byte or any other control character.  These never
      occur in legitimate ids, but a raw NUL would otherwise survive validation
      and surface as an opaque ``ValueError`` (HTTP 500) deep in the filesystem
      layer instead of a clean 422 here.
    - Must not start with '-'.  Leading hyphens are valid POSIX filenames but
      are dangerous when segments are passed as git/shell arguments (option
      injection).  No legitimate yukar id starts with '-'.
    """
    if not value:
        raise PathSegmentError(f"Path {label} must not be empty")
    if value in (".", ".."):
        raise PathSegmentError(f"Path {label} must not be '.' or '..': {value!r}")
    if any(c in _FORBIDDEN_CHARS for c in value):
        raise PathSegmentError(f"Path {label} must be a single segment (no '/' or '\\'): {value!r}")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise PathSegmentError(f"Path {label} must not contain control characters: {value!r}")
    if value.startswith("-"):
        raise PathSegmentError(
            f"Path {label} must not start with '-' (option injection risk): {value!r}"
        )


# ---------------------------------------------------------------------------
# Workspace / Project
# ---------------------------------------------------------------------------


def workspace_root(root: str) -> Path:
    return Path(root)


def project_dir(root: str, project_id: str) -> Path:
    _validate_segment(project_id, "project_id")
    return workspace_root(root) / project_id


def yukar_dir(root: str, project_id: str) -> Path:
    return project_dir(root, project_id) / ".yukar"


def project_yaml(root: str, project_id: str) -> Path:
    return yukar_dir(root, project_id) / "project.yaml"


def repos_dir(root: str, project_id: str) -> Path:
    return yukar_dir(root, project_id) / "repos"


def repo_yaml(root: str, project_id: str, repo_name: str) -> Path:
    _validate_segment(repo_name, "repo_name")
    return repos_dir(root, project_id) / f"{repo_name}.yaml"


def browser_auth_state(root: str, project_id: str, repo_name: str) -> Path:
    """Saved Playwright storage_state for a repo's app (design §12).

    Written by the host after the user completes an interactive login; injected
    into every agent browser context for the repo.  Agents never read it.
    """
    _validate_segment(repo_name, "repo_name")
    return repos_dir(root, project_id) / f"{repo_name}.browser-auth.json"


def project_docs_dir(root: str, project_id: str) -> Path:
    return project_dir(root, project_id) / "docs"


def project_doc_path(root: str, project_id: str, filename: str) -> Path:
    return project_docs_dir(root, project_id) / filename


def slide_templates_dir(root: str, project_id: str) -> Path:
    """Project-level slide template bundles (a subdirectory of project docs,
    so it is NOT swept into the Manager prompt by the top-level ``*.md`` glob)."""
    return project_docs_dir(root, project_id) / "slide-templates"


def slide_template_dir(root: str, project_id: str, name: str) -> Path:
    _validate_segment(name, "template name")
    return slide_templates_dir(root, project_id) / name


def cache_dir(root: str, project_id: str) -> Path:
    return yukar_dir(root, project_id) / "cache"


def index_dir(root: str, project_id: str, repo_name: str) -> Path:
    _validate_segment(repo_name, "repo_name")
    return cache_dir(root, project_id) / "index" / repo_name


# ---------------------------------------------------------------------------
# Epics
# ---------------------------------------------------------------------------


def epics_dir(root: str, project_id: str) -> Path:
    return project_dir(root, project_id) / "epics"


def epic_dir(root: str, project_id: str, epic_id: str) -> Path:
    _validate_segment(epic_id, "epic_id")
    return epics_dir(root, project_id) / epic_id


def epic_yukar_dir(root: str, project_id: str, epic_id: str) -> Path:
    return epic_dir(root, project_id, epic_id) / ".yukar"


def epic_yaml(root: str, project_id: str, epic_id: str) -> Path:
    return epic_yukar_dir(root, project_id, epic_id) / "epic.yaml"


def tasks_yaml(root: str, project_id: str, epic_id: str) -> Path:
    return epic_yukar_dir(root, project_id, epic_id) / "tasks.yaml"


def state_yaml(root: str, project_id: str, epic_id: str) -> Path:
    return epic_yukar_dir(root, project_id, epic_id) / "state.yaml"


def plan_approval_yaml(root: str, project_id: str, epic_id: str) -> Path:
    """Per-epic plan-approval record (lifecycle redesign).

    Lives next to tasks.yaml / state.yaml but is run-independent: it records
    the task-plan snapshot hash the user approved, and survives run stop /
    error / restart.
    """
    return epic_yukar_dir(root, project_id, epic_id) / "plan_approval.yaml"


def threads_yaml(root: str, project_id: str, epic_id: str) -> Path:
    return epic_dir(root, project_id, epic_id) / "threads.yaml"


def epic_docs_dir(root: str, project_id: str, epic_id: str) -> Path:
    return epic_dir(root, project_id, epic_id) / "docs"


def epic_doc_path(root: str, project_id: str, epic_id: str, filename: str) -> Path:
    return epic_docs_dir(root, project_id, epic_id) / filename


def epic_screenshots_dir(root: str, project_id: str, epic_id: str) -> Path:
    """Directory for browser-verification screenshots the agent chose to keep.

    A subdirectory of the epic docs folder so the markdown doc loaders (which
    glob ``docs/*.md`` non-recursively) never pick these binary files up, while
    the screenshots still live "under the epic docs folder" the user browses.
    """
    return epic_docs_dir(root, project_id, epic_id) / "screenshots"


def epic_screenshot_path(root: str, project_id: str, epic_id: str, filename: str) -> Path:
    _validate_segment(filename, "screenshot filename")
    return epic_screenshots_dir(root, project_id, epic_id) / filename


# ---------------------------------------------------------------------------
# Sessions (Strands FileSessionManager compatible layout)
# ---------------------------------------------------------------------------


def sessions_dir(root: str, project_id: str, epic_id: str) -> Path:
    return epic_dir(root, project_id, epic_id) / "sessions"


def session_dir(root: str, project_id: str, epic_id: str) -> Path:
    """1 Epic = 1 session. session_id = epic_id."""
    return sessions_dir(root, project_id, epic_id) / f"session_{epic_id}"


def session_json(root: str, project_id: str, epic_id: str) -> Path:
    return session_dir(root, project_id, epic_id) / "session.json"


def agents_dir(root: str, project_id: str, epic_id: str) -> Path:
    return session_dir(root, project_id, epic_id) / "agents"


def agent_dir(root: str, project_id: str, epic_id: str, agent_id: str) -> Path:
    _validate_segment(agent_id, "agent_id")
    return agents_dir(root, project_id, epic_id) / f"agent_{agent_id}"


def agent_json(root: str, project_id: str, epic_id: str, agent_id: str) -> Path:
    return agent_dir(root, project_id, epic_id, agent_id) / "agent.json"


def messages_dir(root: str, project_id: str, epic_id: str, agent_id: str) -> Path:
    return agent_dir(root, project_id, epic_id, agent_id) / "messages"


def message_json(root: str, project_id: str, epic_id: str, agent_id: str, index: int) -> Path:
    return messages_dir(root, project_id, epic_id, agent_id) / f"message_{index}.json"


# ---------------------------------------------------------------------------
# Worktrees
# ---------------------------------------------------------------------------


def worktrees_dir(root: str, project_id: str, epic_id: str) -> Path:
    return epic_dir(root, project_id, epic_id) / "worktrees"


def manager_worktrees_dir(root: str, project_id: str, epic_id: str, trial_id: str) -> Path:
    """Return the directory that holds worktrees for one manager trial.

    Layout: epics/{epic_id}/worktrees/{trial_id}/
    ``trial_id`` keys the (branch+worktree) line of work — several manager
    conversations on the same branch share this directory.  Only config/paths.py
    knows this layout (workspace invariant).
    """
    _validate_segment(trial_id, "trial_id")
    return worktrees_dir(root, project_id, epic_id) / trial_id


def worktree_dir(
    root: str, project_id: str, epic_id: str, trial_id: str, repo_name: str
) -> Path:
    """Return the path for a single repo worktree under a manager trial.

    Layout: epics/{epic_id}/worktrees/{trial_id}/{repo_name}
    """
    _validate_segment(repo_name, "repo_name")
    return manager_worktrees_dir(root, project_id, epic_id, trial_id) / repo_name


def trial_id_of_worktree(worktree_path: Path) -> str:
    """Inverse of :func:`worktree_dir` — the trial segment of a worktree path.

    Layout: .../worktrees/{trial_id}/{repo_name}; only config/paths.py knows
    this layout, so consumers that hold a worktree path but not the trial id
    (e.g. AgentContext-scoped tools) recover it here rather than hard-coding
    the parent-directory convention.
    """
    return worktree_path.parent.name


# ---------------------------------------------------------------------------
# Project-level agent config / skills / MCP (Wave 4a)
# ---------------------------------------------------------------------------

_ALLOWED_ROLES = frozenset(get_args(ConfigurableAgentRole))


def project_agents_dir(root: str, project_id: str) -> Path:
    """Per-role instruction markdown files: {role}.md."""
    return yukar_dir(root, project_id) / "agents"


def agent_config_path(root: str, project_id: str, role: str) -> Path:
    """Path to the per-role instruction file.  role must be manager|worker|evaluator."""
    if role not in _ALLOWED_ROLES:
        raise PathSegmentError(f"agent role must be one of {sorted(_ALLOWED_ROLES)}, got {role!r}")
    return project_agents_dir(root, project_id) / f"{role}.md"


def project_skills_dir(root: str, project_id: str) -> Path:
    """Parent directory that holds skill subdirectories (each with SKILL.md)."""
    return project_dir(root, project_id) / "skills"


def skill_dir(root: str, project_id: str, name: str) -> Path:
    """Directory for a single skill."""
    _validate_segment(name, "skill_name")
    return project_skills_dir(root, project_id) / name


def skill_md_path(root: str, project_id: str, name: str) -> Path:
    """Path to SKILL.md for a named skill."""
    return skill_dir(root, project_id, name) / "SKILL.md"


def project_mcp_yaml(root: str, project_id: str) -> Path:
    """Project-level MCP server config: .yukar/mcp.yaml."""
    return yukar_dir(root, project_id) / "mcp.yaml"


# ---------------------------------------------------------------------------
# Project Memory (cross-Epic)
# ---------------------------------------------------------------------------


def memory_jsonl(root: str, project_id: str) -> Path:
    """Source-of-truth JSONL: {project}/.yukar/memory/project.jsonl
    (human-editable, one record per line)."""
    return yukar_dir(root, project_id) / "memory" / "project.jsonl"


def memory_index_dir(root: str, project_id: str) -> Path:
    """Derived FAISS cache: {project}/.yukar/cache/memory/
    (rebuildable from the source of truth)."""
    return cache_dir(root, project_id) / "memory"


# ---------------------------------------------------------------------------
# Agent profiles (Wave 5 BE-A)
# ---------------------------------------------------------------------------


def agent_profiles_dir(root: str, project_id: str) -> Path:
    """Directory that holds per-profile Markdown files: .yukar/agent_profiles/."""
    return yukar_dir(root, project_id) / "agent_profiles"


def agent_profile_path(root: str, project_id: str, name: str) -> Path:
    """Path for a single agent profile Markdown file: .yukar/agent_profiles/{name}.md."""
    _validate_segment(name, "profile_name")
    return agent_profiles_dir(root, project_id) / f"{name}.md"


# ---------------------------------------------------------------------------
# Usage / ledger
# ---------------------------------------------------------------------------


def usage_dir(root: str) -> Path:
    """Workspace-global usage directory."""
    return workspace_root(root) / "usage"


def ledger_yaml(root: str) -> Path:
    """Global token usage ledger YAML."""
    return usage_dir(root) / "ledger.yaml"


def exchange_rate_yaml(root: str) -> Path:
    """Cached exchange rate YAML."""
    return usage_dir(root) / "exchange_rate.yaml"


# ---------------------------------------------------------------------------
# Git hardening — empty hooks directory
# ---------------------------------------------------------------------------


def empty_hooks_dir() -> Path:
    """Return (and guarantee the integrity of) the empty git hooks directory.

    This directory is used to disable git hooks for all hardened ``run_git``
    calls via ``-c core.hooksPath=<path>``.  The directory is located outside
    all project worktrees so sandboxed agents cannot write into it.

    Resolved at call time (not module import time) so that:
    * Tests that change HOME or monkeypatch this function see the updated path.
    * Environment changes between server start and first call are reflected.

    Invariants enforced on every call:
    * The path must be a directory (not a file, symlink, or other fs object).
      If the path exists but is not a directory, raises ``RuntimeError`` — a
      rogue non-directory at this path would silently make core.hooksPath point
      at a non-directory, causing git to fall back to the default hooks dir and
      defeating Tier B suppression entirely.
    * The directory must contain no executable files.  Any stray executable
      hooks are removed to prevent silent Tier B bypass.

    Returns:
        Absolute path to the (empty, verified) hooks directory.

    Raises:
        RuntimeError: If the path exists but is not a directory.
    """
    hooks_dir = Path.home() / ".yukar" / "git-hooks-empty"
    if hooks_dir.exists() and not hooks_dir.is_dir():
        raise RuntimeError(
            f"empty_hooks_dir: {hooks_dir} exists but is not a directory. "
            "Remove the file at this path to allow yukar to create the hooks directory."
        )
    hooks_dir.mkdir(parents=True, exist_ok=True)
    # Remove any stray executable files that could bypass hook suppression.
    try:
        for entry in hooks_dir.iterdir():
            if entry.is_file() and entry.stat().st_mode & 0o111:
                entry.unlink(missing_ok=True)
    except OSError:
        pass  # Best-effort cleanup; failure to clean is logged elsewhere.
    return hooks_dir
