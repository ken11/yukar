"""Workspace trash — O(1) directory teardown for request handlers.

Deleting a trial worktree in-request can take tens of seconds (node_modules
alone is hundreds of thousands of files); the Next.js proxy times the request
out at 30s and the user sees a 500.  Instead, directories are *renamed* into
``<workspace_root>/.trash`` (same filesystem — a single rename syscall) and
actually deleted by a fire-and-forget background sweep.

A sweep deletes everything inside the trash, not just the entry that
scheduled it, so leftovers from a crashed process are collected by the next
sweep — app startup schedules one for exactly that reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import uuid
from pathlib import Path

from yukar.config import paths

logger = logging.getLogger(__name__)

# Strong references so pending sweeps cannot be GC'd mid-run (same pattern as
# app.state.startup_tasks).  Tasks discard themselves on completion.
_sweep_tasks: set[asyncio.Task[None]] = set()


async def discard_to_trash(root: str, path: Path) -> bool:
    """Move *path* into the workspace trash and schedule background deletion.

    Returns ``False`` when the rename could not be performed (the caller
    should fall back to synchronous deletion); never raises.
    """
    trash = paths.trash_dir(root)
    # uuid suffix: several epics may trash same-named repo checkouts in one
    # batch before any sweep runs.
    dest = trash / f"{path.name}-{uuid.uuid4().hex}"

    def _rename() -> None:
        trash.mkdir(parents=True, exist_ok=True)
        os.rename(path, dest)

    try:
        await asyncio.to_thread(_rename)
    except OSError:
        logger.warning("Trash rename failed for %s", path, exc_info=True)
        return False
    schedule_trash_sweep(root)
    return True


def schedule_trash_sweep(root: str) -> None:
    """Delete everything inside the trash in the background (fire-and-forget)."""
    task = asyncio.get_running_loop().create_task(_sweep(root))
    _sweep_tasks.add(task)
    task.add_done_callback(_sweep_tasks.discard)


async def drain_sweeps(timeout: float | None = None) -> None:
    """Wait for pending sweeps (shutdown hygiene / deterministic tests).

    With a *timeout*, still-running deletions are simply left to the OS — the
    renamed entries stay in the trash and the next startup sweep collects
    them, so waiting longer buys nothing.
    """
    # _sweep_tasks is module-global while pytest gives every test its own
    # event loop — a task left over from another (closed) loop cannot be
    # awaited here, so only wait for tasks belonging to the current loop.
    loop = asyncio.get_running_loop()
    tasks = [t for t in _sweep_tasks if t.get_loop() is loop]
    if tasks:
        await asyncio.wait(tasks, timeout=timeout)


async def _sweep(root: str) -> None:
    trash = paths.trash_dir(root)

    def _rm_all() -> None:
        if not trash.is_dir():
            return
        for entry in trash.iterdir():
            # Concurrent sweeps race on the same entries; ignore_errors /
            # missing_ok make the loser a no-op.
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)

    with contextlib.suppress(OSError):
        await asyncio.to_thread(_rm_all)
