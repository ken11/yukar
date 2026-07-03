"""Wave 5 BE-B tests — profile resolution at dispatch time.

Covers:
1. instructions: profile overlay stacks on top of project-role overlay
2. skills: profile.skills non-empty → AgentSkills receives only those skill dirs
3. MCP: profile.mcp_servers non-empty → only matching server tools forwarded
4. run_command: effective allow = repo ∩ profile.allowed_commands; deny = repo deny
   (a profile has no deny list of its own)
5. Unassigned task (task.agent=None) → no change (backward compat)
6. Missing profile → warning logged, fallback to defaults
7. base_role mismatch (worker profile on evaluator call) → warning, fallback
8. build_skills_plugin with names= subset
9. McpClientManager.get_tools_by_server_async
10. base_role mismatch → commands also revert to repo defaults (4-dimension consistency)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1. instructions — overlay stacking
# ---------------------------------------------------------------------------


class TestProfileInstructionsOverlay:
    def test_overlay_profile_instructions_appends(self) -> None:
        from yukar.agents.project_extras import overlay_profile_instructions

        result = overlay_profile_instructions("Base prompt.", "Use pytest.")
        assert result.startswith("Base prompt.")
        assert "Profile-specific instructions" in result
        assert "Use pytest." in result

    def test_overlay_profile_instructions_empty_returns_base(self) -> None:
        from yukar.agents.project_extras import overlay_profile_instructions

        base = "Base prompt."
        assert overlay_profile_instructions(base, "") == base

    def test_stacking_order(self) -> None:
        """project-role overlay comes before profile overlay."""
        from yukar.agents.project_extras import overlay_profile_instructions, overlay_system_prompt

        base = "BASE"
        project_overlay = overlay_system_prompt(base, "/fake", "proj", "worker")
        # No custom instructions exist → base unchanged
        assert project_overlay == base

        profile_overlay = overlay_profile_instructions(project_overlay, "Profile instructions.")
        # Profile section comes after base (and would come after project overlay if present)
        assert profile_overlay.index("BASE") < profile_overlay.index("Profile instructions.")


# ---------------------------------------------------------------------------
# 2. skills — build_skills_plugin with names=
# ---------------------------------------------------------------------------


class TestBuildSkillsPluginSubset:
    def _make_skills(self, tmp_path: Path, names: list[str]) -> Path:
        """Create minimal skill directories under tmp_path/skills/."""
        skills_dir = tmp_path / "skills"
        for name in names:
            skill_dir = skills_dir / name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(f"# {name}\nA test skill.")
        return skills_dir

    def test_no_names_returns_full_plugin(self, tmp_path: Path) -> None:
        from yukar.agents.project_extras import build_skills_plugin
        from yukar.config.paths import project_skills_dir

        # Create two skills.
        root = str(tmp_path / "ws")
        pid = "proj"
        sd = project_skills_dir(root, pid)
        for name in ("skill-a", "skill-b"):
            (sd / name).mkdir(parents=True)
            (sd / name / "SKILL.md").write_text(f"# {name}")

        plugin = build_skills_plugin(root, pid)
        assert plugin is not None

    def test_names_empty_list_returns_full_plugin(self, tmp_path: Path) -> None:
        from yukar.agents.project_extras import build_skills_plugin
        from yukar.config.paths import project_skills_dir

        root = str(tmp_path / "ws")
        pid = "proj"
        sd = project_skills_dir(root, pid)
        (sd / "skill-a").mkdir(parents=True)
        (sd / "skill-a" / "SKILL.md").write_text("# A")

        # Empty list = no filter (same as None)
        plugin = build_skills_plugin(root, pid, names=[])
        assert plugin is not None

    def test_names_subset_passes_individual_dirs(self, tmp_path: Path) -> None:
        """When names is non-empty, only the matching skill dirs are passed to AgentSkills."""
        from yukar.config.paths import project_skills_dir

        root = str(tmp_path / "ws")
        pid = "proj"
        sd = project_skills_dir(root, pid)
        for name in ("skill-a", "skill-b", "skill-c"):
            (sd / name).mkdir(parents=True)
            (sd / name / "SKILL.md").write_text(f"# {name}")

        captured_paths: list[Any] = []

        class FakeAgentSkills:
            def __init__(self, skills: list[Any]) -> None:
                captured_paths.extend(skills)

        # AgentSkills is imported inside the function via `from strands import AgentSkills`,
        # so we patch it on the strands module (which is the import source).
        with patch("strands.AgentSkills", FakeAgentSkills):
            from yukar.agents.project_extras import build_skills_plugin

            build_skills_plugin(root, pid, names=["skill-a", "skill-c"])

        # Should have created a FakeAgentSkills with exactly 2 paths
        assert len(captured_paths) == 2
        path_strs = [str(p) for p in captured_paths]
        assert any("skill-a" in p for p in path_strs)
        assert any("skill-c" in p for p in path_strs)
        assert not any("skill-b" in p for p in path_strs)

    def test_names_missing_skill_returns_none_when_all_missing(self, tmp_path: Path) -> None:
        from yukar.agents.project_extras import build_skills_plugin
        from yukar.config.paths import project_skills_dir

        root = str(tmp_path / "ws")
        pid = "proj"
        sd = project_skills_dir(root, pid)
        sd.mkdir(parents=True)

        # No skills exist — requesting a subset should return None
        result = build_skills_plugin(root, pid, names=["nonexistent"])
        assert result is None

    def test_names_partial_missing_skips_missing(self, tmp_path: Path) -> None:
        """If some requested skills are missing, existing ones are used."""
        from yukar.config.paths import project_skills_dir

        root = str(tmp_path / "ws")
        pid = "proj"
        sd = project_skills_dir(root, pid)
        (sd / "skill-a").mkdir(parents=True)
        (sd / "skill-a" / "SKILL.md").write_text("# A")

        captured_paths: list[Any] = []

        class FakeAgentSkills:
            def __init__(self, skills: list[Any]) -> None:
                captured_paths.extend(skills)

        with patch("strands.AgentSkills", FakeAgentSkills):
            from yukar.agents.project_extras import build_skills_plugin

            build_skills_plugin(root, pid, names=["skill-a", "ghost-skill"])

        assert len(captured_paths) == 1
        assert "skill-a" in str(captured_paths[0])


# ---------------------------------------------------------------------------
# 3. MCP — McpClientManager.get_tools_by_server and subset selection
# ---------------------------------------------------------------------------


class TestMcpToolsByServer:
    def test_get_tools_by_server_sync(self) -> None:
        """_get_tools_by_server returns dict keyed by server name."""
        from yukar.agents.mcp_manager import McpClientManager
        from yukar.models.mcp import McpServerConfig

        cfg_a = McpServerConfig(name="server-a", type="stdio", command="echo")
        cfg_b = McpServerConfig(name="server-b", type="stdio", command="echo")
        mgr = McpClientManager([cfg_a, cfg_b])

        # Simulate two connected clients.
        fake_tool_a = MagicMock(name="tool-a")
        fake_tool_b = MagicMock(name="tool-b")
        client_a = MagicMock()
        client_a.list_tools_sync.return_value = [fake_tool_a]
        client_b = MagicMock()
        client_b.list_tools_sync.return_value = [fake_tool_b]
        mgr._clients = [client_a, client_b]
        mgr._client_names = ["server-a", "server-b"]
        mgr._started = True

        result = mgr._get_tools_by_server()
        assert set(result.keys()) == {"server-a", "server-b"}
        assert result["server-a"] == [fake_tool_a]
        assert result["server-b"] == [fake_tool_b]

    @pytest.mark.asyncio
    async def test_get_tools_by_server_async(self) -> None:
        """get_tools_by_server_async delegates to _get_tools_by_server via to_thread."""
        from yukar.agents.mcp_manager import McpClientManager
        from yukar.models.mcp import McpServerConfig

        mgr = McpClientManager([McpServerConfig(name="s1", type="stdio", command="echo")])
        expected = {"s1": [MagicMock()]}
        mgr._get_tools_by_server = MagicMock(return_value=expected)  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]

        with patch("asyncio.to_thread", new=AsyncMock(return_value=expected)):
            result = await mgr.get_tools_by_server_async()

        assert result == expected

    def test_server_failure_omitted_from_result(self) -> None:
        """A server that fails list_tools_sync is omitted, not raising."""
        from yukar.agents.mcp_manager import McpClientManager
        from yukar.models.mcp import McpServerConfig

        mgr = McpClientManager([McpServerConfig(name="bad", type="stdio", command="echo")])
        bad_client = MagicMock()
        bad_client.list_tools_sync.side_effect = RuntimeError("connection refused")
        mgr._clients = [bad_client]
        mgr._client_names = ["bad"]
        mgr._started = True

        result = mgr._get_tools_by_server()
        assert result == {}

    def test_stop_clears_client_names(self) -> None:
        """_stop() clears _client_names alongside _clients."""
        from yukar.agents.mcp_manager import McpClientManager
        from yukar.models.mcp import McpServerConfig

        mgr = McpClientManager([McpServerConfig(name="s", type="stdio", command="echo")])
        mgr._clients = [MagicMock()]
        mgr._client_names = ["s"]
        mgr._started = True
        mgr._stop()
        assert mgr._client_names == []
        assert mgr._clients == []

    def test_resolve_mcp_tools_for_profile_filters(self) -> None:
        """Orchestrator._resolve_mcp_tools_for_profile returns only requested servers' tools."""
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(),
            git_author_name="t",
            git_author_email="t@t",
        )
        tool_a = MagicMock(name="tool-a")
        tool_b = MagicMock(name="tool-b")
        orch._mcp_tools_by_server = {"server-a": [tool_a], "server-b": [tool_b]}

        result = orch._resolve_mcp_tools_for_profile(["server-a"])
        assert result == [tool_a]
        assert tool_b not in result

    def test_resolve_mcp_tools_for_profile_missing_server_warns(self) -> None:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(),
            git_author_name="t",
            git_author_email="t@t",
        )
        orch._mcp_tools_by_server = {}

        with patch("yukar.agents.orchestrator.logger") as mock_log:
            result = orch._resolve_mcp_tools_for_profile(["nonexistent"])

        assert result == []
        mock_log.warning.assert_called_once()


# ---------------------------------------------------------------------------
# 4. run_command — profile × repo merging
# ---------------------------------------------------------------------------


class TestMergeCommands:
    """Tests for _merge_commands (new signature: resolved_profile, repo_allow, repo_deny).

    Profile resolution (base_role validation) is the responsibility of
    _resolve_profile; _merge_commands now receives the already-validated profile
    (or None).  These tests verify the merging logic in isolation.
    """

    def test_no_profile_returns_repo_values(self) -> None:
        from yukar.agents.dispatch_attempt import _merge_commands

        allow, deny = _merge_commands(None, ["pytest", "npm"], ["rm"])
        assert allow == ["pytest", "npm"]
        assert deny == ["rm"]

    def test_profile_allow_intersects_with_repo(self) -> None:
        """effective_allow = repo_allow ∩ profile.allowed_commands when non-empty."""
        from yukar.agents.dispatch_attempt import _merge_commands
        from yukar.models.agent_profile import AgentProfile

        # profile allows ["pytest"] — a subset of repo's ["pytest", "npm", "git"]
        profile = AgentProfile(
            name="be-worker",
            base_role="worker",
            allowed_commands=["pytest"],
        )
        allow, deny = _merge_commands(profile, ["pytest", "npm", "git"], [])
        # Intersection: only "pytest" is in both repo and profile
        assert allow == ["pytest"]
        assert deny == []

    def test_profile_allow_empty_uses_repo_allow(self) -> None:
        """When profile.allowed_commands is empty, repo.allow is used unchanged."""
        from yukar.agents.dispatch_attempt import _merge_commands
        from yukar.models.agent_profile import AgentProfile

        profile = AgentProfile(
            name="no-filter",
            base_role="worker",
            allowed_commands=[],
        )
        allow, deny = _merge_commands(profile, ["pytest", "git"], ["rm"])
        assert allow == ["pytest", "git"]
        assert deny == ["rm"]

    def test_profile_has_no_deny_of_its_own(self) -> None:
        """A profile has no deny list — effective_deny is always exactly repo_deny."""
        from yukar.agents.dispatch_attempt import _merge_commands
        from yukar.models.agent_profile import AgentProfile

        profile = AgentProfile(
            name="strict-worker",
            base_role="worker",
            allowed_commands=["pytest"],
        )
        allow, deny = _merge_commands(profile, ["pytest", "npm"], ["rm"])
        # Profile narrows allow to the intersection…
        assert allow == ["pytest"]
        # …but cannot add to (or change) the repo deny list.
        assert deny == ["rm"]

    def test_profile_cannot_grant_beyond_repo(self) -> None:
        """Profile allow that is not in repo.allow produces empty effective_allow."""
        from yukar.agents.dispatch_attempt import _merge_commands
        from yukar.models.agent_profile import AgentProfile

        # Profile tries to grant "bash" — repo does not allow it.
        profile = AgentProfile(
            name="escalate",
            base_role="worker",
            allowed_commands=["bash", "sh"],
        )
        allow, deny = _merge_commands(profile, ["pytest"], [])
        # "bash" and "sh" are not in repo.allow → intersection is empty
        assert allow == []

    def test_missing_profile_falls_back_to_repo(self) -> None:
        """When resolved_profile is None, repo values are used unchanged."""
        from yukar.agents.dispatch_attempt import _merge_commands

        allow, deny = _merge_commands(None, ["pytest"], ["rm"])
        assert allow == ["pytest"]
        assert deny == ["rm"]

    def test_repo_deny_is_preserved_regardless_of_profile(self) -> None:
        """The repo deny list passes through unchanged; a profile cannot alter it."""
        from yukar.agents.dispatch_attempt import _merge_commands
        from yukar.models.agent_profile import AgentProfile

        profile = AgentProfile(
            name="narrowed",
            base_role="worker",
            allowed_commands=["pytest"],
        )
        _, deny = _merge_commands(profile, ["pytest"], ["rm", "curl"])
        assert deny == ["rm", "curl"]


class TestResolveProfile:
    """Tests for _resolve_profile — the single profile resolution point."""

    def _make_task(self, agent: str | None = None) -> Any:
        from yukar.models.task import Task

        return Task(id="T1", title="test", agent=agent)

    def test_no_agent_returns_none(self, tmp_path: Path) -> None:
        from yukar.agents.dispatch_attempt import _resolve_profile

        task = self._make_task(agent=None)
        assert _resolve_profile(str(tmp_path), "proj", task, "worker") is None

    def test_missing_profile_returns_none_with_warning(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from yukar.agents.dispatch_attempt import _resolve_profile

        task = self._make_task(agent="ghost")
        with patch("yukar.agents.dispatch_attempt.logger") as mock_log:
            result = _resolve_profile(str(tmp_path), "proj", task, "worker")
        assert result is None
        mock_log.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_matching_base_role_returns_profile(self, tmp_path: Path) -> None:
        from yukar.agents.dispatch_attempt import _resolve_profile
        from yukar.models.agent_profile import AgentProfile
        from yukar.storage.agent_profiles_repo import save_profile

        await save_profile(
            str(tmp_path),
            "proj",
            AgentProfile(name="be-worker", base_role="worker"),
        )
        task = self._make_task(agent="be-worker")
        result = _resolve_profile(str(tmp_path), "proj", task, "worker")
        assert result is not None
        assert result.name == "be-worker"

    @pytest.mark.asyncio
    async def test_wrong_base_role_returns_none_with_warning(self, tmp_path: Path) -> None:
        """base_role mismatch → None, warning logged."""
        from unittest.mock import patch

        from yukar.agents.dispatch_attempt import _resolve_profile
        from yukar.models.agent_profile import AgentProfile
        from yukar.storage.agent_profiles_repo import save_profile

        await save_profile(
            str(tmp_path),
            "proj",
            AgentProfile(
                name="eval-profile",
                base_role="evaluator",
                instructions="Evaluator only.",
            ),
        )
        task = self._make_task(agent="eval-profile")
        with patch("yukar.agents.dispatch_attempt.logger") as mock_log:
            result = _resolve_profile(str(tmp_path), "proj", task, "worker")
        assert result is None
        mock_log.warning.assert_called_once()


class TestMergeCommandsBaseRoleConsistency:
    """Verifies that base_role mismatch also reverts commands to repo defaults.

    This is the key 4-dimension consistency invariant: when _resolve_profile
    returns None (due to base_role mismatch or missing profile), _merge_commands
    must also return the repo allow/deny unchanged — i.e., commands are NOT
    applied from a mismatched profile.

    Previously _merge_commands called get_profile independently (without
    base_role checking), so a mismatched profile could still affect commands
    while instructions/skills/MCP were correctly ignored.
    """

    def _make_task(self, agent: str | None = None) -> Any:
        from yukar.models.task import Task

        return Task(id="T1", title="test", agent=agent)

    @pytest.mark.asyncio
    async def test_wrong_base_role_commands_revert_to_repo(self, tmp_path: Path) -> None:
        """base_role mismatch → _resolve_profile returns None → _merge_commands returns repo values.

        This tests the full pipeline: _resolve_profile + _merge_commands together
        so that the 4-dimension consistency is verified end-to-end.
        """
        from yukar.agents.dispatch_attempt import _merge_commands, _resolve_profile
        from yukar.models.agent_profile import AgentProfile
        from yukar.storage.agent_profiles_repo import save_profile

        root = str(tmp_path)
        # Evaluator profile with restrictive commands — assigned to a worker task.
        await save_profile(
            root,
            "proj",
            AgentProfile(
                name="eval-profile",
                base_role="evaluator",
                instructions="Evaluator only.",
                allowed_commands=["pytest"],
            ),
        )
        task = self._make_task(agent="eval-profile")
        repo_allow = ["pytest", "npm", "git"]
        repo_deny = ["rm"]

        # Resolution with expected_role="worker" → mismatch → None
        resolved = _resolve_profile(root, "proj", task, expected_role="worker")
        assert resolved is None, "base_role mismatch must yield None"

        # _merge_commands with None profile must return repo values unchanged.
        allow, deny = _merge_commands(resolved, repo_allow, repo_deny)
        assert allow == repo_allow, "commands must revert to repo allow on mismatch"
        assert deny == repo_deny, "commands must revert to repo deny on mismatch"

    @pytest.mark.asyncio
    async def test_wrong_base_role_profile_deny_not_applied(self, tmp_path: Path) -> None:
        """Evaluator profile deny must not extend repo deny when assigned to worker task."""
        from yukar.agents.dispatch_attempt import _merge_commands, _resolve_profile
        from yukar.models.agent_profile import AgentProfile
        from yukar.storage.agent_profiles_repo import save_profile

        root = str(tmp_path)
        await save_profile(
            root,
            "proj",
            AgentProfile(
                name="eval-deny-heavy",
                base_role="evaluator",
                allowed_commands=[],
            ),
        )
        task = self._make_task(agent="eval-deny-heavy")
        repo_allow: list[str] = []
        repo_deny = ["rm"]

        resolved = _resolve_profile(root, "proj", task, expected_role="worker")
        allow, deny = _merge_commands(resolved, repo_allow, repo_deny)

        # None of the evaluator-profile denies should appear.
        assert "curl" not in deny
        assert "wget" not in deny
        assert "ssh" not in deny
        assert deny == repo_deny


# ---------------------------------------------------------------------------
# 5 & 6 & 7. Orchestrator _run_worker/_run_evaluator profile resolution
# ---------------------------------------------------------------------------


class TestOrchestratorProfileResolution:
    """Unit tests for _run_worker and _run_evaluator profile dispatch.

    Since profile resolution was moved to dispatch_attempt._resolve_profile,
    orchestrator methods now receive a pre-resolved ``resolved_profile`` arg.
    Tests pass the profile (or None) directly to verify that each dimension
    (instructions / skills / MCP) is applied correctly.

    The warning-on-mismatch / warning-on-missing behaviour is now tested via
    TestResolveProfile (the single resolution point).
    """

    def _make_orchestrator(self) -> Any:
        from yukar.agents.orchestrator import EpicOrchestrator
        from yukar.config.settings import LLMSettings

        orch = EpicOrchestrator(
            llm_settings=LLMSettings(),
            git_author_name="test",
            git_author_email="test@test",
        )
        orch._root = "/fake/root"
        orch._project_id = "proj"
        orch._epic_id = "ep-1"
        orch._run_id = "run-1"
        orch._mcp_tools = []
        orch._mcp_tools_by_server = {}
        orch._indexer_service = None
        return orch

    @pytest.mark.asyncio
    async def test_worker_no_profile_uses_defaults(self, tmp_path: Path) -> None:
        """resolved_profile=None → default build_skills_plugin (no filter)."""
        from yukar.models.task import Task

        orch = self._make_orchestrator()
        task = Task(id="T1", title="test", agent=None)
        ctx = MagicMock()
        ctx.worktree_path = tmp_path
        ctx.workspace_root = str(tmp_path)
        ctx.project_id = "proj"
        ctx.repo_name = "repo"

        called_args: dict[str, Any] = {}

        async def fake_run_worker(**kwargs: Any) -> dict[str, Any]:
            called_args.update(kwargs)
            return {"result": "ok"}

        with (
            patch("yukar.agents.orchestrator.run_worker", side_effect=fake_run_worker),
            patch("yukar.agents.orchestrator.create_model", return_value=MagicMock()),
            patch("yukar.agents.orchestrator.create_conversation_manager", return_value=None),
        ):
            await orch._run_worker(
                project_id="proj",
                epic_id="ep-1",
                run_id="run-1",
                worker_id="w-1",
                task=task,
                ctx=ctx,
                feedback="",
                hitl_prefix="",
                resolved_profile=None,
            )

        # extra_tools should be the empty mcp_tools list
        assert called_args["extra_tools"] == []
        # plugins list is empty (no skills dir exists)
        assert called_args["plugins"] == []

    @pytest.mark.asyncio
    async def test_worker_with_profile_applies_instructions(self, tmp_path: Path) -> None:
        """resolved_profile with instructions → extra_system_prompt includes them."""
        from yukar.models.agent_profile import AgentProfile
        from yukar.models.task import Task

        profile = AgentProfile(
            name="be-worker",
            base_role="worker",
            instructions="Always run pytest.",
            skills=[],
            mcp_servers=[],
        )
        orch = self._make_orchestrator()
        task = Task(id="T1", title="test", agent="be-worker")
        ctx = MagicMock()
        ctx.worktree_path = tmp_path
        ctx.workspace_root = str(tmp_path)
        ctx.project_id = "proj"
        ctx.repo_name = "repo"

        captured: dict[str, Any] = {}

        async def fake_run_worker(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"result": "ok"}

        with (
            patch("yukar.agents.orchestrator.run_worker", side_effect=fake_run_worker),
            patch("yukar.agents.orchestrator.create_model", return_value=MagicMock()),
            patch("yukar.agents.orchestrator.create_conversation_manager", return_value=None),
        ):
            await orch._run_worker(
                project_id="proj",
                epic_id="ep-1",
                run_id="run-1",
                worker_id="w-1",
                task=task,
                ctx=ctx,
                feedback="",
                hitl_prefix="",
                resolved_profile=profile,
            )

        assert "Always run pytest." in captured["extra_system_prompt"]

    @pytest.mark.asyncio
    async def test_worker_missing_profile_falls_back(self, tmp_path: Path) -> None:
        """resolved_profile=None (missing / mismatch case) → defaults apply."""
        from yukar.models.task import Task

        orch = self._make_orchestrator()
        task = Task(id="T1", title="test", agent="ghost-profile")
        ctx = MagicMock()
        ctx.worktree_path = tmp_path
        ctx.workspace_root = str(tmp_path)
        ctx.project_id = "proj"
        ctx.repo_name = "repo"

        captured: dict[str, Any] = {}

        async def fake_run_worker(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"result": "ok"}

        with (
            patch("yukar.agents.orchestrator.run_worker", side_effect=fake_run_worker),
            patch("yukar.agents.orchestrator.create_model", return_value=MagicMock()),
            patch("yukar.agents.orchestrator.create_conversation_manager", return_value=None),
        ):
            await orch._run_worker(
                project_id="proj",
                epic_id="ep-1",
                run_id="run-1",
                worker_id="w-1",
                task=task,
                ctx=ctx,
                feedback="",
                hitl_prefix="",
                resolved_profile=None,
            )

        # extra_tools falls back to full mcp_tools (empty in this test)
        assert captured["extra_tools"] == []

    @pytest.mark.asyncio
    async def test_worker_wrong_base_role_ignored(self, tmp_path: Path) -> None:
        """base_role mismatch → caller passes resolved_profile=None → profile is ignored."""
        from yukar.models.task import Task

        # The mismatch detection happens in _resolve_profile (dispatch_attempt).
        # Orchestrator receives None and uses defaults.
        orch = self._make_orchestrator()
        task = Task(id="T1", title="test", agent="eval-profile")
        ctx = MagicMock()
        ctx.worktree_path = tmp_path
        ctx.workspace_root = str(tmp_path)
        ctx.project_id = "proj"
        ctx.repo_name = "repo"

        captured: dict[str, Any] = {}

        async def fake_run_worker(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"result": "ok"}

        with (
            patch("yukar.agents.orchestrator.run_worker", side_effect=fake_run_worker),
            patch("yukar.agents.orchestrator.create_model", return_value=MagicMock()),
            patch("yukar.agents.orchestrator.create_conversation_manager", return_value=None),
        ):
            await orch._run_worker(
                project_id="proj",
                epic_id="ep-1",
                run_id="run-1",
                worker_id="w-1",
                task=task,
                ctx=ctx,
                feedback="",
                hitl_prefix="",
                resolved_profile=None,  # simulates base_role mismatch outcome
            )

        # Profile instructions must NOT appear since resolved_profile is None.
        assert "Evaluator-only instructions." not in captured.get("extra_system_prompt", "")

    @pytest.mark.asyncio
    async def test_evaluator_wrong_base_role_ignored(self, tmp_path: Path) -> None:
        """base_role mismatch for evaluator → resolved_profile=None → profile ignored."""
        from yukar.models.task import Task

        orch = self._make_orchestrator()
        task = Task(id="T1", title="test", agent="worker-profile")
        ctx = MagicMock()
        ctx.worktree_path = tmp_path
        ctx.workspace_root = str(tmp_path)
        ctx.project_id = "proj"
        ctx.repo_name = "repo"

        captured: dict[str, Any] = {}

        async def fake_run_evaluator(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"accepted": True, "feedback": ""}

        with (
            patch("yukar.agents.orchestrator.run_evaluator", side_effect=fake_run_evaluator),
            patch("yukar.agents.orchestrator.create_model", return_value=MagicMock()),
            patch("yukar.agents.orchestrator.create_conversation_manager", return_value=None),
        ):
            orch._epic = MagicMock()
            await orch._run_evaluator(
                project_id="proj",
                epic_id="ep-1",
                run_id="run-1",
                eval_id="e-1",
                task=task,
                ctx=ctx,
                worker_id="w-1",
                resolved_profile=None,  # simulates base_role mismatch outcome
            )

        assert "Worker instructions." not in captured.get("extra_system_prompt", "")

    @pytest.mark.asyncio
    async def test_worker_mcp_subset_applied(self, tmp_path: Path) -> None:
        """Profile.mcp_servers non-empty → only those servers' tools forwarded."""
        from yukar.models.agent_profile import AgentProfile
        from yukar.models.task import Task

        profile = AgentProfile(
            name="partial-mcp",
            base_role="worker",
            mcp_servers=["server-a"],
        )
        orch = self._make_orchestrator()
        tool_a = MagicMock(name="tool-a")
        tool_b = MagicMock(name="tool-b")
        orch._mcp_tools = [tool_a, tool_b]
        orch._mcp_tools_by_server = {"server-a": [tool_a], "server-b": [tool_b]}

        task = Task(id="T1", title="test", agent="partial-mcp")
        ctx = MagicMock()
        ctx.worktree_path = tmp_path
        ctx.workspace_root = str(tmp_path)
        ctx.project_id = "proj"
        ctx.repo_name = "repo"

        captured: dict[str, Any] = {}

        async def fake_run_worker(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"result": "ok"}

        with (
            patch("yukar.agents.orchestrator.run_worker", side_effect=fake_run_worker),
            patch("yukar.agents.orchestrator.create_model", return_value=MagicMock()),
            patch("yukar.agents.orchestrator.create_conversation_manager", return_value=None),
        ):
            await orch._run_worker(
                project_id="proj",
                epic_id="ep-1",
                run_id="run-1",
                worker_id="w-1",
                task=task,
                ctx=ctx,
                feedback="",
                hitl_prefix="",
                resolved_profile=profile,
            )

        # Only server-a tools should be passed
        assert captured["extra_tools"] == [tool_a]
        assert tool_b not in captured["extra_tools"]

    @pytest.mark.asyncio
    async def test_worker_skills_subset_applied(self, tmp_path: Path) -> None:
        """Profile.skills non-empty → build_skills_plugin called with names=profile.skills."""
        from yukar.models.agent_profile import AgentProfile
        from yukar.models.task import Task

        profile = AgentProfile(
            name="skill-worker",
            base_role="worker",
            skills=["skill-a"],
        )
        orch = self._make_orchestrator()
        task = Task(id="T1", title="test", agent="skill-worker")
        ctx = MagicMock()
        ctx.worktree_path = tmp_path
        ctx.workspace_root = str(tmp_path)
        ctx.project_id = "proj"
        ctx.repo_name = "repo"

        build_skills_calls: list[Any] = []

        def fake_build_skills(r: str, p: str, names: list[str] | None = None) -> None:
            build_skills_calls.append({"root": r, "project_id": p, "names": names})
            return None

        async def fake_run_worker(**kwargs: Any) -> dict[str, Any]:
            return {"result": "ok"}

        with (
            patch("yukar.agents.orchestrator.run_worker", side_effect=fake_run_worker),
            patch("yukar.agents.orchestrator.create_model", return_value=MagicMock()),
            patch("yukar.agents.orchestrator.create_conversation_manager", return_value=None),
            patch(
                "yukar.agents.orchestrator.build_skills_plugin",
                side_effect=fake_build_skills,
            ),
        ):
            await orch._run_worker(
                project_id="proj",
                epic_id="ep-1",
                run_id="run-1",
                worker_id="w-1",
                task=task,
                ctx=ctx,
                feedback="",
                hitl_prefix="",
                resolved_profile=profile,
            )

        # Should have been called with names=["skill-a"]
        assert any(c["names"] == ["skill-a"] for c in build_skills_calls)


# ---------------------------------------------------------------------------
# Task A — Evaluator dedicated AgentContext
# ---------------------------------------------------------------------------


class TestEvaluatorDedicatedCtx:
    """Verify that run_one_attempt builds a separate AgentContext for the Evaluator.

    The Evaluator's command allow/deny should come from the evaluator profile
    merged with the repo-level config, NOT from the worker profile.
    """

    def _make_task(self, agent: str | None = None) -> Any:
        from yukar.models.task import Task

        return Task(id="T1", title="test", repo="repo", agent=agent)

    @pytest.mark.asyncio
    async def test_evaluator_ctx_uses_eval_profile_commands(self, tmp_path: Path) -> None:
        """Evaluator profile's commands take effect; worker profile commands do not leak."""
        from yukar.agents.dispatch_attempt import _merge_commands, _resolve_profile
        from yukar.models.agent_profile import AgentProfile
        from yukar.storage.agent_profiles_repo import save_profile

        root = str(tmp_path)
        project_id = "proj"

        # Worker profile allows only ["pytest"]
        await save_profile(
            root,
            project_id,
            AgentProfile(
                name="worker-profile",
                base_role="worker",
                allowed_commands=["pytest"],
            ),
        )
        # Evaluator profile allows ["pytest", "npm"] (wider)
        await save_profile(
            root,
            project_id,
            AgentProfile(
                name="eval-profile",
                base_role="evaluator",
                allowed_commands=["pytest", "npm"],
            ),
        )

        # Repo allows everything the profiles reference.
        repo_allow = ["pytest", "npm", "git"]
        repo_deny: list[str] = []

        # Simulate what run_one_attempt does for the Worker:
        task = self._make_task()
        task.agent = "worker-profile"
        resolved_worker = _resolve_profile(root, project_id, task, expected_role="worker")
        worker_allow, worker_deny = _merge_commands(resolved_worker, repo_allow, repo_deny)
        # Worker can only use pytest.
        assert worker_allow == ["pytest"]

        # Simulate what run_one_attempt does for the Evaluator (independent resolution):
        task_eval = self._make_task()
        task_eval.agent = "eval-profile"
        resolved_eval = _resolve_profile(root, project_id, task_eval, expected_role="evaluator")
        eval_allow, eval_deny = _merge_commands(resolved_eval, repo_allow, repo_deny)
        # Evaluator gets pytest + npm — NOT restricted to worker's pytest-only allow.
        assert set(eval_allow) == {"pytest", "npm"}

    @pytest.mark.asyncio
    async def test_no_eval_profile_evaluator_gets_repo_allow(self, tmp_path: Path) -> None:
        """When no evaluator profile exists, Evaluator gets the full repo allow list.

        Even if the Worker profile restricts allow to a subset, the Evaluator
        should fall back to the repo allow list, not inherit the worker restriction.
        """
        from yukar.agents.dispatch_attempt import _merge_commands, _resolve_profile
        from yukar.models.agent_profile import AgentProfile
        from yukar.storage.agent_profiles_repo import save_profile

        root = str(tmp_path)
        project_id = "proj"

        # Worker profile restricts allow to ["pytest"] only.
        await save_profile(
            root,
            project_id,
            AgentProfile(
                name="strict-worker",
                base_role="worker",
                allowed_commands=["pytest"],
            ),
        )

        repo_allow = ["pytest", "npm", "git"]
        repo_deny: list[str] = []

        # Worker ctx: restricted by worker profile.
        task_worker = self._make_task()
        task_worker.agent = "strict-worker"
        resolved_worker = _resolve_profile(root, project_id, task_worker, expected_role="worker")
        worker_allow, _ = _merge_commands(resolved_worker, repo_allow, repo_deny)
        assert worker_allow == ["pytest"]

        # Evaluator ctx: no evaluator profile → resolved_eval_profile is None →
        # _merge_commands returns repo allow unchanged.
        task_eval = self._make_task()
        task_eval.agent = None  # no profile assigned
        resolved_eval = _resolve_profile(root, project_id, task_eval, expected_role="evaluator")
        assert resolved_eval is None  # no profile → None
        eval_allow, eval_deny = _merge_commands(resolved_eval, repo_allow, repo_deny)
        # Evaluator must NOT inherit the worker's restricted allow list.
        assert set(eval_allow) == {"pytest", "npm", "git"}
        assert eval_deny == []
