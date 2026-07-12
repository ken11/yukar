"""Shared helper for recording the epic merge fact (``merged_at``).

Two call sites write the merge fact — the single-repo merge endpoint
(``POST /git/merge`` → ``_finalize_epic_if_all_merged``) and the batch arbiter
merge (``ArbiterRunner._process_epic``).  Both go through ``record_epic_merged``
so the recording rules live in exactly one place:

- The merge fact is an **attribute**, not a status: the epic stays open
  (only the user flips ``epic.status``; merging does not end the epic).
- Idempotent: once ``merged_at`` is set it is never overwritten, and the
  ``EpicMergedEvent`` is published at most once per epic.
- Reverting the merge later does not clear the fact — ``merged_at`` records
  that a merge operation happened, not the current git state.
- Fresh-read + serialised: the epic is re-read from disk under a lock and
  only ``merged_at`` / ``updated_at`` are written.  A stale ``Epic`` object
  held by a caller can therefore neither roll back concurrent changes (e.g.
  a PATCH that completed the epic mid-merge) nor double-publish the event
  when two merges race.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from yukar.events import bus as event_bus
from yukar.models.events import EpicMergedEvent
from yukar.storage.epic_repo import get_epic, save_epic

_record_lock = asyncio.Lock()
"""Serialises the read-check-write cycle so racing call sites cannot both
observe ``merged_at is None`` and each publish an ``EpicMergedEvent``."""


async def record_epic_merged(
    root: str,
    project_id: str,
    epic_id: str,
    run_id: str = "",
) -> bool:
    """Record the merge fact on the epic and publish ``EpicMergedEvent``.

    Re-reads the epic from disk (under a module-level lock) so only the
    freshest state is mutated.  No-op (returns ``False``) when the epic does
    not exist or the fact is already recorded.  Otherwise sets ``merged_at``,
    persists the epic, publishes the event, and returns ``True``.  ``run_id``
    is the arbiter run id when called from a batch merge (empty for the
    single-repo merge endpoint, which has no run).
    """
    async with _record_lock:
        epic = await get_epic(root, project_id, epic_id)
        if epic is None or epic.merged_at is not None:
            return False
        now = datetime.now(UTC)
        epic.merged_at = now
        epic.updated_at = now
        await save_epic(root, project_id, epic)
        event_bus.publish(
            project_id,
            epic.id,
            EpicMergedEvent(
                project_id=project_id,
                epic_id=epic.id,
                run_id=run_id,
                merged_at=now,
            ),
        )
        return True
