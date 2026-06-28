"""Epic CRUD — epic.yaml per epic directory."""

from __future__ import annotations

from yukar.config import paths
from yukar.models.epic import Epic
from yukar.storage.yaml_io import (
    load_model_async,
    load_validated_dir_async,
    read_yaml,
    save_model,
)


def _epic_seq(e: Epic) -> int:
    """Return the numeric suffix of an epic id for tie-breaking (e.g. 'EP-10' → 10)."""
    tail = e.id.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0


async def list_epics(root: str, project_id: str) -> list[Epic]:
    epics_directory = paths.epics_dir(root, project_id)
    if not epics_directory.exists():
        return []
    # Collect candidate yaml paths (same traversal order as before: sorted iterdir).
    candidate_yamls = [
        paths.epic_yaml(root, project_id, d.name)
        for d in sorted(epics_directory.iterdir())
        if d.is_dir() and paths.epic_yaml(root, project_id, d.name).exists()
    ]
    result = await load_validated_dir_async(
        candidate_yamls,
        lambda p: Epic.model_validate(read_yaml(p)),
        "epic",
    )
    # Sort by created_at descending (newest first); tie-break by numeric epic id
    # descending so EP-10 > EP-2 > EP-1 (avoids lexicographic EP-10 < EP-2 bug).
    return sorted(result, key=lambda e: (e.created_at, _epic_seq(e)), reverse=True)


async def get_epic(root: str, project_id: str, epic_id: str) -> Epic | None:
    yaml_path = paths.epic_yaml(root, project_id, epic_id)
    return await load_model_async(yaml_path, Epic, default=None)


async def save_epic(root: str, project_id: str, epic: Epic) -> None:
    yaml_path = paths.epic_yaml(root, project_id, epic.id)
    await save_model(yaml_path, epic)


def make_epic_id(counter: int) -> str:
    return f"EP-{counter}"


def make_branch_name(epic_id: str, slug: str) -> str:
    return f"yukar/{epic_id.lower()}-{slug}"
