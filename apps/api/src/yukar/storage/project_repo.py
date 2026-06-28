"""Project and Repo CRUD — maps model ↔ YAML files on disk."""

from __future__ import annotations

import logging
from pathlib import Path

from yukar.config import paths
from yukar.models.project import Project, Repo, RepoCommands
from yukar.storage.yaml_io import (
    load_model_async,
    load_validated_dir_async,
    read_yaml,
    save_model,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


def _project_from_dict(data: dict) -> Project:  # type: ignore[type-arg]
    return Project.model_validate(data)


async def list_projects(root: str) -> list[Project]:
    workspace = paths.workspace_root(root)
    if not workspace.exists():
        return []
    candidate_yamls = [
        paths.project_yaml(root, d.name)
        for d in sorted(workspace.iterdir())
        if d.is_dir() and paths.project_yaml(root, d.name).exists()
    ]
    return await load_validated_dir_async(
        candidate_yamls,
        lambda p: _project_from_dict(read_yaml(p)),
        "project",
    )


async def get_project(root: str, project_id: str) -> Project | None:
    yaml_path = paths.project_yaml(root, project_id)
    return await load_model_async(yaml_path, Project, default=None)


async def save_project(root: str, project: Project) -> None:
    yaml_path = paths.project_yaml(root, project.id)
    await save_model(yaml_path, project)


async def delete_project(root: str, project_id: str) -> bool:
    """Delete project.yaml. Does not remove the full directory."""
    yaml_path = paths.project_yaml(root, project_id)
    if not yaml_path.exists():
        return False
    yaml_path.unlink()
    return True


# ---------------------------------------------------------------------------
# Repo
# ---------------------------------------------------------------------------


async def list_repos(root: str, project_id: str) -> list[Repo]:
    repo_dir = paths.repos_dir(root, project_id)
    if not repo_dir.exists():
        return []
    return await load_validated_dir_async(
        sorted(repo_dir.glob("*.yaml")),
        lambda p: Repo.model_validate(read_yaml(p)),
        "repo",
    )


async def get_repo(root: str, project_id: str, repo_name: str) -> Repo | None:
    yaml_path = paths.repo_yaml(root, project_id, repo_name)
    return await load_model_async(yaml_path, Repo, default=None)


async def save_repo(root: str, project_id: str, repo: Repo) -> None:
    yaml_path = paths.repo_yaml(root, project_id, repo.name)
    await save_model(yaml_path, repo)


async def delete_repo(root: str, project_id: str, repo_name: str) -> bool:
    yaml_path = paths.repo_yaml(root, project_id, repo_name)
    if not yaml_path.exists():
        return False
    yaml_path.unlink()
    return True


async def update_repo_commands(
    root: str,
    project_id: str,
    repo_name: str,
    commands: RepoCommands,
) -> Repo | None:
    """Update the run_command allow/deny lists for an existing repo.

    Reads the current repo YAML, replaces only the ``commands`` field,
    and writes back atomically via save_repo.

    Returns the updated Repo, or None if the repo does not exist.
    """
    repo = await get_repo(root, project_id, repo_name)
    if repo is None:
        return None
    repo.commands = commands
    await save_repo(root, project_id, repo)
    return repo


# ---------------------------------------------------------------------------
# Epic counter
# ---------------------------------------------------------------------------


async def increment_epic_counter(root: str, project_id: str) -> int:
    """Atomically increment epic_counter and return the new value."""
    project = await get_project(root, project_id)
    if project is None:
        raise ValueError(f"Project not found: {project_id}")
    project.epic_counter += 1
    await save_project(root, project)
    return project.epic_counter


def resolve_git_repo(repo_path: str) -> Path:
    """Check that a path is a git repo (.git exists). Returns Path."""
    p = Path(repo_path)
    if not p.exists():
        raise ValueError(f"Path does not exist: {repo_path}")
    if not (p / ".git").exists():
        raise ValueError(f"Not a git repository: {repo_path}")
    return p
