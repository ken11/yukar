"""Threads router — CRUD + SSE stream."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from yukar.api.routers import get_epic_or_404
from yukar.deps import WorkspaceRootDep
from yukar.events import bus as event_bus
from yukar.events.sse import format_keepalive, run_event_to_sse, sse_response
from yukar.git.worktree import remove_worktree
from yukar.models.epic import Epic
from yukar.models.message import ContentPart, Message, MessagePayload
from yukar.models.roles import UserCreatableThreadRole
from yukar.models.thread import ThreadEntry, ThreadsFile
from yukar.runs.supervisor import RunSupervisor, get_supervisor
from yukar.storage import session_store, threads_repo


def get_run_supervisor() -> RunSupervisor:
    """Return the active supervisor singleton.

    Defined here (not imported from deps) so that tests can patch
    ``yukar.api.routers.threads.get_run_supervisor`` directly.
    """
    return get_supervisor()


def _supervisor_provider() -> RunSupervisor:
    """Indirection layer so that patching ``get_run_supervisor`` works.

    FastAPI's ``Depends`` captures the function object at definition time.
    By calling ``get_run_supervisor()`` inside a wrapper function, the name
    lookup happens at dependency resolution time — after any test patch has
    replaced the module-level ``get_run_supervisor`` attribute.
    """
    return get_run_supervisor()


SupervisorDep = Annotated[RunSupervisor, Depends(_supervisor_provider)]

router = APIRouter(
    prefix="/api/projects/{project_id}/epics/{epic_id}",
    tags=["threads"],
)


class CreateThreadRequest(BaseModel):
    title: str
    # arbiter is excluded: arbiter threads are created internally by the merge
    # system and must never be directly created via the API.
    role: UserCreatableThreadRole = "user"
    repo: str | None = None
    task: str | None = None
    archive_active: bool = (
        False  # when role=manager, archive the current active trial before creating a new one
    )


class PostMessageRequest(BaseModel):
    content: str
    role: Literal["user", "assistant"] = "user"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_active_manager_thread(epic: Epic, tf: ThreadsFile, thread_id: str) -> bool:
    """Return True if *thread_id* is the current active manager trial.

    Resolution order:
    1. ``epic.active_thread_id`` — the explicit pointer to the active trial.
       If None, fall back to "manager" (backward compatibility with single-trial
       epics that predate this field).
    2. The thread_id must match the resolved active id.
    3. The corresponding ThreadEntry (if present) must have role=manager and
       status != "archived".  Completed (resolved/failed) or interrupted trials
       are still continuable; only archived ones are read-only.  If no
       ThreadEntry exists for the resolved id, accept it only when the resolved
       id is "manager" (backward compat: orchestrator registers the thread
       lazily on run start).

    Args:
        epic: The loaded Epic object (needed for active_thread_id).
        tf: The loaded ThreadsFile (needed for ThreadEntry lookups).
        thread_id: The id to test.

    Returns:
        True when *thread_id* is the active manager trial.
    """
    active_id = epic.active_thread_id or "manager"
    if thread_id != active_id:
        return False
    entry = next((t for t in tf.threads if t.id == thread_id), None)
    if entry is None:
        # No entry yet — accept only the default "manager" id (lazy registration).
        return thread_id == "manager"
    # Accept any non-archived manager trial so that users can send follow-up
    # messages after a run completes (resolved/failed/interrupted).  Archived
    # threads are rejected with 403 upstream (post_message) before this helper
    # is called, so we only need to exclude "archived" here.
    from yukar.agents.trials import is_active_manager_thread as _is_active

    return _is_active(entry)


def _get_manager_branch(epic: Epic, entry: ThreadEntry | None) -> str:
    """Return the git branch for a manager trial.

    If the ThreadEntry has a non-None ``branch``, that is the trial-specific branch.
    Otherwise fall back to ``epic.branch`` (single-trial backward compat).
    """
    if entry is not None and entry.branch is not None:
        return entry.branch
    return epic.branch


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/threads", response_model=list[ThreadEntry])
async def list_threads(project_id: str, epic_id: str, root: WorkspaceRootDep) -> list[ThreadEntry]:
    await get_epic_or_404(root, project_id, epic_id)
    tf = await threads_repo.get_threads(root, project_id, epic_id)
    return tf.threads


@router.post("/threads", response_model=ThreadEntry, status_code=201)
async def create_thread(
    project_id: str,
    epic_id: str,
    body: CreateThreadRequest,
    root: WorkspaceRootDep,
    supervisor: SupervisorDep,
) -> ThreadEntry:
    from yukar.config import paths as p
    from yukar.storage.epic_repo import get_epic, save_epic
    from yukar.storage.thread_locks import epic_thread_lock

    await get_epic_or_404(root, project_id, epic_id)

    async with epic_thread_lock(project_id, epic_id):
        epic = await get_epic(root, project_id, epic_id)
        if epic is None:
            raise HTTPException(status_code=404, detail=f"Epic not found: {epic_id!r}")

        if body.role == "manager":
            tf_existing = await threads_repo.get_threads(root, project_id, epic_id)
            active_id = epic.active_thread_id or "manager"
            existing_active = next(
                (
                    t
                    for t in tf_existing.threads
                    if t.id == active_id and t.role == "manager" and t.status == "active"
                ),
                None,
            )

            if existing_active is not None:
                if not body.archive_active:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"A manager trial ({existing_active.id!r}) is already active. "
                            "Archive it before creating a new one, or pass archive_active=true."
                        ),
                    )
                # archive_active=True: atomically archive then create within the lock
                # if a run is active, return 409 (recovery safety: exit without archiving)
                if supervisor.is_thread_run_active(project_id, epic_id, existing_active.id):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Thread {existing_active.id!r} has an active run. "
                            "Stop the run before archiving."
                        ),
                    )
                # Archive the existing active trial
                existing_active.status = "archived"
                await threads_repo.save_threads(root, project_id, epic_id, tf_existing)

                # Remove worktrees (best-effort)
                _log = logging.getLogger(__name__)
                from yukar.storage.project_repo import get_repo

                for repo_name in list(epic.touched_repos):
                    wt_path = p.worktree_dir(
                        root, project_id, epic_id, existing_active.id, repo_name
                    )
                    if wt_path.exists():
                        repo_obj = await get_repo(root, project_id, repo_name)
                        if repo_obj is None:
                            _log.warning(
                                "create_thread archive: repo %r not found; "
                                "skipping worktree removal for %s",
                                repo_name,
                                wt_path,
                            )
                            continue
                        removed, err = await remove_worktree(
                            Path(repo_obj.path), wt_path, force=True
                        )
                        if not removed:
                            _log.warning(
                                "create_thread archive: failed to remove worktree %s: %s",
                                wt_path,
                                err,
                            )

                # Clear active_thread_id
                epic.active_thread_id = None
                epic.updated_at = datetime.now(UTC)
                await save_epic(root, project_id, epic)

            elif epic.active_thread_id is not None:
                # active_thread_id is set but the corresponding entry was not found,
                # or the entry is resolved/failed (finished but not archived).
                # resolved/failed: by design we do not auto-archive. Proceed to create a new trial.
                # Only a missing entry (genuine inconsistency) returns 409.
                current = next(
                    (
                        t
                        for t in tf_existing.threads
                        if t.id == active_id and t.role == "manager"
                    ),
                    None,
                )
                if current is None:
                    # No ThreadEntry at all — genuine inconsistency.
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Epic active_thread_id is set to {epic.active_thread_id!r} but the "
                            "referenced thread was not found. "
                            "Clear active_thread_id first."
                        ),
                    )
                # resolved / failed → pass through to create the new trial.
                # active_thread_id will be repointed to the new trial below.
                # (archived is impossible here: existing_active check filters status=="active"
                #  and archived entries do not match; the resolved/failed branch lands here.)

        thread_id = f"th-{uuid.uuid4().hex[:8]}"

        # For manager trials: derive a unique branch name and update epic.
        branch: str | None = None
        if body.role == "manager":
            # Collect all existing manager trials (any status) ordered by creation.
            tf_all = await threads_repo.get_threads(root, project_id, epic_id)
            existing_managers = [t for t in tf_all.threads if t.role == "manager"]
            ordinal = len(existing_managers) + 1
            if ordinal == 1:
                # First trial: use the canonical epic.branch (no suffix).
                branch = epic.branch
            else:
                # Subsequent trials: base the suffix on the *first* trial's branch so that
                # ordinals remain "{base}-2", "{base}-3", … regardless of how many times
                # epic.branch has been repointed.
                # The first manager trial always carries the un-suffixed base branch.
                first_manager = existing_managers[0]
                base_branch = (
                    first_manager.branch if first_manager.branch is not None else epic.branch
                )
                branch = f"{base_branch}-{ordinal}"

        # M4: if the manager trial title is empty, assign an ordinal-based default
        title = body.title
        if body.role == "manager" and not title.strip():
            tf_for_title = await threads_repo.get_threads(root, project_id, epic_id)
            n = len([t for t in tf_for_title.threads if t.role == "manager"]) + 1
            title = f"Trial {n}"

        entry = ThreadEntry(
            id=thread_id,
            title=title,
            role=body.role,
            repo=body.repo,
            task=body.task,
            status="active",
            branch=branch if body.role == "manager" else None,
            created_at=datetime.now(UTC),
        )
        # Register agent directory in session store
        state = {
            "title": title,
            "role": body.role,
            "repo": body.repo,
            "task": body.task,
            "status": "active",
        }
        await session_store.ensure_agent(root, project_id, epic_id, thread_id, state)
        # Add to threads.yaml index
        await threads_repo.add_thread(root, project_id, epic_id, entry)

        # For manager trials: update epic.active_thread_id AND epic.branch (active trial's branch),
        # then persist.
        if body.role == "manager":
            epic_obj = await get_epic(root, project_id, epic_id)
            if epic_obj is not None:
                epic_obj.active_thread_id = thread_id
                # Repoint epic.branch to the new active trial's branch so that
                # legacy callers reading epic.branch always see the current trial.
                if branch is not None:
                    epic_obj.branch = branch
                epic_obj.updated_at = datetime.now(UTC)
                await save_epic(root, project_id, epic_obj)

        return entry


@router.get("/threads/{thread_id}", response_model=list[Message])
async def get_thread_messages(
    project_id: str, epic_id: str, thread_id: str, root: WorkspaceRootDep
) -> list[Message]:
    messages = await asyncio.to_thread(
        session_store.list_messages, root, project_id, epic_id, thread_id
    )
    return messages


@router.post("/threads/{thread_id}/archive", status_code=200)
async def archive_thread(
    project_id: str,
    epic_id: str,
    thread_id: str,
    root: WorkspaceRootDep,
    supervisor: SupervisorDep,
) -> dict[str, str]:
    """Archive a manager trial thread.

    Sets its status to ``archived``, removes its worktrees, and clears
    ``epic.active_thread_id`` so a new trial can be created.

    Returns:
        ``{"status": "archived", "thread_id": <id>}``

    Raises:
        400: If the thread is not a manager thread.
        404: If the thread or epic does not exist.
        409: If the thread's run is currently active (stop it first).
    """
    from yukar.config import paths as p
    from yukar.storage.epic_repo import get_epic, save_epic
    from yukar.storage.thread_locks import epic_thread_lock

    await get_epic_or_404(root, project_id, epic_id)

    async with epic_thread_lock(project_id, epic_id):
        tf = await threads_repo.get_threads(root, project_id, epic_id)
        entry = next((t for t in tf.threads if t.id == thread_id), None)

        if entry is None:
            raise HTTPException(status_code=404, detail=f"Thread not found: {thread_id!r}")

        if entry.role != "manager":
            raise HTTPException(
                status_code=400,
                detail=f"Thread {thread_id!r} is not a manager thread (role={entry.role!r}). "
                "Only manager threads can be archived.",
            )

        # Refuse to archive while this trial's run is active.
        if supervisor.is_thread_run_active(project_id, epic_id, thread_id):
            raise HTTPException(
                status_code=409,
                detail=(f"Thread {thread_id!r} has an active run. Stop the run before archiving."),
            )

        # Transition thread status to archived.
        entry.status = "archived"
        await threads_repo.save_threads(root, project_id, epic_id, tf)

        # Remove worktrees for this trial (best-effort: don't fail if already gone).
        _log = logging.getLogger(__name__)

        epic_obj = await get_epic(root, project_id, epic_id)
        if epic_obj is not None:
            from yukar.storage.project_repo import get_repo

            for repo_name in list(epic_obj.touched_repos):
                wt_path = p.worktree_dir(root, project_id, epic_id, thread_id, repo_name)
                if wt_path.exists():
                    # Resolve the bare repo path (needed by remove_worktree).
                    repo_obj = await get_repo(root, project_id, repo_name)
                    if repo_obj is None:
                        _log.warning(
                            "archive_thread: repo %r not found; skipping worktree removal for %s",
                            repo_name,
                            wt_path,
                        )
                        continue
                    removed, err = await remove_worktree(Path(repo_obj.path), wt_path, force=True)
                    if not removed:
                        _log.warning(
                            "archive_thread: failed to remove worktree %s: %s", wt_path, err
                        )

            # Clear active_thread_id so a new trial can be started.
            epic_obj.active_thread_id = None
            epic_obj.updated_at = datetime.now(UTC)
            await save_epic(root, project_id, epic_obj)

    return {"status": "archived", "thread_id": thread_id}


@router.post("/threads/{thread_id}/messages", response_model=Message, status_code=201)
async def post_message(
    project_id: str,
    epic_id: str,
    thread_id: str,
    body: PostMessageRequest,
    root: WorkspaceRootDep,
    supervisor: SupervisorDep,
) -> Message:
    # Reject messages targeting a non-existent epic before any session/state dir
    # is created.  Without this guard a client could post to an arbitrary epic_id
    # and (for the manager path) start a continuation run, leaving orphaned
    # sessions/ and state under a phantom epic.
    epic = await get_epic_or_404(root, project_id, epic_id)

    # Load threads once for all subsequent checks.
    tf = await threads_repo.get_threads(root, project_id, epic_id)

    # Non-user roles are rejected first (422): allowing assistant-role hand-writes
    # would enable duplicate or fabricated messages in the Manager's session history
    # (two-writer hazard).  This check runs before the 403 worker/archived checks
    # so that role-mismatch always gets 422 regardless of thread type.
    if body.role != "user":
        raise HTTPException(
            status_code=422,
            detail=(
                "Only user (HITL) messages may be posted via this endpoint. "
                f"Role {body.role!r} is not accepted."
            ),
        )

    # Enforce read-only policy: worker and evaluator threads are written exclusively
    # by the orchestrator.  Human (role=user) messages to those threads are rejected
    # with 403 to surface the mis-use clearly (K2).
    # Threads with no ThreadEntry (unregistered / ad-hoc) are not blocked here —
    # they fall through to the inject-only path below.
    entry = next((t for t in tf.threads if t.id == thread_id), None)
    if entry is not None and entry.role in ("worker", "evaluator"):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Thread '{thread_id}' is a {entry.role!r} thread and is read-only. "
                "Human messages may only be posted to the manager thread."
            ),
        )
    # Reject messages to archived threads.
    if entry is not None and entry.status == "archived":
        raise HTTPException(
            status_code=403,
            detail=(
                f"Thread '{thread_id}' is archived and no longer accepts messages. "
                "Create a new manager trial to continue."
            ),
        )

    # HITL: for user messages on the active manager thread, use start_or_inject:
    # - active run  → inject directly (unblocks awaiting_input or queued for next turn)
    # - no active run → start a continuation run with the message as seed (I4/K3)

    if _is_active_manager_thread(epic, tf, thread_id):
        # HITL / continuation path.  start_or_inject either:
        #   - active run  → inject directly (unblocks awaiting_input or next turn)
        #   - no active run → start a continuation run with the message as seed
        #
        # The user message is NOT persisted here.  The FSM is the sole writer:
        # - inject path: orchestrator drains the queue; on the next turn
        #   stream_async receives the user text as the sole prompt and FSM
        #   records it as one clean user message.
        # - continuation path: _seed_prompt is passed to turn-0 stream_async;
        #   FSM records it once when the run starts.
        # Either way: if start_or_inject raises (budget/arbiter/409), nothing
        # is written — the client can safely retry.
        #
        # TOCTOU guard (fix 1): also hold epic_thread_lock when starting a continuation
        # before calling start_or_inject. This prevents a continuation from stepping on
        # the worktree immediately after archive has confirmed no run is active.
        # The inject-only path (active run present) is lightweight and safe outside the lock,
        # but since start_or_inject branches internally on is_running, calling it inside
        # the lock uniformly causes no problem.
        # Lock order: epic_thread_lock (outer) → _start_lock (inner, supervisor.start etc.).
        from yukar.storage.thread_locks import epic_thread_lock

        try:
            async with epic_thread_lock(project_id, epic_id):
                await supervisor.start_or_inject(root, project_id, epic_id, thread_id, body.content)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # Return a synthetic Message so the caller gets a 201 with the content
        # it sent.  The canonical message will be written by FSM on the next
        # manager turn; this response is acknowledgement only.
        msg = Message(
            message=MessagePayload(
                role="user",
                content=[ContentPart(text=body.content)],
            ),
            message_id=-1,
            created_at=datetime.now(UTC),
        )
    else:
        # thread_id is not the active manager thread.
        # This branch is reached for:
        #   (a) role=user / ad-hoc threads with no ThreadEntry — inject+append.
        #   (b) manager threads that are not the active trial — 409.
        # Worker/evaluator threads are already rejected as 403 above, and
        # archived threads as 403 above.
        _entry_in_else = next((t for t in tf.threads if t.id == thread_id), None)
        if _entry_in_else is not None and _entry_in_else.role == "manager":
            # A manager trial that is not the active one: silently dropping the
            # message would cause confusing state (message persisted but no run
            # to process it, or processed by the wrong trial's run).
            # Return 409 so the caller knows the message was not routed.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Thread '{thread_id}' is a manager trial but is not the active trial. "
                    "Post messages to the active manager trial."
                ),
            )
        # Non-manager thread (role=user or ad-hoc / no entry): inject-only;
        # no active run → message is a no-op for the agent, but persisting it
        # allows the user to keep a log in the thread.
        supervisor.inject_hitl_message(project_id, epic_id, thread_id, body.content)
        msg = await session_store.append_message(
            root, project_id, epic_id, thread_id, body.role, body.content
        )

    return msg


@router.get("/threads/{thread_id}/stream")
async def thread_stream(
    project_id: str,
    epic_id: str,
    thread_id: str,
) -> StreamingResponse:
    """SSE stream filtered to this thread's token events.

    Backfill ordering (Mn3 fix):
    Subscribe to the live queue *first*, then take a snapshot of the
    per-thread token ring-buffer for replay.  This eliminates the window
    between snapshot and subscribe where published events could be missed.

    Events that appear in both the backfill snapshot and the live queue are
    deduplicated by object identity (``publish`` appends the same object to
    both the ring-buffer and each subscriber queue, so ``id()`` equality is
    an exact match).
    """

    async def _stream() -> AsyncGenerator[str]:
        async with event_bus.subscribe(project_id, epic_id) as q:
            # Snapshot the backfill *after* registering the subscriber so that
            # any event published between snapshot and subscribe is guaranteed
            # to be in q (not lost).
            # Combine token backfill and user-message backfill.
            token_backfill = event_bus.get_thread_token_backfill(project_id, epic_id, thread_id)
            user_msg_backfill = event_bus.get_user_message_backfill(project_id, epic_id, thread_id)
            backfill = token_backfill + user_msg_backfill
            # Track replayed objects by identity to dedup boundary events that
            # may also arrive via the live queue.
            replayed_ids: set[int] = set()
            for buffered_event in backfill:
                if hasattr(buffered_event, "model_dump"):
                    replayed_ids.add(id(buffered_event))
                    yield run_event_to_sse(buffered_event)

            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                    if event is None:
                        break
                    # Skip events that were already delivered via backfill
                    # (object identity dedup — same object appended to both
                    # buffer and queue by publish()).
                    if id(event) in replayed_ids:
                        continue
                    # Filter: only emit events related to this thread
                    evt_thread = getattr(event, "thread_id", None)
                    if (evt_thread is None or evt_thread == thread_id) and hasattr(
                        event, "model_dump"
                    ):
                        yield run_event_to_sse(event)
                except TimeoutError:
                    yield format_keepalive()

    return sse_response(_stream())
