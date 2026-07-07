"""Wave 5 BE-A tests — named agent profiles, Task.agent, RepoInput.commands.

Covers:
1. AgentProfile model validation (incl. base_role literal, RepoCommands embed)
2. config/paths.py: agent_profiles_dir / agent_profile_path
3. storage/agent_profiles_repo: list / get / save / delete + frontmatter roundtrip
4. storage/project_repo: update_repo_commands
5. Task.agent field persistence (backward compat: None default)
6. RepoInput.commands forwarded to Repo.commands on create_project
7. API: /agent-profiles CRUD (404 project guard, PUT/GET/DELETE)
8. API: /repos list + /repos/{repo}/commands PUT
9. Manager tools: make_agent_profile_tools (list/read/write/delete)
10. Manager tools: make_skill_mcp_tools (list_skills/read_skill/write_skill/write_mcp_server)
11. task_update tool: agent field persists to tasks.yaml
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# 1. AgentProfile model
# ---------------------------------------------------------------------------


class TestAgentProfileModel:
    def test_defaults(self) -> None:
        from yukar.models.agent_profile import AgentProfile

        p = AgentProfile(name="frontend-worker", base_role="worker")
        assert p.description == ""
        assert p.instructions == ""
        assert p.skills == []
        assert p.mcp_servers == []

    def test_full_construction(self) -> None:
        from yukar.models.agent_profile import AgentProfile

        p = AgentProfile(
            name="be",
            description="Backend worker",
            base_role="worker",
            instructions="Use pytest.",
            skills=["pytest-patterns"],
            mcp_servers=["github-mcp"],
        )
        assert p.base_role == "worker"
        assert p.skills == ["pytest-patterns"]

    def test_evaluator_base_role(self) -> None:
        from yukar.models.agent_profile import AgentProfile

        p = AgentProfile(name="strict-eval", base_role="evaluator")
        assert p.base_role == "evaluator"

    def test_invalid_base_role(self) -> None:
        from typing import cast

        from pydantic import ValidationError

        from yukar.models.agent_profile import AgentProfile

        with pytest.raises(ValidationError):
            # Cast to silence ty; pydantic will raise ValidationError at runtime.
            bad_role = cast(Any, "manager")
            AgentProfile(name="bad", base_role=bad_role)

    def test_model_dump_roundtrip(self) -> None:
        from yukar.models.agent_profile import AgentProfile

        p = AgentProfile(name="x", base_role="worker", instructions="hi")
        data = p.model_dump(mode="json")
        p2 = AgentProfile.model_validate(data)
        assert p2 == p


# ---------------------------------------------------------------------------
# 2. config/paths.py — new path functions
# ---------------------------------------------------------------------------


class TestAgentProfilePaths:
    def test_agent_profiles_dir(self, tmp_path: Path) -> None:
        from yukar.config.paths import agent_profiles_dir, yukar_dir

        root = str(tmp_path)
        assert agent_profiles_dir(root, "p") == yukar_dir(root, "p") / "agent_profiles"

    def test_agent_profile_path(self, tmp_path: Path) -> None:
        from yukar.config.paths import agent_profile_path, agent_profiles_dir

        root = str(tmp_path)
        expected = agent_profiles_dir(root, "p") / "frontend-worker.md"
        assert agent_profile_path(root, "p", "frontend-worker") == expected

    def test_agent_profile_path_traversal_rejected(self, tmp_path: Path) -> None:
        from yukar.config.paths import PathSegmentError, agent_profile_path

        with pytest.raises(PathSegmentError):
            agent_profile_path(str(tmp_path), "p", "../evil")

    def test_agent_profile_path_empty_rejected(self, tmp_path: Path) -> None:
        from yukar.config.paths import PathSegmentError, agent_profile_path

        with pytest.raises(PathSegmentError):
            agent_profile_path(str(tmp_path), "p", "")


# ---------------------------------------------------------------------------
# 3. storage/agent_profiles_repo
# ---------------------------------------------------------------------------


class TestAgentProfilesRepo:
    def test_list_empty_when_no_dir(self, tmp_path: Path) -> None:
        from yukar.storage.agent_profiles_repo import list_profiles

        assert list_profiles(str(tmp_path), "proj") == []

    @pytest.mark.asyncio
    async def test_save_and_list(self, tmp_path: Path) -> None:
        from yukar.models.agent_profile import AgentProfile
        from yukar.storage.agent_profiles_repo import list_profiles, save_profile

        root = str(tmp_path)
        p = AgentProfile(
            name="frontend-worker",
            description="FE specialist",
            base_role="worker",
            instructions="Use TypeScript.",
        )
        await save_profile(root, "proj", p)
        profiles = list_profiles(root, "proj")
        assert len(profiles) == 1
        assert profiles[0].name == "frontend-worker"
        assert profiles[0].description == "FE specialist"
        assert profiles[0].base_role == "worker"

    @pytest.mark.asyncio
    async def test_save_and_get(self, tmp_path: Path) -> None:
        from yukar.models.agent_profile import AgentProfile
        from yukar.storage.agent_profiles_repo import get_profile, save_profile

        root = str(tmp_path)
        p = AgentProfile(
            name="backend-worker",
            description="BE",
            base_role="worker",
            instructions="Use pytest.",
            skills=["pytest-patterns"],
            mcp_servers=["github-mcp"],
        )
        await save_profile(root, "proj", p)
        loaded = get_profile(root, "proj", "backend-worker")
        assert loaded is not None
        assert loaded.name == "backend-worker"
        assert loaded.instructions == "Use pytest."
        assert loaded.skills == ["pytest-patterns"]
        assert loaded.mcp_servers == ["github-mcp"]

    def test_get_missing_returns_none(self, tmp_path: Path) -> None:
        from yukar.storage.agent_profiles_repo import get_profile

        assert get_profile(str(tmp_path), "proj", "ghost") is None

    @pytest.mark.asyncio
    async def test_delete(self, tmp_path: Path) -> None:
        from yukar.models.agent_profile import AgentProfile
        from yukar.storage.agent_profiles_repo import delete_profile, list_profiles, save_profile

        root = str(tmp_path)
        p = AgentProfile(name="del-me", base_role="worker")
        await save_profile(root, "proj", p)
        assert len(list_profiles(root, "proj")) == 1
        deleted = delete_profile(root, "proj", "del-me")
        assert deleted is True
        assert list_profiles(root, "proj") == []

    def test_delete_missing_returns_false(self, tmp_path: Path) -> None:
        from yukar.storage.agent_profiles_repo import delete_profile

        assert delete_profile(str(tmp_path), "proj", "ghost") is False

    @pytest.mark.asyncio
    async def test_frontmatter_roundtrip(self, tmp_path: Path) -> None:
        """All frontmatter fields survive a write→read cycle."""
        from yukar.models.agent_profile import AgentProfile
        from yukar.storage.agent_profiles_repo import get_profile, save_profile

        root = str(tmp_path)
        original = AgentProfile(
            name="full-profile",
            description="Full test",
            base_role="evaluator",
            instructions="Be strict.\n\nAlways check tests.",
            skills=["skill-a", "skill-b"],
            mcp_servers=["server-1"],
        )
        await save_profile(root, "proj", original)
        loaded = get_profile(root, "proj", "full-profile")
        assert loaded is not None
        assert loaded.name == original.name
        assert loaded.description == original.description
        assert loaded.base_role == original.base_role
        assert "Be strict." in loaded.instructions
        assert "Always check tests." in loaded.instructions
        assert loaded.skills == original.skills
        assert loaded.mcp_servers == original.mcp_servers

    @pytest.mark.asyncio
    async def test_empty_instructions(self, tmp_path: Path) -> None:
        """Profile with no instructions body still round-trips correctly."""
        from yukar.models.agent_profile import AgentProfile
        from yukar.storage.agent_profiles_repo import get_profile, save_profile

        root = str(tmp_path)
        p = AgentProfile(name="empty-body", base_role="worker", instructions="")
        await save_profile(root, "proj", p)
        loaded = get_profile(root, "proj", "empty-body")
        assert loaded is not None
        assert loaded.instructions == ""

    def test_legacy_command_frontmatter_ignored_on_load(self, tmp_path: Path) -> None:
        """Legacy profiles carrying a command allowlist still load (keys ignored).

        Per-profile command control was removed; a profile file written by an
        older version may still carry ``allowed_commands`` or the even-older
        ``commands: {allow, deny}`` block.  Loading must not crash — the keys are
        simply ignored (command permissions come solely from the repo config).
        """
        from yukar.config import paths as p
        from yukar.storage.agent_profiles_repo import get_profile

        root = str(tmp_path)
        # allowed_commands form
        path_a = p.agent_profile_path(root, "proj", "legacy-allowed")
        path_a.parent.mkdir(parents=True, exist_ok=True)
        path_a.write_text(
            "---\n"
            "name: legacy-allowed\n"
            "description: legacy\n"
            "base_role: worker\n"
            "skills: []\n"
            "mcp_servers: []\n"
            "allowed_commands:\n  - pytest\n  - npm test\n"
            "---\n"
            "Legacy body.\n"
        )
        # even-older commands:{allow,deny} form
        path_b = p.agent_profile_path(root, "proj", "legacy-commands")
        path_b.write_text(
            "---\n"
            "name: legacy-commands\n"
            "base_role: evaluator\n"
            "commands:\n  allow:\n    - pytest\n  deny:\n    - rm\n"
            "---\n"
        )

        loaded_a = get_profile(root, "proj", "legacy-allowed")
        assert loaded_a is not None
        assert loaded_a.base_role == "worker"
        assert loaded_a.instructions.strip() == "Legacy body."
        assert not hasattr(loaded_a, "allowed_commands")

        loaded_b = get_profile(root, "proj", "legacy-commands")
        assert loaded_b is not None
        assert loaded_b.base_role == "evaluator"
        assert not hasattr(loaded_b, "allowed_commands")

    @pytest.mark.asyncio
    async def test_multiple_profiles(self, tmp_path: Path) -> None:
        from yukar.models.agent_profile import AgentProfile
        from yukar.storage.agent_profiles_repo import list_profiles, save_profile

        root = str(tmp_path)
        for name in ("alpha-worker", "beta-worker", "gamma-eval"):
            role = "evaluator" if "eval" in name else "worker"
            await save_profile(root, "proj", AgentProfile(name=name, base_role=role))  # type: ignore[arg-type]
        profiles = list_profiles(root, "proj")
        assert len(profiles) == 3
        names = [p.name for p in profiles]
        assert "alpha-worker" in names and "beta-worker" in names


# ---------------------------------------------------------------------------
# 4. storage/project_repo: update_repo_commands
# ---------------------------------------------------------------------------


class TestUpdateRepoCommands:
    @pytest.mark.asyncio
    async def test_update_commands(self, tmp_path: Path) -> None:
        from yukar.models.project import Repo, RepoCommands
        from yukar.storage.project_repo import get_repo, save_repo, update_repo_commands

        root = str(tmp_path)
        repo = Repo(name="api", path="/tmp/api")
        await save_repo(root, "proj", repo)

        updated = await update_repo_commands(
            root, "proj", "api", RepoCommands(allow=["pytest"], deny=["rm"])
        )
        assert updated is not None
        assert updated.commands.allow == ["pytest"]
        assert updated.commands.deny == ["rm"]

        # Persisted correctly.
        loaded = await get_repo(root, "proj", "api")
        assert loaded is not None
        assert loaded.commands.allow == ["pytest"]

    @pytest.mark.asyncio
    async def test_update_missing_repo_returns_none(self, tmp_path: Path) -> None:
        from yukar.models.project import RepoCommands
        from yukar.storage.project_repo import update_repo_commands

        result = await update_repo_commands(str(tmp_path), "proj", "ghost", RepoCommands())
        assert result is None


# ---------------------------------------------------------------------------
# 5. Task.agent field persistence
# ---------------------------------------------------------------------------


class TestTaskAgentField:
    def test_agent_defaults_to_none(self) -> None:
        from yukar.models.task import Task

        t = Task(id="T1", title="Test")
        assert t.agent is None

    def test_agent_can_be_set(self) -> None:
        from yukar.models.task import Task

        t = Task(id="T1", title="Test", agent="frontend-worker")
        assert t.agent == "frontend-worker"

    def test_backward_compat_missing_agent_field(self) -> None:
        """Existing tasks.yaml without 'agent' key should parse as None."""
        from yukar.models.task import Task

        t = Task.model_validate({"id": "T1", "title": "Old task"})
        assert t.agent is None

    @pytest.mark.asyncio
    async def test_task_agent_persisted_to_yaml(self, tmp_path: Path) -> None:
        from yukar.models.task import Task, TasksFile
        from yukar.storage.tasks_repo import get_tasks, save_tasks

        root = str(tmp_path)
        tf = TasksFile(tasks=[Task(id="T1", title="T", agent="backend-worker")])
        await save_tasks(root, "proj", "ep-1", tf)

        loaded = await get_tasks(root, "proj", "ep-1")
        assert loaded.tasks[0].agent == "backend-worker"

    @pytest.mark.asyncio
    async def test_task_agent_none_persisted_and_reloaded(self, tmp_path: Path) -> None:
        from yukar.models.task import Task, TasksFile
        from yukar.storage.tasks_repo import get_tasks, save_tasks

        root = str(tmp_path)
        tf = TasksFile(tasks=[Task(id="T1", title="T")])
        await save_tasks(root, "proj", "ep-1", tf)

        loaded = await get_tasks(root, "proj", "ep-1")
        assert loaded.tasks[0].agent is None


# ---------------------------------------------------------------------------
# 6. RepoInput.commands forwarded to Repo.commands
# ---------------------------------------------------------------------------


class TestRepoInputCommands:
    def test_repoinput_defaults(self) -> None:
        from yukar.api.routers.projects import RepoInput

        r = RepoInput(name="api", path="/tmp/repo")
        assert r.commands.allow == []
        assert r.commands.deny == []

    def test_repoinput_with_commands(self) -> None:
        from yukar.api.routers.projects import RepoInput
        from yukar.models.project import RepoCommands

        r = RepoInput(
            name="api",
            path="/tmp/repo",
            commands=RepoCommands(allow=["pytest"], deny=["rm"]),
        )
        assert r.commands.allow == ["pytest"]
        assert r.commands.deny == ["rm"]

    @pytest.mark.asyncio
    async def test_create_project_persists_commands(
        self, app_client: Any, tmp_workspace: Path, fixture_git_repo: Path
    ) -> None:
        """RepoInput.commands is written to the repo YAML on project creation."""
        body = {
            "id": "cmd-proj",
            "name": "Cmd Test",
            "repos": [
                {
                    "name": "api",
                    "path": str(fixture_git_repo),
                    "commands": {"allow": ["pytest", "npm test"], "deny": ["curl"]},
                }
            ],
        }
        resp = await app_client.post("/api/projects", json=body)
        assert resp.status_code == 201

        # Verify via the repos API that commands were persisted.
        root = str(tmp_workspace)
        from yukar.storage.project_repo import get_repo

        repo = await get_repo(root, "cmd-proj", "api")
        assert repo is not None
        assert repo.commands.allow == ["pytest", "npm test"]
        assert repo.commands.deny == ["curl"]


# ---------------------------------------------------------------------------
# 7. API: /agent-profiles CRUD
# ---------------------------------------------------------------------------


class TestAgentProfilesAPI:
    @pytest.mark.asyncio
    async def test_list_404_for_missing_project(self, app_client: Any) -> None:
        resp = await app_client.get("/api/projects/noexist/agent-profiles")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_empty(self, app_client: Any, tmp_workspace: Path) -> None:
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        await save_project(str(tmp_workspace), Project(id="p", name="p"))
        resp = await app_client.get("/api/projects/p/agent-profiles")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_put_creates_profile(self, app_client: Any, tmp_workspace: Path) -> None:
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        await save_project(str(tmp_workspace), Project(id="p", name="p"))
        body = {
            "name": "ignored",  # should be overridden by path param
            "description": "Frontend specialist",
            "base_role": "worker",
            "instructions": "Use React.",
            "skills": [],
            "mcp_servers": [],
            "commands": {"allow": [], "deny": []},
        }
        resp = await app_client.put("/api/projects/p/agent-profiles/frontend-worker", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "frontend-worker"
        assert data["description"] == "Frontend specialist"
        assert data["base_role"] == "worker"

    @pytest.mark.asyncio
    async def test_get_profile(self, app_client: Any, tmp_workspace: Path) -> None:
        from yukar.models.agent_profile import AgentProfile
        from yukar.models.project import Project
        from yukar.storage.agent_profiles_repo import save_profile
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p"))
        await save_profile(root, "p", AgentProfile(name="be-worker", base_role="worker"))

        resp = await app_client.get("/api/projects/p/agent-profiles/be-worker")
        assert resp.status_code == 200
        assert resp.json()["name"] == "be-worker"

    @pytest.mark.asyncio
    async def test_get_profile_404(self, app_client: Any, tmp_workspace: Path) -> None:
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        await save_project(str(tmp_workspace), Project(id="p", name="p"))
        resp = await app_client.get("/api/projects/p/agent-profiles/ghost")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_profile(self, app_client: Any, tmp_workspace: Path) -> None:
        from yukar.models.agent_profile import AgentProfile
        from yukar.models.project import Project
        from yukar.storage.agent_profiles_repo import save_profile
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p"))
        await save_profile(root, "p", AgentProfile(name="del-me", base_role="worker"))

        resp = await app_client.delete("/api/projects/p/agent-profiles/del-me")
        assert resp.status_code == 204

        resp2 = await app_client.get("/api/projects/p/agent-profiles/del-me")
        assert resp2.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_missing_404(self, app_client: Any, tmp_workspace: Path) -> None:
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        await save_project(str(tmp_workspace), Project(id="p", name="p"))
        resp = await app_client.delete("/api/projects/p/agent-profiles/ghost")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_traversal_in_name_422(self, app_client: Any, tmp_workspace: Path) -> None:
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        await save_project(str(tmp_workspace), Project(id="p", name="p"))
        resp = await app_client.get("/api/projects/p/agent-profiles/../evil")
        # FastAPI will not route this; expect 404 or 422
        assert resp.status_code in (404, 422)


# ---------------------------------------------------------------------------
# 8. API: /repos list + /repos/{repo}/commands PUT
# ---------------------------------------------------------------------------


class TestReposCommandsAPI:
    @pytest.mark.asyncio
    async def test_list_repos_404_for_missing_project(self, app_client: Any) -> None:
        resp = await app_client.get("/api/projects/noexist/repos")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_repos_empty(self, app_client: Any, tmp_workspace: Path) -> None:
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        await save_project(str(tmp_workspace), Project(id="p", name="p"))
        resp = await app_client.get("/api/projects/p/repos")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_repos_includes_commands(self, app_client: Any, tmp_workspace: Path) -> None:
        from yukar.models.project import Project, Repo, RepoCommands
        from yukar.storage.project_repo import save_project, save_repo

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p", repos=["api"]))
        await save_repo(
            root, "p", Repo(name="api", path="/tmp/api", commands=RepoCommands(allow=["pytest"]))
        )
        resp = await app_client.get("/api/projects/p/repos")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "api"
        assert data[0]["commands"]["allow"] == ["pytest"]

    @pytest.mark.asyncio
    async def test_put_repo_commands(self, app_client: Any, tmp_workspace: Path) -> None:
        from yukar.models.project import Project, Repo
        from yukar.storage.project_repo import save_project, save_repo

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p", repos=["api"]))
        await save_repo(root, "p", Repo(name="api", path="/tmp/api"))

        resp = await app_client.put(
            "/api/projects/p/repos/api/commands",
            json={"allow": ["pytest", "npm test"], "deny": ["rm"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["commands"]["allow"] == ["pytest", "npm test"]
        assert data["commands"]["deny"] == ["rm"]

    @pytest.mark.asyncio
    async def test_put_repo_commands_missing_repo_404(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        await save_project(str(tmp_workspace), Project(id="p", name="p"))
        resp = await app_client.put(
            "/api/projects/p/repos/ghost/commands",
            json={"allow": [], "deny": []},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 8b. API: add (POST) / remove (DELETE) repos on an existing project
# ---------------------------------------------------------------------------


class TestReposAddDeleteAPI:
    @pytest.mark.asyncio
    async def test_add_repo_success(
        self, app_client: Any, tmp_workspace: Path, tmp_path: Path
    ) -> None:
        from tests._helpers import make_git_repo
        from yukar.models.project import Project
        from yukar.storage.project_repo import get_project, get_repo, save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p"))
        repo_path = make_git_repo(tmp_path, "svc")

        resp = await app_client.post(
            "/api/projects/p/repos",
            json={"name": "svc", "path": str(repo_path), "default_branch": "main"},
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "svc"

        # Repo yaml persisted and project.repos updated in sync.
        assert await get_repo(root, "p", "svc") is not None
        project = await get_project(root, "p")
        assert project is not None
        assert "svc" in project.repos

    @pytest.mark.asyncio
    async def test_add_repo_derives_name_from_path(
        self, app_client: Any, tmp_workspace: Path, tmp_path: Path
    ) -> None:
        from tests._helpers import make_git_repo
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p"))
        repo_path = make_git_repo(tmp_path, "derived")

        resp = await app_client.post(
            "/api/projects/p/repos",
            json={"name": "", "path": str(repo_path)},
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "derived"

    @pytest.mark.asyncio
    async def test_add_repo_duplicate_name_409(
        self, app_client: Any, tmp_workspace: Path, tmp_path: Path
    ) -> None:
        from tests._helpers import make_git_repo
        from yukar.models.project import Project, Repo
        from yukar.storage.project_repo import save_project, save_repo

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p", repos=["svc"]))
        await save_repo(root, "p", Repo(name="svc", path="/tmp/old"))
        repo_path = make_git_repo(tmp_path, "svc")

        resp = await app_client.post(
            "/api/projects/p/repos",
            json={"name": "svc", "path": str(repo_path)},
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_add_repo_non_git_path_422(
        self, app_client: Any, tmp_workspace: Path, tmp_path: Path
    ) -> None:
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p"))
        # A real directory that is NOT a git repo.
        not_git = tmp_path / "plain"
        not_git.mkdir()

        resp = await app_client.post(
            "/api/projects/p/repos",
            json={"name": "plain", "path": str(not_git)},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_add_repo_missing_project_404(
        self, app_client: Any, tmp_path: Path
    ) -> None:
        from tests._helpers import make_git_repo

        repo_path = make_git_repo(tmp_path, "svc")
        resp = await app_client.post(
            "/api/projects/noexist/repos",
            json={"name": "svc", "path": str(repo_path)},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_repo_success(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        from yukar.models.project import Project, Repo
        from yukar.storage.project_repo import get_project, get_repo, save_project, save_repo

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p", repos=["svc"]))
        await save_repo(root, "p", Repo(name="svc", path="/tmp/svc"))

        resp = await app_client.delete("/api/projects/p/repos/svc")
        assert resp.status_code == 204

        # Repo yaml gone and project.repos entry dropped.
        assert await get_repo(root, "p", "svc") is None
        project = await get_project(root, "p")
        assert project is not None
        assert "svc" not in project.repos

    @pytest.mark.asyncio
    async def test_delete_repo_purges_index(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        from yukar.config import paths
        from yukar.models.project import Project, Repo
        from yukar.storage.project_repo import save_project, save_repo

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p", repos=["svc"]))
        await save_repo(root, "p", Repo(name="svc", path="/tmp/svc"))

        # Simulate a pre-existing index cache for the repo.
        index_dir = paths.index_dir(root, "p", "svc")
        index_dir.mkdir(parents=True, exist_ok=True)
        (index_dir / "faiss.index").write_text("stub")

        resp = await app_client.delete("/api/projects/p/repos/svc")
        assert resp.status_code == 204
        assert not index_dir.exists()

    @pytest.mark.asyncio
    async def test_delete_repo_missing_404(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        await save_project(str(tmp_workspace), Project(id="p", name="p"))
        resp = await app_client.delete("/api/projects/p/repos/ghost")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_repo_missing_project_404(self, app_client: Any) -> None:
        resp = await app_client.delete("/api/projects/noexist/repos/svc")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 9. Manager tools: make_agent_profile_tools
# ---------------------------------------------------------------------------


class TestAgentProfileTools:
    def _unwrap(self, tool: Any) -> Any:
        """Extract the underlying function from a Strands @tool object."""
        return tool.func if hasattr(tool, "func") else tool.__wrapped__

    @pytest.mark.asyncio
    async def test_write_and_list(self, tmp_path: Path) -> None:
        from yukar.agents.tools.agent_profile_tools import make_agent_profile_tools

        tools = make_agent_profile_tools(str(tmp_path), "proj")
        list_fn = self._unwrap(tools[0])
        write_fn = self._unwrap(tools[2])

        result = await write_fn(
            name="fe-worker",
            description="Frontend",
            base_role="worker",
            instructions="Use React.",
        )
        assert result["ok"] is True

        listed = list_fn()
        assert len(listed["profiles"]) == 1
        assert listed["profiles"][0]["name"] == "fe-worker"

    @pytest.mark.asyncio
    async def test_read_profile(self, tmp_path: Path) -> None:
        from yukar.agents.tools.agent_profile_tools import make_agent_profile_tools

        tools = make_agent_profile_tools(str(tmp_path), "proj")
        read_fn = self._unwrap(tools[1])
        write_fn = self._unwrap(tools[2])

        await write_fn(name="be", description="Backend", base_role="worker")
        result = read_fn(name="be")
        assert result["name"] == "be"
        assert result["description"] == "Backend"

    def test_read_missing_profile(self, tmp_path: Path) -> None:
        from yukar.agents.tools.agent_profile_tools import make_agent_profile_tools

        tools = make_agent_profile_tools(str(tmp_path), "proj")
        read_fn = self._unwrap(tools[1])
        result = read_fn(name="ghost")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_delete_profile(self, tmp_path: Path) -> None:
        from yukar.agents.tools.agent_profile_tools import make_agent_profile_tools

        tools = make_agent_profile_tools(str(tmp_path), "proj")
        write_fn = self._unwrap(tools[2])
        delete_fn = self._unwrap(tools[3])

        await write_fn(name="del", description="", base_role="worker")
        result = delete_fn(name="del")
        assert result["ok"] is True

        # Double-delete returns error.
        result2 = delete_fn(name="del")
        assert result2["ok"] is False

    @pytest.mark.asyncio
    async def test_write_cannot_set_commands(self, tmp_path: Path) -> None:
        """The Manager cannot set command permissions via a profile.

        Command scope comes solely from the repo-level allow/deny list, so
        ``write_agent_profile`` intentionally exposes no ``allowed_commands``
        argument — passing one is a TypeError.  The AgentProfile model has no
        such field at all.
        """
        from yukar.agents.tools.agent_profile_tools import make_agent_profile_tools
        from yukar.storage.agent_profiles_repo import get_profile

        root = str(tmp_path)
        tools = make_agent_profile_tools(root, "proj")
        write_fn = self._unwrap(tools[2])

        with pytest.raises(TypeError):
            await write_fn(
                name="cmd-worker",
                description="",
                base_role="worker",
                allowed_commands=["pytest"],
            )

        # A profile still creates fine without any command-permission concept.
        await write_fn(name="cmd-worker", description="", base_role="worker")
        profile = get_profile(root, "proj", "cmd-worker")
        assert profile is not None
        assert not hasattr(profile, "allowed_commands")

    @pytest.mark.asyncio
    async def test_partial_update_preserves_unspecified_fields(self, tmp_path: Path) -> None:
        """Read-merge: omitting a field on update must NOT wipe it (the clobber bug)."""
        from yukar.agents.tools.agent_profile_tools import make_agent_profile_tools
        from yukar.storage.agent_profiles_repo import get_profile

        root = str(tmp_path)
        tools = make_agent_profile_tools(root, "proj")
        write_fn = self._unwrap(tools[2])

        # Create a profile with skills + MCP servers.
        await write_fn(
            name="fe-worker",
            description="Frontend",
            base_role="worker",
            skills=["react"],
            mcp_servers=["playwright"],
        )
        # Update ONLY the instructions — omit skills and mcp_servers.
        result = await write_fn(name="fe-worker", instructions="Prefer TypeScript.")
        assert result["ok"] is True
        assert result["unchanged"] is False

        profile = get_profile(root, "proj", "fe-worker")
        assert profile is not None
        assert profile.instructions == "Prefer TypeScript."
        # The omitted fields survive (previously they were clobbered to []).
        assert profile.skills == ["react"]
        assert profile.mcp_servers == ["playwright"]
        assert profile.base_role == "worker"
        assert profile.description == "Frontend"

    @pytest.mark.asyncio
    async def test_explicit_empty_list_clears_skills(self, tmp_path: Path) -> None:
        """Passing an explicit [] (not None) clears a list field."""
        from yukar.agents.tools.agent_profile_tools import make_agent_profile_tools
        from yukar.storage.agent_profiles_repo import get_profile

        root = str(tmp_path)
        tools = make_agent_profile_tools(root, "proj")
        write_fn = self._unwrap(tools[2])

        await write_fn(name="w", description="", base_role="worker", skills=["react"])
        await write_fn(name="w", skills=[])
        profile = get_profile(root, "proj", "w")
        assert profile is not None
        assert profile.skills == []

    @pytest.mark.asyncio
    async def test_noop_update_is_skipped(self, tmp_path: Path) -> None:
        """Re-writing an identical profile is reported as unchanged (anti-churn)."""
        from yukar.agents.tools.agent_profile_tools import make_agent_profile_tools

        root = str(tmp_path)
        tools = make_agent_profile_tools(root, "proj")
        write_fn = self._unwrap(tools[2])

        await write_fn(name="w", description="d", base_role="worker", skills=["react"])
        # Same values again → no-op.
        result = await write_fn(name="w", description="d", base_role="worker", skills=["react"])
        assert result["ok"] is True
        assert result["unchanged"] is True

    @pytest.mark.asyncio
    async def test_create_requires_base_role(self, tmp_path: Path) -> None:
        """Creating a brand-new profile without base_role is an error."""
        from yukar.agents.tools.agent_profile_tools import make_agent_profile_tools

        tools = make_agent_profile_tools(str(tmp_path), "proj")
        write_fn = self._unwrap(tools[2])

        result = await write_fn(name="new-one", description="x")
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_write_invalid_base_role_returns_error(self, tmp_path: Path) -> None:
        from yukar.agents.tools.agent_profile_tools import make_agent_profile_tools

        tools = make_agent_profile_tools(str(tmp_path), "proj")
        write_fn = self._unwrap(tools[2])

        result = await write_fn(name="bad", description="", base_role="manager")
        assert result["ok"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# 10. Manager tools: make_skill_mcp_tools
# ---------------------------------------------------------------------------


class TestSkillMcpTools:
    def _unwrap(self, tool: Any) -> Any:
        return tool.func if hasattr(tool, "func") else tool.__wrapped__

    @pytest.mark.asyncio
    async def test_write_and_list_skills(self, tmp_path: Path) -> None:
        from yukar.agents.tools.skill_mcp_tools import make_skill_mcp_tools

        tools = make_skill_mcp_tools(str(tmp_path), "proj")
        list_fn = self._unwrap(tools[0])
        write_fn = self._unwrap(tools[2])

        result = await write_fn(
            name="pytest-patterns",
            content="---\nname: pytest-patterns\ndescription: Patterns\n---\n# Pytest",
        )
        assert result["ok"] is True

        listed = list_fn()
        assert any(s["name"] == "pytest-patterns" for s in listed["skills"])

    @pytest.mark.asyncio
    async def test_read_skill(self, tmp_path: Path) -> None:
        from yukar.agents.tools.skill_mcp_tools import make_skill_mcp_tools

        tools = make_skill_mcp_tools(str(tmp_path), "proj")
        read_fn = self._unwrap(tools[1])
        write_fn = self._unwrap(tools[2])

        await write_fn(name="my-skill", content="# Content")
        result = read_fn(name="my-skill")
        # content is now list[dict] (content-block format), markdown body is
        # in result["content"][0]["text"].
        assert result["status"] == "success"
        assert isinstance(result["content"], list)
        assert "Content" in result["content"][0]["text"]
        # Structural metadata is preserved as top-level keys.
        assert result["name"] == "my-skill"

    def test_read_missing_skill(self, tmp_path: Path) -> None:
        from yukar.agents.tools.skill_mcp_tools import make_skill_mcp_tools

        tools = make_skill_mcp_tools(str(tmp_path), "proj")
        read_fn = self._unwrap(tools[1])
        result = read_fn(name="ghost")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_write_mcp_server(self, tmp_path: Path) -> None:
        from yukar.agents.tools.skill_mcp_tools import make_skill_mcp_tools
        from yukar.storage.mcp_repo import get_mcp_config

        root = str(tmp_path)
        tools = make_skill_mcp_tools(root, "proj")
        write_mcp_fn = self._unwrap(tools[3])

        result = await write_mcp_fn(
            name="github-mcp",
            server_type="stdio",
            command="npx",
            args=["@github/mcp"],
        )
        assert result["ok"] is True

        cfg = get_mcp_config(root, "proj")
        assert any(s.name == "github-mcp" for s in cfg.servers)

    @pytest.mark.asyncio
    async def test_write_mcp_server_upsert(self, tmp_path: Path) -> None:
        """Writing the same server name twice should update, not duplicate."""
        from yukar.agents.tools.skill_mcp_tools import make_skill_mcp_tools
        from yukar.storage.mcp_repo import get_mcp_config

        root = str(tmp_path)
        tools = make_skill_mcp_tools(root, "proj")
        write_mcp_fn = self._unwrap(tools[3])

        await write_mcp_fn(name="server1", server_type="stdio", command="npx")
        await write_mcp_fn(name="server1", server_type="stdio", command="node")

        cfg = get_mcp_config(root, "proj")
        matching = [s for s in cfg.servers if s.name == "server1"]
        assert len(matching) == 1
        assert matching[0].command == "node"


# ---------------------------------------------------------------------------
# 11. task_update tool: agent field persists to tasks.yaml
# ---------------------------------------------------------------------------


class TestTaskUpdateAgentField:
    @pytest.mark.asyncio
    async def test_task_update_sets_agent(self, tmp_path: Path) -> None:
        """The task_update Strands tool writes agent to tasks.yaml via agent_profile param."""
        from yukar.agents.orchestrator import _make_task_update_tool
        from yukar.models.task import TasksFile
        from yukar.storage.tasks_repo import get_tasks

        root = str(tmp_path)
        tasks_holder: list[TasksFile] = [TasksFile()]
        tool = _make_task_update_tool(root, "proj", "ep-1", "run-1", tasks_holder)
        fn = tool.func if hasattr(tool, "func") else tool.__wrapped__

        result = await fn(
            task_id="T1",
            title="Frontend task",
            status="todo",
            agent_profile="frontend-worker",
        )
        assert result["task_id"] == "T1"

        loaded = await get_tasks(root, "proj", "ep-1")
        assert loaded.tasks[0].agent == "frontend-worker"

    @pytest.mark.asyncio
    async def test_task_update_agent_none_by_default(self, tmp_path: Path) -> None:
        from yukar.agents.orchestrator import _make_task_update_tool
        from yukar.models.task import TasksFile
        from yukar.storage.tasks_repo import get_tasks

        root = str(tmp_path)
        tasks_holder: list[TasksFile] = [TasksFile()]
        tool = _make_task_update_tool(root, "proj", "ep-1", "run-1", tasks_holder)
        fn = tool.func if hasattr(tool, "func") else tool.__wrapped__

        await fn(task_id="T1", title="Task", status="todo")
        loaded = await get_tasks(root, "proj", "ep-1")
        assert loaded.tasks[0].agent is None

    @pytest.mark.asyncio
    async def test_task_update_updates_existing_task_agent(self, tmp_path: Path) -> None:
        """Calling task_update on an existing task with agent_profile= updates the field."""
        from yukar.agents.orchestrator import _make_task_update_tool
        from yukar.models.task import Task, TasksFile
        from yukar.storage.tasks_repo import get_tasks

        root = str(tmp_path)
        initial_tf = TasksFile(tasks=[Task(id="T1", title="Old")])
        tasks_holder: list[TasksFile] = [initial_tf]
        tool = _make_task_update_tool(root, "proj", "ep-1", "run-1", tasks_holder)
        fn = tool.func if hasattr(tool, "func") else tool.__wrapped__

        await fn(task_id="T1", title="Old", status="todo", agent_profile="backend-worker")
        loaded = await get_tasks(root, "proj", "ep-1")
        assert loaded.tasks[0].agent == "backend-worker"

    def test_task_update_tool_spec_includes_agent_profile(self, tmp_path: Path) -> None:
        """agent_profile must appear in the Strands tool spec (not filtered as special param)."""
        from yukar.agents.orchestrator import _make_task_update_tool
        from yukar.models.task import TasksFile

        tasks_holder: list[TasksFile] = [TasksFile()]
        tool = _make_task_update_tool(str(tmp_path), "proj", "ep-1", "run-1", tasks_holder)
        props = tool.tool_spec["inputSchema"]["json"]["properties"]
        assert "agent_profile" in props, (
            "agent_profile must be in tool spec — 'agent' is reserved by Strands and filtered out"
        )
