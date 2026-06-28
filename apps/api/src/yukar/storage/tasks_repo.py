"""Tasks CRUD — tasks.yaml per epic."""

from __future__ import annotations

from yukar.config import paths
from yukar.models.task import TasksFile
from yukar.storage.yaml_io import load_model_async, save_model


async def get_tasks(root: str, project_id: str, epic_id: str) -> TasksFile:
    yaml_path = paths.tasks_yaml(root, project_id, epic_id)
    return await load_model_async(yaml_path, TasksFile, default=TasksFile())


async def save_tasks(root: str, project_id: str, epic_id: str, tasks: TasksFile) -> None:
    yaml_path = paths.tasks_yaml(root, project_id, epic_id)
    await save_model(yaml_path, tasks)
