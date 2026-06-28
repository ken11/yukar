"""Wave 4a tests — project-level agent settings (L1/L2/L3).

Covers:
1. paths.py: new path functions for agent config / skills / mcp
2. storage/agent_config_repo: get/save instructions (CRUD + backward compat)
3. storage/skills_repo: list/get/save/delete + frontmatter parsing
4. storage/mcp_repo: get/save McpConfig (round-trip, empty default)
5. API routes: /agent-configs, /skills, /mcp (404 project guard, CRUD)
6. McpClientManager: start/stop/get_tools with monkeypatched MCPClient
7. project_extras: build_skills_plugin (with/without skills), overlay_system_prompt
8. Manager tools: write_agent_config / read_agent_config
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1. paths.py — new functions
# ---------------------------------------------------------------------------


class TestPaths:
    def test_project_agents_dir(self, tmp_path: Path) -> None:
        from yukar.config.paths import project_agents_dir, yukar_dir

        root = str(tmp_path)
        pid = "proj1"
        assert project_agents_dir(root, pid) == yukar_dir(root, pid) / "agents"

    def test_agent_config_path_valid_roles(self, tmp_path: Path) -> None:
        from yukar.config.paths import agent_config_path, project_agents_dir

        root = str(tmp_path)
        pid = "p"
        for role in ("manager", "worker", "evaluator"):
            expected = project_agents_dir(root, pid) / f"{role}.md"
            assert agent_config_path(root, pid, role) == expected

    def test_agent_config_path_invalid_role(self, tmp_path: Path) -> None:
        from yukar.config.paths import PathSegmentError, agent_config_path

        with pytest.raises(PathSegmentError):
            agent_config_path(str(tmp_path), "p", "admin")

    def test_project_skills_dir(self, tmp_path: Path) -> None:
        from yukar.config.paths import project_dir, project_skills_dir

        root = str(tmp_path)
        pid = "p"
        assert project_skills_dir(root, pid) == project_dir(root, pid) / "skills"

    def test_skill_md_path(self, tmp_path: Path) -> None:
        from yukar.config.paths import project_skills_dir, skill_md_path

        root = str(tmp_path)
        pid = "p"
        name = "my-skill"
        expected = project_skills_dir(root, pid) / name / "SKILL.md"
        assert skill_md_path(root, pid, name) == expected

    def test_skill_md_path_traversal_rejected(self, tmp_path: Path) -> None:
        from yukar.config.paths import PathSegmentError, skill_md_path

        with pytest.raises(PathSegmentError):
            skill_md_path(str(tmp_path), "p", "../evil")

    def test_project_mcp_yaml(self, tmp_path: Path) -> None:
        from yukar.config.paths import project_mcp_yaml, yukar_dir

        root = str(tmp_path)
        pid = "p"
        assert project_mcp_yaml(root, pid) == yukar_dir(root, pid) / "mcp.yaml"


# ---------------------------------------------------------------------------
# 2. storage/agent_config_repo
# ---------------------------------------------------------------------------


class TestAgentConfigRepo:
    @pytest.mark.asyncio
    async def test_get_returns_empty_when_missing(self, tmp_path: Path) -> None:
        from yukar.storage.agent_config_repo import get_agent_instructions

        result = get_agent_instructions(str(tmp_path), "proj", "worker")
        assert result == ""

    @pytest.mark.asyncio
    async def test_save_and_get_roundtrip(self, tmp_path: Path) -> None:
        from yukar.storage.agent_config_repo import (
            get_agent_instructions,
            save_agent_instructions,
        )

        root = str(tmp_path)
        await save_agent_instructions(root, "proj", "worker", "# Custom\nUse pytest.")
        result = get_agent_instructions(root, "proj", "worker")
        assert result == "# Custom\nUse pytest."

    @pytest.mark.asyncio
    async def test_save_evaluator_instructions(self, tmp_path: Path) -> None:
        from yukar.storage.agent_config_repo import (
            get_agent_instructions,
            save_agent_instructions,
        )

        root = str(tmp_path)
        await save_agent_instructions(root, "proj", "evaluator", "Be strict.")
        assert get_agent_instructions(root, "proj", "evaluator") == "Be strict."

    def test_get_invalid_role_raises(self, tmp_path: Path) -> None:
        from yukar.config.paths import PathSegmentError
        from yukar.storage.agent_config_repo import get_agent_instructions

        with pytest.raises(PathSegmentError):
            get_agent_instructions(str(tmp_path), "proj", "root")


# ---------------------------------------------------------------------------
# 3. storage/skills_repo
# ---------------------------------------------------------------------------


class TestSkillsRepo:
    def test_list_skills_empty(self, tmp_path: Path) -> None:
        from yukar.storage.skills_repo import list_skills

        assert list_skills(str(tmp_path), "proj") == []

    @pytest.mark.asyncio
    async def test_save_and_list(self, tmp_path: Path) -> None:
        from yukar.storage.skills_repo import list_skills, save_skill

        root = str(tmp_path)
        content = "---\nname: test-skill\ndescription: A test\n---\n# Body"
        await save_skill(root, "proj", "test-skill", content)
        skills = list_skills(root, "proj")
        assert len(skills) == 1
        assert skills[0].name == "test-skill"
        assert skills[0].description == "A test"

    @pytest.mark.asyncio
    async def test_get_skill(self, tmp_path: Path) -> None:
        from yukar.storage.skills_repo import get_skill, save_skill

        root = str(tmp_path)
        content = "---\nname: my-skill\ndescription: Desc\n---\nContent here"
        await save_skill(root, "proj", "my-skill", content)
        skill = get_skill(root, "proj", "my-skill")
        assert skill.name == "my-skill"
        assert skill.description == "Desc"
        assert "Content here" in skill.content

    def test_get_skill_not_found(self, tmp_path: Path) -> None:
        from yukar.storage.skills_repo import get_skill

        with pytest.raises(FileNotFoundError):
            get_skill(str(tmp_path), "proj", "nonexistent")

    @pytest.mark.asyncio
    async def test_delete_skill(self, tmp_path: Path) -> None:
        from yukar.storage.skills_repo import delete_skill, list_skills, save_skill

        root = str(tmp_path)
        await save_skill(root, "proj", "del-skill", "# Del")
        assert len(list_skills(root, "proj")) == 1
        deleted = delete_skill(root, "proj", "del-skill")
        assert deleted is True
        assert list_skills(root, "proj") == []

    def test_delete_skill_not_found(self, tmp_path: Path) -> None:
        from yukar.storage.skills_repo import delete_skill

        deleted = delete_skill(str(tmp_path), "proj", "ghost")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_skill_without_frontmatter(self, tmp_path: Path) -> None:
        from yukar.storage.skills_repo import get_skill, save_skill

        root = str(tmp_path)
        await save_skill(root, "proj", "plain", "# Plain skill content")
        skill = get_skill(root, "proj", "plain")
        assert skill.name == "plain"  # falls back to directory name
        assert skill.description == ""


# ---------------------------------------------------------------------------
# 4. storage/mcp_repo
# ---------------------------------------------------------------------------


class TestMcpRepo:
    def test_get_returns_empty_when_missing(self, tmp_path: Path) -> None:
        from yukar.storage.mcp_repo import get_mcp_config

        cfg = get_mcp_config(str(tmp_path), "proj")
        assert cfg.servers == []

    @pytest.mark.asyncio
    async def test_save_and_get_roundtrip(self, tmp_path: Path) -> None:
        from yukar.models.mcp import McpConfig, McpServerConfig
        from yukar.storage.mcp_repo import get_mcp_config, save_mcp_config

        root = str(tmp_path)
        cfg = McpConfig(
            servers=[
                McpServerConfig(name="my-server", type="stdio", command="npx", args=["my-mcp"])
            ]
        )
        await save_mcp_config(root, "proj", cfg)
        loaded = get_mcp_config(root, "proj")
        assert len(loaded.servers) == 1
        assert loaded.servers[0].name == "my-server"
        assert loaded.servers[0].command == "npx"

    @pytest.mark.asyncio
    async def test_env_values_saved_raw(self, tmp_path: Path) -> None:
        """${VAR} should be stored as-is, not expanded."""
        from yukar.models.mcp import McpConfig, McpServerConfig
        from yukar.storage.mcp_repo import get_mcp_config, save_mcp_config

        root = str(tmp_path)
        cfg = McpConfig(
            servers=[
                McpServerConfig(
                    name="s",
                    type="sse",
                    url="http://localhost:8080",
                    env={"TOKEN": "${MY_TOKEN}"},
                )
            ]
        )
        await save_mcp_config(root, "proj", cfg)
        loaded = get_mcp_config(root, "proj")
        assert loaded.servers[0].env["TOKEN"] == "${MY_TOKEN}"


# ---------------------------------------------------------------------------
# 5. API routes: /agent-configs, /skills, /mcp
# ---------------------------------------------------------------------------


class TestProjectSettingsAPI:
    @pytest.mark.asyncio
    async def test_agent_configs_404_for_missing_project(self, app_client: Any) -> None:
        resp = await app_client.get("/api/projects/noexist/agent-configs")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_agent_configs_get_all_empty(self, app_client: Any, tmp_workspace: Path) -> None:
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p"))
        resp = await app_client.get("/api/projects/p/agent-configs")
        assert resp.status_code == 200
        data = resp.json()
        for role in ("manager", "worker", "evaluator"):
            assert data[role] == ""

    @pytest.mark.asyncio
    async def test_agent_config_put_and_get(self, app_client: Any, tmp_workspace: Path) -> None:
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p"))

        resp = await app_client.put(
            "/api/projects/p/agent-configs/worker",
            json={"instructions": "Use ruff."},
        )
        assert resp.status_code == 200
        assert resp.json()["instructions"] == "Use ruff."

        resp2 = await app_client.get("/api/projects/p/agent-configs/worker")
        assert resp2.status_code == 200
        assert resp2.json()["instructions"] == "Use ruff."

    @pytest.mark.asyncio
    async def test_agent_config_invalid_role_422(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p"))

        resp = await app_client.get("/api/projects/p/agent-configs/admin")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_skills_list_empty(self, app_client: Any, tmp_workspace: Path) -> None:
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p"))
        resp = await app_client.get("/api/projects/p/skills")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_skill_put_get_delete(self, app_client: Any, tmp_workspace: Path) -> None:
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p"))

        content = "---\nname: demo\ndescription: Demo skill\n---\n# Demo"
        resp = await app_client.put("/api/projects/p/skills/demo", json={"content": content})
        assert resp.status_code == 200
        assert resp.json()["name"] == "demo"

        resp2 = await app_client.get("/api/projects/p/skills")
        assert resp2.status_code == 200
        assert len(resp2.json()) == 1

        resp3 = await app_client.get("/api/projects/p/skills/demo")
        assert resp3.status_code == 200
        assert "Demo" in resp3.json()["content"]

        resp4 = await app_client.delete("/api/projects/p/skills/demo")
        assert resp4.status_code == 204

        resp5 = await app_client.get("/api/projects/p/skills")
        assert resp5.json() == []

    @pytest.mark.asyncio
    async def test_skill_not_found_404(self, app_client: Any, tmp_workspace: Path) -> None:
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p"))
        resp = await app_client.get("/api/projects/p/skills/ghost")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_mcp_get_empty(self, app_client: Any, tmp_workspace: Path) -> None:
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p"))
        resp = await app_client.get("/api/projects/p/mcp")
        assert resp.status_code == 200
        assert resp.json()["servers"] == []

    @pytest.mark.asyncio
    async def test_mcp_put_and_get(self, app_client: Any, tmp_workspace: Path) -> None:
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        await save_project(root, Project(id="p", name="p"))

        body = {
            "servers": [
                {
                    "name": "my-mcp",
                    "type": "stdio",
                    "command": "npx",
                    "args": ["@my/mcp"],
                    "env": {},
                }
            ]
        }
        resp = await app_client.put("/api/projects/p/mcp", json=body)
        assert resp.status_code == 200

        resp2 = await app_client.get("/api/projects/p/mcp")
        assert resp2.status_code == 200
        assert resp2.json()["servers"][0]["name"] == "my-mcp"

    @pytest.mark.asyncio
    async def test_mcp_404_for_missing_project(self, app_client: Any) -> None:
        resp = await app_client.get("/api/projects/noexist/mcp")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 6. McpClientManager — start/stop/get_tools with mocked MCPClient
# ---------------------------------------------------------------------------


class TestMcpClientManager:
    def _make_mock_client(self, tools: list[str]) -> MagicMock:
        """Return a mock MCPClient that yields tool stubs."""
        client = MagicMock()
        client.list_tools_sync.return_value = tools
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        return client

    @pytest.mark.asyncio
    async def test_start_with_no_configs(self) -> None:
        from yukar.agents.mcp_manager import McpClientManager

        mgr = McpClientManager([])
        await mgr.start_async()
        tools = await mgr.get_tools_async()
        assert tools == []
        await mgr.stop_async()

    @pytest.mark.asyncio
    async def test_start_stop_get_tools_stdio(self) -> None:
        import yukar.agents.mcp_manager as mm
        from yukar.agents.mcp_manager import McpClientManager
        from yukar.models.mcp import McpServerConfig

        cfg = McpServerConfig(name="test", type="stdio", command="echo", args=["hi"])
        mgr = McpClientManager([cfg])

        mock_client = self._make_mock_client(["tool_a", "tool_b"])

        with (
            patch.object(mm, "StdioServerParameters", MagicMock()),
            patch.object(mm, "stdio_client", MagicMock()),
            patch.object(mm, "MCPClient", return_value=mock_client),
            patch.object(mm, "_MCP_AVAILABLE", True),
        ):
            await mgr.start_async()
            tools = await mgr.get_tools_async()
            assert tools == ["tool_a", "tool_b"]
            await mgr.stop_async()
            mock_client.__exit__.assert_called_once()

    @pytest.mark.asyncio
    async def test_server_failure_skipped(self) -> None:
        """A failing server should not prevent the manager from starting."""
        import yukar.agents.mcp_manager as mm
        from yukar.agents.mcp_manager import McpClientManager
        from yukar.models.mcp import McpServerConfig

        cfg = McpServerConfig(name="bad", type="stdio", command="bad-cmd")
        mgr = McpClientManager([cfg])

        bad_client = MagicMock()
        bad_client.__enter__ = MagicMock(side_effect=RuntimeError("connection failed"))

        with (
            patch.object(mm, "StdioServerParameters", MagicMock()),
            patch.object(mm, "stdio_client", MagicMock()),
            patch.object(mm, "MCPClient", return_value=bad_client),
            patch.object(mm, "_MCP_AVAILABLE", True),
        ):
            await mgr.start_async()
            # Manager started despite failure; no clients connected.
            tools = await mgr.get_tools_async()
            assert tools == []
            await mgr.stop_async()

    @pytest.mark.asyncio
    async def test_sse_missing_url_skipped(self) -> None:
        """SSE server without url should be skipped, not crash."""
        from yukar.agents.mcp_manager import McpClientManager
        from yukar.models.mcp import McpServerConfig

        cfg = McpServerConfig(name="sse-no-url", type="sse", url=None)
        mgr = McpClientManager([cfg])
        await mgr.start_async()
        tools = await mgr.get_tools_async()
        assert tools == []
        await mgr.stop_async()

    @pytest.mark.asyncio
    async def test_env_var_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from yukar.agents.mcp_manager import _expand_env_vars

        monkeypatch.setenv("MY_TOKEN", "secret123")
        result = _expand_env_vars("Bearer ${MY_TOKEN}")
        assert result == "Bearer secret123"

    def test_env_var_expansion_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from yukar.agents.mcp_manager import _expand_env_vars

        monkeypatch.delenv("MISSING_VAR", raising=False)
        result = _expand_env_vars("${MISSING_VAR}")
        assert result == ""

    def test_tool_filters_none_when_empty(self) -> None:
        from yukar.agents.mcp_manager import McpClientManager
        from yukar.models.mcp import McpServerConfig

        cfg = McpServerConfig(name="s", type="stdio", command="x")
        assert McpClientManager._build_tool_filters(cfg) is None

    def test_tool_filters_allowed(self) -> None:
        from yukar.agents.mcp_manager import McpClientManager
        from yukar.models.mcp import McpServerConfig

        cfg = McpServerConfig(name="s", type="stdio", command="x", allowed_tools=["t1", "t2"])
        filters = McpClientManager._build_tool_filters(cfg)
        assert filters is not None
        assert filters["allowed"] == ["t1", "t2"]

    def test_tool_filters_rejected(self) -> None:
        from yukar.agents.mcp_manager import McpClientManager
        from yukar.models.mcp import McpServerConfig

        cfg = McpServerConfig(name="s", type="stdio", command="x", rejected_tools=["bad"])
        filters = McpClientManager._build_tool_filters(cfg)
        assert filters is not None
        assert filters["rejected"] == ["bad"]


# ---------------------------------------------------------------------------
# 7. project_extras: build_skills_plugin, overlay_system_prompt
# ---------------------------------------------------------------------------


class TestProjectExtras:
    @pytest.mark.asyncio
    async def test_build_skills_plugin_no_skills_dir(self, tmp_path: Path) -> None:
        from yukar.agents.project_extras import build_skills_plugin

        # No skills dir → None
        plugin = build_skills_plugin(str(tmp_path), "proj")
        assert plugin is None

    @pytest.mark.asyncio
    async def test_build_skills_plugin_empty_dir(self, tmp_path: Path) -> None:
        from yukar.agents.project_extras import build_skills_plugin
        from yukar.config.paths import project_skills_dir

        skills_dir = project_skills_dir(str(tmp_path), "proj")
        skills_dir.mkdir(parents=True)
        plugin = build_skills_plugin(str(tmp_path), "proj")
        assert plugin is None

    @pytest.mark.asyncio
    async def test_build_skills_plugin_with_skill(self, tmp_path: Path) -> None:
        from yukar.agents.project_extras import build_skills_plugin
        from yukar.config.paths import skill_md_path

        md_path = skill_md_path(str(tmp_path), "proj", "my-skill")
        md_path.parent.mkdir(parents=True)
        md_path.write_text("# Skill")

        plugin = build_skills_plugin(str(tmp_path), "proj")
        assert plugin is not None

    def test_overlay_no_instructions(self, tmp_path: Path) -> None:
        from yukar.agents.project_extras import overlay_system_prompt

        result = overlay_system_prompt("base prompt", str(tmp_path), "proj", "worker")
        assert result == "base prompt"

    @pytest.mark.asyncio
    async def test_overlay_with_instructions(self, tmp_path: Path) -> None:
        from yukar.agents.project_extras import overlay_system_prompt
        from yukar.storage.agent_config_repo import save_agent_instructions

        root = str(tmp_path)
        await save_agent_instructions(root, "proj", "worker", "Use TypeScript.")
        result = overlay_system_prompt("Base.", root, "proj", "worker")
        assert "Base." in result
        assert "Use TypeScript." in result
        assert "worker" in result


# ---------------------------------------------------------------------------
# 8. Manager tools: write_agent_config / read_agent_config
# ---------------------------------------------------------------------------


class TestAgentConfigTools:
    @pytest.mark.asyncio
    async def test_write_and_read_agent_config(self, tmp_path: Path) -> None:
        from yukar.agents.tools.agent_config_tools import make_agent_config_tools

        root = str(tmp_path)
        tools = make_agent_config_tools(root, "proj")
        write_tool, read_tool = tools

        # Call the inner function (Strands wraps with @tool decorator).
        write_fn = write_tool.func if hasattr(write_tool, "func") else write_tool.__wrapped__
        read_fn = read_tool.func if hasattr(read_tool, "func") else read_tool.__wrapped__

        result = await write_fn(role="worker", instructions="Always add type hints.")
        assert result["ok"] is True

        read_result = read_fn(role="worker")
        assert read_result["instructions"] == "Always add type hints."

    @pytest.mark.asyncio
    async def test_write_invalid_role_returns_error(self, tmp_path: Path) -> None:
        from yukar.agents.tools.agent_config_tools import make_agent_config_tools

        root = str(tmp_path)
        tools = make_agent_config_tools(root, "proj")
        write_tool = tools[0]
        write_fn = write_tool.func if hasattr(write_tool, "func") else write_tool.__wrapped__

        result = await write_fn(role="admin", instructions="evil")
        assert result["ok"] is False
        assert "error" in result

    def test_read_missing_returns_empty(self, tmp_path: Path) -> None:
        from yukar.agents.tools.agent_config_tools import make_agent_config_tools

        tools = make_agent_config_tools(str(tmp_path), "proj")
        read_tool = tools[1]
        read_fn = read_tool.func if hasattr(read_tool, "func") else read_tool.__wrapped__

        result = read_fn(role="evaluator")
        assert result["instructions"] == ""
