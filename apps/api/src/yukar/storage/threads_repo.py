"""Threads index CRUD — threads.yaml (thin listing index)."""

from __future__ import annotations

from typing import Literal

from yukar.config import paths
from yukar.models.thread import ThreadEntry, ThreadsFile
from yukar.storage.yaml_io import load_model_async, save_model

_ThreadStatus = Literal["active", "resolved", "failed", "archived"]


async def get_threads(root: str, project_id: str, epic_id: str) -> ThreadsFile:
    yaml_path = paths.threads_yaml(root, project_id, epic_id)
    return await load_model_async(yaml_path, ThreadsFile, default=ThreadsFile())


async def save_threads(root: str, project_id: str, epic_id: str, threads: ThreadsFile) -> None:
    yaml_path = paths.threads_yaml(root, project_id, epic_id)
    await save_model(yaml_path, threads)


async def add_thread(root: str, project_id: str, epic_id: str, entry: ThreadEntry) -> None:
    tf = await get_threads(root, project_id, epic_id)
    tf.threads.append(entry)
    await save_threads(root, project_id, epic_id, tf)


async def update_thread_status(
    root: str,
    project_id: str,
    epic_id: str,
    thread_id: str,
    status: _ThreadStatus,
) -> None:
    """Update the status of a single thread entry in threads.yaml.

    No-op if the thread_id is not found (defensive; avoids crashing the
    orchestrator on unexpected thread IDs).
    """
    tf = await get_threads(root, project_id, epic_id)
    changed = False
    for entry in tf.threads:
        if entry.id == thread_id:
            entry.status = status
            changed = True
            break
    if changed:
        await save_threads(root, project_id, epic_id, tf)
