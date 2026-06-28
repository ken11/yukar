"""Manager-trial helpers — ghost-worktree guard and active-trial resolution.

These utilities are shared by:
- runs/resolve_runner.py  (raises RuntimeError on no-active-trial)
- runs/arbiter_runner.py  (raises RuntimeError on no-active-trial)
- api/routers/git.py      (returns skip PruneRepoResult on no-active-trial)
- api/routers/threads.py  (_is_active_manager_thread predicate)

All four locations implement the same security invariant: an epic whose
manager trials have ALL been archived must NOT fall back to the "manager/"
ghost-worktree path.  Factoring the check here prevents semantic drift
between the call sites.

Backward-compatibility rule (must be preserved):
  ``epic.active_thread_id is None`` AND no archived manager threads exist
  → the epic was created before the multi-trial feature and uses the legacy
  "manager" worktree id.  Return "manager" so those epics continue to work.
"""

from __future__ import annotations

from yukar.models.epic import Epic
from yukar.models.thread import ThreadEntry

# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


def is_active_manager_thread(entry: ThreadEntry) -> bool:
    """Return True when *entry* represents a non-archived manager thread.

    Used by ``_is_active_manager_thread`` in api/routers/threads.py to check
    whether a specific thread is the active manager trial.

    Args:
        entry: A ``ThreadEntry`` loaded from threads.yaml.

    Returns:
        True when ``entry.role == "manager"`` and
        ``entry.status != "archived"``.
    """
    return entry.role == "manager" and entry.status != "archived"


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

_BACKWARD_COMPAT_ID = "manager"


async def resolve_active_trial_id(
    root: str,
    project_id: str,
    epic_id: str,
    epic: Epic,
) -> str | None:
    """Resolve the worktree-id for the active manager trial of *epic*.

    Resolution order:

    1. If ``epic.active_thread_id`` is not None, return it directly.
    2. Otherwise load ``threads.yaml`` and check for archived manager threads:
       - If any exist (the multi-trial feature was ever used and all trials
         were subsequently archived), return ``None`` — the caller should
         refuse to fall back to the ghost "manager/" worktree path.
       - If none exist (legacy single-trial epic, pre-multi-trial), return
         ``"manager"`` for backward compatibility.

    Args:
        root: Workspace root path (str).
        project_id: The project identifier.
        epic_id: The epic identifier.
        epic: The loaded ``Epic`` object.

    Returns:
        The active trial worktree id (e.g. ``"manager"`` or a thread id), or
        ``None`` when all trials are archived and the ghost-worktree fallback
        must be refused.
    """
    if epic.active_thread_id is not None:
        return epic.active_thread_id

    # Lazy import avoids a circular dependency at module load time; storage is
    # a leaf layer that must not import from agents/.
    from yukar.storage import threads_repo as _tr  # noqa: PLC0415

    tf = await _tr.get_threads(root, project_id, epic_id)
    has_explicit_archived = any(
        t.role == "manager" and t.status == "archived" for t in tf.threads
    )
    if has_explicit_archived:
        # All manager trials have been archived; refuse the ghost-worktree
        # fallback.  The caller decides whether to raise or skip.
        return None

    # Legacy single-trial epic — backward-compat fallback.
    return _BACKWARD_COMPAT_ID
