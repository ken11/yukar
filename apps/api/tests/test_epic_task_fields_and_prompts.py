"""Wave 1 gap-closure tests — spec audit B2/F3/H1/H2/F4/C2/C3.

Covers:
1. Epic.acceptance_criteria field: model, YAML round-trip, backward compat
2. Task.contract field: model, YAML round-trip, backward compat
3. task_update tool: contract parameter is stored in TasksFile
4. Manager prompt: acceptance_criteria injected into Turn 0 prompt
5. Worker prompt: task.contract injected
6. Evaluator prompt: task.contract + epic.acceptance_criteria injected
7. Manager docs tools: read_project_docs / write_project_doc / read_epic_docs / write_epic_doc
8. Evaluator gets repo_search / repo_summarize when indexer_service provided
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 1. Epic.acceptance_criteria — model and backward compat
# ---------------------------------------------------------------------------


class TestEpicAcceptanceCriteria:
    def test_default_is_empty_string(self) -> None:
        from yukar.models.epic import Epic

        e = Epic(id="EP-1", slug="s", title="T")
        assert e.acceptance_criteria == ""

    def test_set_acceptance_criteria(self) -> None:
        from yukar.models.epic import Epic

        e = Epic(id="EP-1", slug="s", title="T", acceptance_criteria="All tests pass.")
        assert e.acceptance_criteria == "All tests pass."

    def test_backward_compat_yaml_without_field(self, tmp_path: Path) -> None:
        """Existing epic.yaml without acceptance_criteria must parse with '' default."""
        from yukar.models.epic import Epic

        # Simulate YAML dict that predates the field
        raw: dict[str, Any] = {
            "id": "EP-1",
            "slug": "test",
            "title": "Old Epic",
            "description": "desc",
            "status": "planned",
            "branch": "",
            "touched_repos": [],
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
        }
        epic = Epic.model_validate(raw)
        assert epic.acceptance_criteria == ""

    async def test_round_trip_via_epic_repo(self, tmp_path: Path) -> None:
        """Write and read an epic with acceptance_criteria via epic_repo."""
        from yukar.models.epic import Epic
        from yukar.storage import epic_repo

        root = str(tmp_path / "ws")
        epic = Epic(
            id="EP-1",
            slug="test",
            title="My Epic",
            acceptance_criteria="The endpoint returns 200 with field X.",
        )
        await epic_repo.save_epic(root, "proj", epic)
        loaded = await epic_repo.get_epic(root, "proj", "EP-1")
        assert loaded is not None
        assert loaded.acceptance_criteria == "The endpoint returns 200 with field X."

    async def test_create_epic_api_acceptance_criteria(
        self, app_client: Any, tmp_workspace: Path
    ) -> None:
        """POST /api/projects/{p}/epics with acceptance_criteria stores the value."""
        from yukar.models.project import Project
        from yukar.storage.project_repo import save_project

        root = str(tmp_workspace)
        project_id = "proj"
        await save_project(root, Project(id=project_id, name=project_id))

        resp = await app_client.post(
            f"/api/projects/{project_id}/epics",
            json={
                "title": "Test Epic",
                "description": "desc",
                "acceptance_criteria": "All tests green.",
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["acceptance_criteria"] == "All tests green."


# ---------------------------------------------------------------------------
# 2. Task.contract — model and backward compat
# ---------------------------------------------------------------------------


class TestTaskContract:
    def test_default_is_empty_string(self) -> None:
        from yukar.models.task import Task

        t = Task(id="T1", title="Do something")
        assert t.contract == ""

    def test_set_contract(self) -> None:
        from yukar.models.task import Task

        t = Task(id="T1", title="Do something", contract="Implement foo.py with bar().")
        assert t.contract == "Implement foo.py with bar()."

    def test_backward_compat_yaml_without_field(self) -> None:
        """Existing tasks.yaml without contract must parse with '' default."""
        from yukar.models.task import TasksFile

        raw: dict[str, Any] = {
            "tasks": [
                {
                    "id": "T1",
                    "title": "Old task",
                    "status": "todo",
                    "repo": None,
                    "depends_on": [],
                    "thread": None,
                    # no contract key
                }
            ],
            "progress": {"done": 0, "total": 1},
        }
        tf = TasksFile.model_validate(raw)
        assert tf.tasks[0].contract == ""

    async def test_round_trip_via_tasks_repo(self, tmp_path: Path) -> None:
        """Write and read tasks.yaml with contract via tasks_repo."""
        from yukar.models.task import Task, TasksFile
        from yukar.storage import tasks_repo

        root = str(tmp_path / "ws")
        tf = TasksFile(
            tasks=[Task(id="T1", title="build it", contract="Create src/foo.py; pytest passes.")]
        )
        await tasks_repo.save_tasks(root, "proj", "EP-1", tf)
        loaded = await tasks_repo.get_tasks(root, "proj", "EP-1")
        assert loaded.tasks[0].contract == "Create src/foo.py; pytest passes."


# ---------------------------------------------------------------------------
# 3. task_update tool stores contract
# ---------------------------------------------------------------------------


class TestTaskUpdateToolContract:
    async def test_task_update_tool_sets_contract(self, tmp_path: Path) -> None:
        """task_update tool should persist contract when provided."""

        from strands import Agent

        from yukar.agents.orchestrator import _make_task_update_tool
        from yukar.llm.fake import FakeModel, ToolUseTurn
        from yukar.models.task import TasksFile
        from yukar.storage import tasks_repo

        root = str(tmp_path / "ws")
        project_id = "p"
        epic_id = "EP-1"
        run_id = "r1"

        tasks_holder: list[TasksFile] = [TasksFile()]
        tool = _make_task_update_tool(root, project_id, epic_id, run_id, tasks_holder)

        model = FakeModel(
            script=[
                ToolUseTurn(
                    tool_name="task_update",
                    tool_input={
                        "task_id": "T1",
                        "title": "build it",
                        "status": "todo",
                        "repo": "myrepo",
                        "contract": "Create foo.py, run pytest, all tests pass.",
                    },
                ),
            ]
        )

        from yukar.events import bus as event_bus

        # Dummy subscriber so publish doesn't raise
        async with event_bus.subscribe(project_id, epic_id):
            agent = Agent(model=model, tools=[tool])
            async for _ in agent.stream_async("update task"):
                pass

        saved = await tasks_repo.get_tasks(root, project_id, epic_id)
        assert saved.tasks[0].contract == "Create foo.py, run pytest, all tests pass."

    async def test_task_update_tool_updates_contract_on_existing_task(self, tmp_path: Path) -> None:
        """task_update called twice should update contract on existing task."""
        from strands import Agent

        from yukar.agents.orchestrator import _make_task_update_tool
        from yukar.events import bus as event_bus
        from yukar.llm.fake import FakeModel, ToolUseTurn
        from yukar.models.task import Task, TasksFile

        root = str(tmp_path / "ws")

        tasks_holder: list[TasksFile] = [
            TasksFile(tasks=[Task(id="T1", title="old", contract="old contract")])
        ]
        tool = _make_task_update_tool(root, "p", "EP-1", "r1", tasks_holder)

        model = FakeModel(
            script=[
                ToolUseTurn(
                    tool_name="task_update",
                    tool_input={
                        "task_id": "T1",
                        "title": "new title",
                        "contract": "new contract",
                    },
                ),
            ]
        )

        async with event_bus.subscribe("p", "EP-1"):
            agent = Agent(model=model, tools=[tool])
            async for _ in agent.stream_async("update"):
                pass

        assert tasks_holder[0].tasks[0].contract == "new contract"


# ---------------------------------------------------------------------------
# 4. Manager prompt: acceptance_criteria injection
# ---------------------------------------------------------------------------


class TestManagerPromptCriteria:
    def test_acceptance_criteria_injected_when_set(self) -> None:
        from yukar.agents.prompts import _build_manager_prompt
        from yukar.models.epic import Epic

        epic = Epic(
            id="EP-1",
            slug="test",
            title="My Epic",
            description="desc",
            acceptance_criteria="All endpoints return < 200ms.",
        )
        prompt = _build_manager_prompt(epic, "", "", "")
        assert "All endpoints return < 200ms." in prompt
        assert "Acceptance Criteria" in prompt

    def test_acceptance_criteria_omitted_when_empty(self) -> None:
        from yukar.agents.prompts import _build_manager_prompt
        from yukar.models.epic import Epic

        epic = Epic(id="EP-1", slug="test", title="My Epic", description="desc")
        prompt = _build_manager_prompt(epic, "", "", "")
        assert "Acceptance Criteria" not in prompt

    def test_repo_inspection_instruction_present(self) -> None:
        from yukar.agents.prompts import _build_manager_prompt
        from yukar.models.epic import Epic

        epic = Epic(id="EP-1", slug="test", title="My Epic")
        prompt = _build_manager_prompt(epic, "", "", "")
        # Must mention repo inspection before task planning
        assert "repo_summarize" in prompt or "inspect" in prompt.lower()

    def test_manager_system_prompt_mentions_contract(self) -> None:
        from yukar.agents.prompts import _MANAGER_SYSTEM_PROMPT

        assert "contract" in _MANAGER_SYSTEM_PROMPT.lower()

    def test_manager_system_prompt_mentions_repo_inspection(self) -> None:
        from yukar.agents.prompts import _MANAGER_SYSTEM_PROMPT

        assert "repo_summarize" in _MANAGER_SYSTEM_PROMPT or "repo" in _MANAGER_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 5. Worker prompt: task.contract injection
# ---------------------------------------------------------------------------


class TestWorkerPromptContract:
    def test_contract_injected_when_set(self) -> None:
        from pathlib import Path

        from yukar.agents.prompts import _build_worker_prompt
        from yukar.models.task import Task

        task = Task(id="T1", title="build it", contract="Create foo.py; pytest passes.")
        prompt = _build_worker_prompt(task, Path("/tmp/wt"), feedback="", hitl_prefix="")
        assert "Create foo.py; pytest passes." in prompt
        assert "Task Contract" in prompt

    def test_contract_omitted_when_empty(self) -> None:
        from pathlib import Path

        from yukar.agents.prompts import _build_worker_prompt
        from yukar.models.task import Task

        task = Task(id="T1", title="build it")
        prompt = _build_worker_prompt(task, Path("/tmp/wt"), feedback="", hitl_prefix="")
        assert "Task Contract" not in prompt


# ---------------------------------------------------------------------------
# 6. Evaluator prompt: contract + acceptance_criteria injection
# ---------------------------------------------------------------------------


class TestEvaluatorPromptCriteria:
    def test_task_contract_injected(self) -> None:
        from pathlib import Path

        from yukar.agents.prompts import _build_evaluator_prompt
        from yukar.models.epic import Epic
        from yukar.models.task import Task

        task = Task(id="T1", title="build it", contract="Create foo.py.")
        epic = Epic(id="EP-1", slug="s", title="T")
        prompt = _build_evaluator_prompt(task, Path("/tmp/wt"), epic=epic)
        assert "Create foo.py." in prompt
        assert "Task Contract" in prompt

    def test_acceptance_criteria_injected(self) -> None:
        from pathlib import Path

        from yukar.agents.prompts import _build_evaluator_prompt
        from yukar.models.epic import Epic
        from yukar.models.task import Task

        task = Task(id="T1", title="build it", contract="Create foo.py.")
        epic = Epic(
            id="EP-1",
            slug="s",
            title="T",
            acceptance_criteria="All integration tests pass.",
        )
        prompt = _build_evaluator_prompt(task, Path("/tmp/wt"), epic=epic)
        assert "All integration tests pass." in prompt
        assert "Acceptance Criteria" in prompt

    def test_no_epic_arg_still_works(self) -> None:
        from pathlib import Path

        from yukar.agents.prompts import _build_evaluator_prompt
        from yukar.models.task import Task

        task = Task(id="T1", title="build it")
        prompt = _build_evaluator_prompt(task, Path("/tmp/wt"))
        assert "Evaluate Task: T1" in prompt

    def test_evaluator_system_prompt_mentions_contract(self) -> None:
        from yukar.agents.prompts import _EVALUATOR_SYSTEM_PROMPT

        assert "contract" in _EVALUATOR_SYSTEM_PROMPT.lower()


# ---------------------------------------------------------------------------
# 7. Manager docs tools
# ---------------------------------------------------------------------------


class TestManagerDocsTools:
    async def test_write_and_read_project_doc(self, tmp_path: Path) -> None:
        from yukar.agents.tools.docs_tools import make_manager_docs_tools

        root = str(tmp_path / "ws")
        tools = make_manager_docs_tools(root, "proj", "EP-1")
        # find by name
        tool_map = {t.__name__: t for t in tools}

        write_result = await tool_map["write_project_doc"](
            filename="decisions.md", content="# Decisions\n\nUse pydantic v2."
        )
        assert write_result["ok"] is True

        read_result = tool_map["read_project_docs"]()
        assert "decisions.md" in read_result["files"]
        assert "Use pydantic v2." in read_result["docs"]["decisions.md"]

    async def test_write_and_read_epic_doc(self, tmp_path: Path) -> None:
        from yukar.agents.tools.docs_tools import make_manager_docs_tools

        root = str(tmp_path / "ws")
        tools = make_manager_docs_tools(root, "proj", "EP-1")
        tool_map = {t.__name__: t for t in tools}

        write_result = await tool_map["write_epic_doc"](
            filename="plan.md", content="# Plan\n\nTask T1: add endpoint."
        )
        assert write_result["ok"] is True

        read_result = tool_map["read_epic_docs"]()
        assert "plan.md" in read_result["files"]
        assert "Task T1: add endpoint." in read_result["docs"]["plan.md"]

    async def test_read_returns_empty_when_no_docs(self, tmp_path: Path) -> None:
        from yukar.agents.tools.docs_tools import make_manager_docs_tools

        root = str(tmp_path / "ws")
        tools = make_manager_docs_tools(root, "proj", "EP-NEW")
        tool_map = {t.__name__: t for t in tools}

        assert tool_map["read_project_docs"]()["files"] == []
        assert tool_map["read_epic_docs"]()["files"] == []

    async def test_write_invalid_filename_returns_error(self, tmp_path: Path) -> None:
        from yukar.agents.tools.docs_tools import make_manager_docs_tools

        root = str(tmp_path / "ws")
        tools = make_manager_docs_tools(root, "proj", "EP-1")
        tool_map = {t.__name__: t for t in tools}

        # no .md extension → should return error dict, not raise
        result = await tool_map["write_project_doc"](
            filename="traversal/../hack.txt", content="bad"
        )
        assert result["ok"] is False

    def test_four_tools_returned(self, tmp_path: Path) -> None:
        from yukar.agents.tools.docs_tools import make_manager_docs_tools

        tools = make_manager_docs_tools(str(tmp_path), "p", "EP-1")
        names = {t.__name__ for t in tools}
        assert names == {
            "read_project_docs",
            "write_project_doc",
            "read_epic_docs",
            "write_epic_doc",
        }


# ---------------------------------------------------------------------------
# 8. Evaluator gets repo_search/repo_summarize when indexer_service provided
# ---------------------------------------------------------------------------


class TestEvaluatorRepoTools:
    async def test_make_evaluator_tools_with_indexer(self, tmp_path: Path) -> None:
        """make_evaluator_tools result is a list; evaluator.py appends repo tools."""
        from types import SimpleNamespace
        from typing import cast

        from yukar.agents.context import AgentContext
        from yukar.agents.tools.evaluator_tools import make_evaluator_tools
        from yukar.agents.tools.repo_tools import make_repo_tools

        ctx = cast(
            AgentContext,
            SimpleNamespace(
                worktree_path=tmp_path,
                workspace_root=str(tmp_path),
                project_id="proj",
                repo_name="myrepo",
            ),
        )

        base_tools = make_evaluator_tools(ctx)
        assert len(base_tools) == 2  # read_diff, run_tests baseline

        # Simulate what run_evaluator does: append repo tools.
        class FakeIndexer:
            workspace_root = str(tmp_path)

            async def search(self, *a: Any, **kw: Any) -> list[Any]:
                return []

        repo_tools = make_repo_tools("proj", FakeIndexer(), repo_name="myrepo")
        all_tools = [*base_tools, *repo_tools]
        tool_names = {t.__name__ for t in all_tools}
        assert "read_diff" in tool_names
        assert "run_tests" in tool_names
        assert "repo_search" in tool_names
        assert "repo_summarize" in tool_names

    async def test_run_evaluator_signature_accepts_indexer_service(self) -> None:
        """run_evaluator accepts indexer_service and epic kwargs without error."""
        import inspect

        from yukar.agents.evaluator import run_evaluator

        sig = inspect.signature(run_evaluator)
        assert "indexer_service" in sig.parameters
        assert "epic" in sig.parameters
