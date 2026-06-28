"""Run state CRUD — state.yaml per epic."""

from __future__ import annotations

from yukar.config import paths
from yukar.models.run import RunState
from yukar.storage.yaml_io import load_model_async, save_model


async def get_state(root: str, project_id: str, epic_id: str) -> RunState | None:
    yaml_path = paths.state_yaml(root, project_id, epic_id)
    return await load_model_async(yaml_path, RunState, default=None)


async def save_state(root: str, project_id: str, epic_id: str, state: RunState) -> None:
    yaml_path = paths.state_yaml(root, project_id, epic_id)
    await save_model(yaml_path, state)
