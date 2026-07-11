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
"""

from __future__ import annotations

from datetime import UTC, datetime

from yukar.events import bus as event_bus
from yukar.models.epic import Epic
from yukar.models.events import EpicMergedEvent
from yukar.storage.epic_repo import save_epic


async def record_epic_merged(
    root: str,
    project_id: str,
    epic: Epic,
    run_id: str = "",
) -> bool:
    """Record the merge fact on *epic* and publish ``EpicMergedEvent``.

    No-op (returns ``False``) when the fact is already recorded.  Otherwise
    sets ``merged_at``, persists the epic, publishes the event, and returns
    ``True``.  ``run_id`` is the arbiter run id when called from a batch merge
    (empty for the single-repo merge endpoint, which has no run).
    """
    if epic.merged_at is not None:
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
