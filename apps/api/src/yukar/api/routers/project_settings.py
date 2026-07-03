"""Project-level settings router — Wave 4a (L1/L2/L3) + Wave 5 BE-A.

Endpoints:
  Agent configs (L1):
    GET  /api/projects/{pid}/agent-configs
    GET  /api/projects/{pid}/agent-configs/{role}
    PUT  /api/projects/{pid}/agent-configs/{role}

  Skills (L2):
    GET    /api/projects/{pid}/skills
    GET    /api/projects/{pid}/skills/{name}
    PUT    /api/projects/{pid}/skills/{name}
    DELETE /api/projects/{pid}/skills/{name}

  MCP (L3):
    GET /api/projects/{pid}/mcp
    PUT /api/projects/{pid}/mcp

  Agent profiles (Wave 5 BE-A):
    GET    /api/projects/{pid}/agent-profiles
    GET    /api/projects/{pid}/agent-profiles/{name}
    PUT    /api/projects/{pid}/agent-profiles/{name}
    DELETE /api/projects/{pid}/agent-profiles/{name}

  Repos (Wave 5 BE-A):
    GET    /api/projects/{pid}/repos
    POST   /api/projects/{pid}/repos
    PUT    /api/projects/{pid}/repos/{repo}/commands
    DELETE /api/projects/{pid}/repos/{repo}

All routes validate project existence (404) before delegating to storage.
Path segment safety is enforced by config/paths.py (_validate_segment /
_ALLOWED_ROLES) — PathSegmentError maps to 422 in app.py.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from yukar.api.routers import get_project_or_404
from yukar.config import paths
from yukar.deps import IndexerServiceDep, WorkspaceRootDep
from yukar.models.agent_config import AgentConfig
from yukar.models.agent_profile import AgentProfile
from yukar.models.mcp import McpConfig
from yukar.models.project import Repo, RepoCommands
from yukar.models.roles import ConfigurableAgentRole
from yukar.models.skill import Skill, SkillMeta
from yukar.storage import agent_config_repo, agent_profiles_repo, mcp_repo, skills_repo
from yukar.storage.project_repo import (
    delete_repo,
    get_repo,
    list_repos,
    resolve_git_repo,
    save_project,
    save_repo,
    update_repo_commands,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["project-settings"])

_ROLES = get_args(ConfigurableAgentRole)


# ---------------------------------------------------------------------------
# Agent configs (L1)
# ---------------------------------------------------------------------------


@router.get("/{project_id}/agent-configs", response_model=dict[str, str])
async def list_agent_configs(project_id: str, root: WorkspaceRootDep) -> dict[str, str]:
    """Return all per-role instructions as a dict of {role: instructions}."""
    await get_project_or_404(root, project_id)
    return {
        role: agent_config_repo.get_agent_instructions(root, project_id, role) for role in _ROLES
    }


@router.get("/{project_id}/agent-configs/{role}", response_model=AgentConfig)
async def get_agent_config(project_id: str, role: str, root: WorkspaceRootDep) -> AgentConfig:
    """Return per-role instructions for a specific role."""
    await get_project_or_404(root, project_id)
    # PathSegmentError is raised by agent_config_path → maps to 422.
    instructions = agent_config_repo.get_agent_instructions(root, project_id, role)
    return AgentConfig.model_validate({"role": role, "instructions": instructions})


class AgentConfigUpdateRequest(BaseModel):
    instructions: str


@router.put("/{project_id}/agent-configs/{role}", response_model=AgentConfig)
async def put_agent_config(
    project_id: str,
    role: str,
    body: AgentConfigUpdateRequest,
    root: WorkspaceRootDep,
) -> AgentConfig:
    """Set per-role instructions for a specific role."""
    await get_project_or_404(root, project_id)
    await agent_config_repo.save_agent_instructions(root, project_id, role, body.instructions)
    return AgentConfig.model_validate({"role": role, "instructions": body.instructions})


# ---------------------------------------------------------------------------
# Skills (L2)
# ---------------------------------------------------------------------------


@router.get("/{project_id}/skills", response_model=list[SkillMeta])
async def list_project_skills(project_id: str, root: WorkspaceRootDep) -> list[SkillMeta]:
    """Return metadata for all skills in this project."""
    await get_project_or_404(root, project_id)
    return skills_repo.list_skills(root, project_id)


@router.get("/{project_id}/skills/{name}", response_model=Skill)
async def get_project_skill(project_id: str, name: str, root: WorkspaceRootDep) -> Skill:
    """Return the full content of a skill by name."""
    await get_project_or_404(root, project_id)
    try:
        return skills_repo.get_skill(root, project_id, name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Skill not found: {name}") from exc


class SkillUpdateRequest(BaseModel):
    content: str


@router.put("/{project_id}/skills/{name}", response_model=Skill)
async def put_project_skill(
    project_id: str,
    name: str,
    body: SkillUpdateRequest,
    root: WorkspaceRootDep,
) -> Skill:
    """Create or replace the SKILL.md content for a named skill."""
    await get_project_or_404(root, project_id)
    await skills_repo.save_skill(root, project_id, name, body.content)
    return skills_repo.get_skill(root, project_id, name)


@router.delete("/{project_id}/skills/{name}", status_code=204)
async def delete_project_skill(project_id: str, name: str, root: WorkspaceRootDep) -> None:
    """Delete a skill by name."""
    await get_project_or_404(root, project_id)
    deleted = skills_repo.delete_skill(root, project_id, name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Skill not found: {name}")


# ---------------------------------------------------------------------------
# MCP (L3)
# ---------------------------------------------------------------------------


@router.get("/{project_id}/mcp", response_model=McpConfig)
async def get_project_mcp(project_id: str, root: WorkspaceRootDep) -> McpConfig:
    """Return the MCP server configuration for this project."""
    await get_project_or_404(root, project_id)
    return mcp_repo.get_mcp_config(root, project_id)


@router.put("/{project_id}/mcp", response_model=McpConfig)
async def put_project_mcp(project_id: str, body: McpConfig, root: WorkspaceRootDep) -> McpConfig:
    """Replace the MCP server configuration for this project."""
    await get_project_or_404(root, project_id)
    await mcp_repo.save_mcp_config(root, project_id, body)
    return body


# ---------------------------------------------------------------------------
# Agent profiles (Wave 5 BE-A)
# ---------------------------------------------------------------------------


@router.get("/{project_id}/agent-profiles", response_model=list[AgentProfile])
async def list_agent_profiles(project_id: str, root: WorkspaceRootDep) -> list[AgentProfile]:
    """Return all named agent profiles for this project."""
    await get_project_or_404(root, project_id)
    return agent_profiles_repo.list_profiles(root, project_id)


@router.get("/{project_id}/agent-profiles/{name}", response_model=AgentProfile)
async def get_agent_profile(project_id: str, name: str, root: WorkspaceRootDep) -> AgentProfile:
    """Return a named agent profile by name."""
    await get_project_or_404(root, project_id)
    profile = agent_profiles_repo.get_profile(root, project_id, name)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Agent profile not found: {name}")
    return profile


@router.put("/{project_id}/agent-profiles/{name}", response_model=AgentProfile)
async def put_agent_profile(
    project_id: str,
    name: str,
    body: AgentProfile,
    root: WorkspaceRootDep,
) -> AgentProfile:
    """Create or replace a named agent profile.

    The ``name`` field in the body is ignored; the path parameter takes precedence.
    """
    await get_project_or_404(root, project_id)
    # Ensure body.name matches path so storage uses the right filename.
    profile = body.model_copy(update={"name": name})
    await agent_profiles_repo.save_profile(root, project_id, profile)
    saved = agent_profiles_repo.get_profile(root, project_id, name)
    if saved is None:
        # Should not happen, but be defensive.
        return profile  # pragma: no cover
    return saved


@router.delete("/{project_id}/agent-profiles/{name}", status_code=204)
async def delete_agent_profile(project_id: str, name: str, root: WorkspaceRootDep) -> None:
    """Delete a named agent profile."""
    await get_project_or_404(root, project_id)
    deleted = agent_profiles_repo.delete_profile(root, project_id, name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Agent profile not found: {name}")


# ---------------------------------------------------------------------------
# Repos (Wave 5 BE-A)
# ---------------------------------------------------------------------------


class AddRepoRequest(BaseModel):
    """Body for adding a repo to an existing project.

    Mirrors ``projects.RepoInput`` — a local git repo already on disk.
    ``name`` defaults to the last path segment when left blank.
    """

    name: str = ""
    path: str
    default_branch: str = "main"
    commands: RepoCommands = Field(default_factory=RepoCommands)


@router.get("/{project_id}/repos", response_model=list[Repo])
async def list_project_repos(project_id: str, root: WorkspaceRootDep) -> list[Repo]:
    """Return all repos for this project including their commands config."""
    await get_project_or_404(root, project_id)
    return await list_repos(root, project_id)


@router.post("/{project_id}/repos", response_model=Repo, status_code=201)
async def add_project_repo(
    project_id: str,
    body: AddRepoRequest,
    root: WorkspaceRootDep,
    indexer: IndexerServiceDep,
    background_tasks: BackgroundTasks,
) -> Repo:
    """Register an existing local git repo with a project and index it.

    Validates the path is a git repo (422) and rejects a name that already
    exists (409). Adds the name to ``project.repos`` and kicks off an initial
    index in the background (does not block the 201).
    """
    project = await get_project_or_404(root, project_id)

    name = body.name.strip() or body.path.rstrip("/").split("/")[-1]
    if not name:
        raise HTTPException(status_code=422, detail="Repo name could not be derived from path")

    # PathSegmentError (bad name) → 422 via app.py handler; check that first so a
    # malformed name never reaches the git-path probe.
    if await get_repo(root, project_id, name) is not None:
        raise HTTPException(status_code=409, detail=f"Repo already exists: {name}")

    try:
        resolve_git_repo(body.path)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    repo = Repo(
        name=name,
        path=body.path,
        default_branch=body.default_branch,
        commands=body.commands,
    )
    await save_repo(root, project_id, repo)

    if name not in project.repos:
        project.repos.append(name)
    project.updated_at = datetime.now(UTC)
    await save_project(root, project)

    async def _initial_index() -> None:
        try:
            n = await indexer.reindex_repo(project_id, name, Path(body.path))
            logger.info("Initial index %s/%s: %d chunks", project_id, name, n)
        except Exception:
            logger.exception("Initial index %s/%s failed", project_id, name)

    background_tasks.add_task(_initial_index)

    return repo


@router.put(
    "/{project_id}/repos/{repo_name}/commands",
    response_model=Repo,
)
async def put_repo_commands(
    project_id: str,
    repo_name: str,
    body: RepoCommands,
    root: WorkspaceRootDep,
) -> Repo:
    """Replace the run_command allow/deny lists for a repo."""
    await get_project_or_404(root, project_id)
    updated = await update_repo_commands(root, project_id, repo_name, body)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Repo not found: {repo_name}")
    return updated


@router.delete("/{project_id}/repos/{repo_name}", status_code=204)
async def remove_project_repo(
    project_id: str,
    repo_name: str,
    root: WorkspaceRootDep,
) -> None:
    """Unregister a repo: drop its YAML, its project.repos entry, and its index.

    yukar never touches the local git repo itself — only the registration.
    Purging the search index is best-effort; a failure there does not block
    the removal.
    """
    project = await get_project_or_404(root, project_id)

    deleted = await delete_repo(root, project_id, repo_name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Repo not found: {repo_name}")

    if repo_name in project.repos:
        project.repos.remove(repo_name)
        project.updated_at = datetime.now(UTC)
        await save_project(root, project)

    # Best-effort: purge the FAISS index cache for this repo.
    index_dir = paths.index_dir(root, project_id, repo_name)
    await asyncio.to_thread(shutil.rmtree, index_dir, ignore_errors=True)
