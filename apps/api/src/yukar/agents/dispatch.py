"""Dispatch layer — Worker+Evaluator scheduling for the Epic orchestrator.

``run_dispatch`` is the host-side implementation of the Manager's ``dispatch``
effector tool.  One attempt (worker+evaluator by default; worker-only or
evaluator-only via the item's ``agents`` argument) is delegated to
``dispatch_attempt.run_one_attempt``.  ``ensure_worktree_for_repo`` lives in
``dispatch_attempt`` and is re-exported here for backwards compatibility.

All shared mutable state is passed explicitly via ``DispatchContext`` so that
the functions here are free of hidden ``self`` dependencies.

Public re-exports (``from yukar.agents.dispatch import ...`` keeps working):
- ``DispatchContext``, ``OrchestratorHooks``, ``run_dispatch``
- ``publish_task_update``, ``register_agent_thread``
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Literal

from yukar.agents.dispatch_attempt import ensure_worktree_for_repo, run_one_attempt
from yukar.agents.dispatch_helpers import (
    get_first_repo,
    publish_task_update,
    register_agent_thread,
)
from yukar.agents.failures import (
    AgentFailure,
    dispatch_suspended_reason,
    verbatim_retry_reason,
)
from yukar.models.epic import Epic
from yukar.models.events import DelegationEvent, DelegationItem
from yukar.models.run import RunState
from yukar.models.task import TaskProgress, TasksFile
from yukar.runs.scheduler import WorkerScheduler
from yukar.storage import tasks_repo, threads_repo

logger = logging.getLogger(__name__)

# Callable type for the orchestrator's checkpoint hook.
_CheckpointFn = Callable[[], Coroutine[Any, Any, None]]

# Callable type for the orchestrator's HITL drain.
_DrainFn = Callable[[], list[tuple[str, str]]]

# Callable type for the orchestrator's run_worker/run_evaluator methods.
_RunWorkerFn = Callable[..., Coroutine[Any, Any, dict[str, Any]]]
_RunEvaluatorFn = Callable[..., Coroutine[Any, Any, dict[str, Any]]]


@dataclass(slots=True)
class OrchestratorHooks:
    """Callbacks into EpicOrchestrator for pause/HITL/agent execution.

    Grouping the four callbacks reduces the DispatchContext field count and
    makes it easier to spot that they all originate from the same object.
    """

    checkpoint: _CheckpointFn
    drain_pending: _DrainFn
    run_worker: _RunWorkerFn
    run_evaluator: _RunEvaluatorFn


@dataclass(frozen=True, slots=True)
class FailedAttempt:
    """Record of the last infrastructure failure of a task (per run).

    ``fingerprint`` identifies the dispatch item that failed (contract, feedback,
    agents, repo) so a verbatim re-dispatch after a size failure can be rejected
    deterministically — the half-written work was discarded, so the identical
    item would hit the identical limit.

    For an Evaluator failure, ``subject_files`` / ``contract`` record what the
    Evaluator was judging: re-evaluating the same files (or a superset) under
    the same contract — with any request shape — is equally futile.
    """

    failure: AgentFailure
    role: str
    fingerprint: str
    subject_files: frozenset[str] = frozenset()
    contract: str = ""


@dataclass(slots=True)
class DispatchContext:
    """All shared mutable state needed by the dispatch layer.

    Passed explicitly so dispatch functions carry no hidden ``self`` reference
    to ``EpicOrchestrator``.
    """

    root: str
    project_id: str
    epic_id: str
    run_id: str
    epic: Epic
    state: RunState
    tasks_holder: list[TasksFile]
    attempt_counts: dict[str, int]
    state_lock: asyncio.Lock
    scheduler: WorkerScheduler
    # Live callable — always returns the current stop flag from the orchestrator.
    # This is set once in orchestrator._run_dispatch and never mutated here.
    is_stopped: Callable[[], bool]
    # Dispatch is only ever entered from a running or paused turn — a parked
    # (waiting) run has no in-flight dispatch by construction.
    run_status: Literal["running", "paused"]
    pub: Callable[[object], None]
    max_attempts: int
    # Git author identity used by the host commit after Evaluator acceptance (issue④).
    git_author_name: str
    git_author_email: str
    # Grouped orchestrator callbacks.
    hooks: OrchestratorHooks
    # Manager-conversation identity — used to route HITL messages and to parent
    # worker threads.  Defaults to "manager" for backward compat with single-trial runs.
    manager_thread_id: str = "manager"
    # Trial identity (branch+worktree line) — used to route worktree paths.  Several
    # manager conversations on the same branch share a trial_id (and thus a worktree).
    # Defaults to "manager" for backward compat; equals manager_thread_id for a fresh trial.
    manager_trial_id: str = "manager"
    # Branch the manager trial uses.  Defaults to epic.branch for backward compat.
    # Set to the ThreadEntry.branch of the active trial when it is non-None.
    manager_branch: str = ""
    # Run-scoped failure memory (shared across dispatch calls by the
    # orchestrator).  ``failure_log`` keeps the last infrastructure failure per
    # task (verbatim-retry gate); ``provider_fault`` holds the error detail once
    # a provider-auth failure has been seen — dispatch is then suspended for the
    # rest of the run (every further model call would fail the same way).
    # In-memory only, by design: it is NOT written to state.yaml, so a
    # continuation run (the user's next message) or a server restart starts
    # with a clean slate — which is exactly the "fix the credentials, then send
    # the next message" recovery path the Manager is told about.
    failure_log: dict[str, FailedAttempt] = field(default_factory=dict)
    provider_fault: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Agents-argument helper
# ---------------------------------------------------------------------------

# Valid entries for a dispatch item's ``agents`` argument.
_ALLOWED_AGENTS = ("worker", "evaluator")


def _parse_agents(item: dict[str, Any]) -> tuple[bool, bool] | str:
    """Parse and validate a dispatch item's ``agents`` argument.

    Returns ``(include_worker, include_evaluator)`` on success, or an error
    message string when the value is invalid (the caller rejects the item).

    Allowed values: ``["worker", "evaluator"]`` (the default when omitted),
    ``["worker"]`` and ``["evaluator"]``.  Anything else — an empty list, a
    non-list, or unknown entries — is rejected.  The order of entries is
    irrelevant: execution order is always worker → evaluator.
    """
    raw = item.get("agents")
    if raw is None:
        return (True, True)
    if not isinstance(raw, list) or not all(isinstance(a, str) for a in raw):
        return (
            f'invalid "agents" value {raw!r}: expected a list of strings — '
            '["worker", "evaluator"] (default), ["worker"], or ["evaluator"]'
        )
    if not raw:
        return (
            'invalid "agents" value []: the list must not be empty — use '
            '["worker", "evaluator"] (default), ["worker"], or ["evaluator"]'
        )
    unknown = [a for a in raw if a not in _ALLOWED_AGENTS]
    if unknown:
        return (
            f'invalid "agents" value {raw!r}: unknown entries {unknown!r} — '
            'the only allowed entries are "worker" and "evaluator"'
        )
    chosen = set(raw)
    return ("worker" in chosen, "evaluator" in chosen)


def _item_fingerprint(
    task: Any,
    feedback: str,
    include_worker: bool,
    include_evaluator: bool,
    repo: str,
) -> str:
    """Stable hash of everything that shapes an attempt's input."""
    payload = json.dumps(
        {
            "title": task.title,
            "contract": task.contract,
            "feedback": " ".join(feedback.split()),
            "worker": include_worker,
            "evaluator": include_evaluator,
            "repo": repo,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _changed_paths(worktree: Any) -> list[str]:
    """Paths an evaluator-only attempt would stage: staged, modified, untracked.

    ``git status --porcelain -z --untracked-files=all --no-renames`` — the
    same set ``git add -A`` picks up, resolved BEFORE the attempt so that
    re-evaluating the very subject a previous Evaluator failed on (size /
    no verdict) is rejected without consuming an attempt.
    """
    from yukar.git.runner import run_git

    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"]
    res = await run_git(*args, cwd=worktree, check=False)
    return sorted(
        entry[3:] for entry in (res.stdout or "").split("\0") if len(entry) > 3
    )


# ---------------------------------------------------------------------------
# Verdict helper
# ---------------------------------------------------------------------------


def _verdict(
    task_id: str,
    *,
    accepted: bool,
    status: str,
    feedback: str = "",
    worker_id: str | None = None,
    eval_id: str | None = None,
    reason: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a per-item verdict dict for ``run_dispatch`` results.

    Only keys with non-``None`` values for optional fields (``reason``, any
    extra kwargs) are included so that the LLM-visible JSON shape stays
    identical to the pre-refactor literal dicts.
    """
    d: dict[str, Any] = {
        "task_id": task_id,
        "accepted": accepted,
        "status": status,
        "feedback": feedback,
        "worker_id": worker_id,
        "eval_id": eval_id,
    }
    if reason is not None:
        d["reason"] = reason
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _update_thread_statuses(
    ctx_d: DispatchContext,
    worker_id: str | None,
    eval_id: str | None,
    verdict_status: Literal["resolved", "failed"],
) -> None:
    """Update worker and evaluator thread statuses in threads.yaml.

    Both the accepted (→ "resolved") and rejected (→ "failed") branches share
    the same structure; only *verdict_status* differs.  None guards are preserved
    so callers that do not have a worker_id or eval_id work correctly.

    Must be called from within ``ctx_d.state_lock``.
    """
    if worker_id is not None:
        await threads_repo.update_thread_status(
            ctx_d.root,
            ctx_d.project_id,
            ctx_d.epic_id,
            worker_id,
            verdict_status,
        )
    if eval_id is not None:
        await threads_repo.update_thread_status(
            ctx_d.root,
            ctx_d.project_id,
            ctx_d.epic_id,
            eval_id,
            verdict_status,
        )


async def _mark_blocked(ctx_d: DispatchContext, tf: TasksFile, task: Any) -> None:
    """Set *task* to blocked, publish the update and persist tasks.yaml.

    Must be called from within ``ctx_d.state_lock``.
    """
    task.status = "blocked"
    publish_task_update(
        ctx_d.pub,
        ctx_d.project_id,
        ctx_d.epic_id,
        ctx_d.run_id,
        task.id,
        "blocked",
        task.title,
    )
    done_count = sum(1 for t in tf.tasks if t.status == "done")
    tf.progress = TaskProgress(done=done_count, total=len(tf.tasks))
    await tasks_repo.save_tasks(ctx_d.root, ctx_d.project_id, ctx_d.epic_id, tf)


# ---------------------------------------------------------------------------
# Per-item dispatch handler
# ---------------------------------------------------------------------------


async def _handle_dispatch_item(
    idx: int,
    item: dict[str, Any],
    ctx_d: DispatchContext,
    tf: TasksFile,
    completed_ids: set[str],
    results: list[dict[str, Any]],
) -> None:
    """Validate and execute a single dispatch item (one attempt).

    The item's ``agents`` argument selects the composition (worker+evaluator
    by default, worker-only, or evaluator-only — always worker → evaluator).

    Mutates ``results[idx]`` in place with the item verdict.
    ``completed_ids`` is mutated when a task is accepted so that sibling
    items within the same dispatch call can observe the update.
    """
    task_id: str = item.get("task_id", "")
    feedback: str = item.get("feedback", "")
    repo_override: str | None = item.get("repo")

    # Validate: agents composition (empty / unknown values reject the item).
    parsed_agents = _parse_agents(item)
    if isinstance(parsed_agents, str):
        results[idx] = _verdict(
            task_id,
            accepted=False,
            status="rejected",
            reason=parsed_agents,
        )
        return
    include_worker, include_evaluator = parsed_agents

    # Stop check — use is_stopped() (live callable) so that a stop() issued
    # after DispatchContext was constructed is detected here too.
    if ctx_d.is_stopped():
        results[idx] = _verdict(task_id, accepted=False, status="stopped")
        return

    # Validate: task must exist.
    task = next((t for t in tf.tasks if t.id == task_id), None)
    if task is None:
        results[idx] = _verdict(
            task_id,
            accepted=False,
            status="rejected",
            reason=f"task {task_id!r} not found in tasks.yaml",
        )
        return

    # Validate: task must not be already done.
    if task.status == "done":
        results[idx] = _verdict(task_id, accepted=True, status="done", reason="already done")
        return

    # Validate: deps must be satisfied.
    unsatisfied = [dep for dep in task.depends_on if dep not in completed_ids]
    if unsatisfied:
        results[idx] = _verdict(
            task_id,
            accepted=False,
            status="rejected",
            reason=f"dependencies not yet completed: {unsatisfied}",
        )
        return

    # Resolve repo.
    repo_name: str | None = repo_override or task.repo
    if repo_name is None:
        repo_obj = await get_first_repo(ctx_d.root, ctx_d.project_id)
        repo_name = repo_obj.name if repo_obj else None
    if repo_name is None:
        async with ctx_d.state_lock:
            task.status = "blocked"
            publish_task_update(
                ctx_d.pub,
                ctx_d.project_id,
                ctx_d.epic_id,
                ctx_d.run_id,
                task.id,
                "blocked",
                task.title,
            )
            await tasks_repo.save_tasks(ctx_d.root, ctx_d.project_id, ctx_d.epic_id, tf)
        results[idx] = _verdict(
            task_id,
            accepted=False,
            status="blocked",
            reason="no repo configured for this task or project",
        )
        return

    # Host gate: once a provider-auth failure has been seen in this run, every
    # further agent call would fail the same way — reject without consuming an
    # attempt and tell the Manager to report to the user.
    if ctx_d.provider_fault:
        results[idx] = _verdict(
            task_id,
            accepted=False,
            status="rejected",
            reason=dispatch_suspended_reason(ctx_d.provider_fault[0]),
        )
        return

    # Host gate: a verbatim re-dispatch after a size failure (max_tokens /
    # context_overflow) is futile — the half-written work was discarded, so
    # the identical item hits the identical limit.  Reject it without
    # consuming an attempt; a changed contract, feedback, or agents composition
    # (different fingerprint) is allowed through.
    fingerprint = _item_fingerprint(task, feedback, include_worker, include_evaluator, repo_name)
    prev = ctx_d.failure_log.get(task_id)
    if prev is not None and prev.failure.retry_verbatim_futile and prev.fingerprint == fingerprint:
        results[idx] = _verdict(
            task_id,
            accepted=False,
            status="rejected",
            reason=verbatim_retry_reason(task_id, prev.failure.kind, role=prev.role),
        )
        return

    # Evaluator-only pre-check: there must BE something to evaluate.  The
    # worktree is created lazily by worker-bearing attempts; if it does not
    # exist yet, an evaluator-only item has no evaluation subject — reject
    # instead of creating an empty worktree.  Checked before the attempt
    # counter so a rejected item consumes no attempt.
    if not include_worker:
        from yukar.config import paths as p

        worktree_probe = p.worktree_dir(
            ctx_d.root, ctx_d.project_id, ctx_d.epic_id, ctx_d.manager_trial_id, repo_name
        )
        if not worktree_probe.exists():
            results[idx] = _verdict(
                task_id,
                accepted=False,
                status="rejected",
                reason=(
                    f'agents=["evaluator"] rejected: no worktree exists yet for repo '
                    f"{repo_name!r}, so there is nothing to evaluate. Run a worker "
                    "attempt first (it creates the worktree and produces the changes)."
                ),
            )
            return

        # Resolve the evaluation subject up front (no attempt consumed):
        # re-evaluating the very same subject (same contract, same or a
        # superset of the files) that an Evaluator already failed on for size
        # / no verdict is futile whatever the request shape — reject it.  The
        # way through is a smaller task (task_update) or a changed contract.
        changed = await _changed_paths(worktree_probe)
        if (
            prev is not None
            and prev.role == "evaluator"
            and prev.failure.retry_verbatim_futile
            and prev.contract == task.contract
            and prev.subject_files
            and prev.subject_files <= set(changed)
        ):
            results[idx] = _verdict(
                task_id,
                accepted=False,
                status="rejected",
                reason=verbatim_retry_reason(task_id, prev.failure.kind, role="evaluator"),
            )
            return

    # Host safety: attempt counter upper bound.
    # Read-check-increment under state_lock to prevent double-dispatch
    # races when the same task_id appears in parallel coroutines.
    async with ctx_d.state_lock:
        attempt_count = ctx_d.attempt_counts.get(task_id, 0)
        if attempt_count >= ctx_d.max_attempts:
            await _mark_blocked(ctx_d, tf, task)
            results[idx] = _verdict(
                task_id,
                accepted=False,
                status="blocked",
                reason=f"max attempts ({ctx_d.max_attempts}) reached",
            )
            return

        # Increment attempt counter and mark in_progress atomically.
        ctx_d.attempt_counts[task_id] = attempt_count + 1
        task.status = "in_progress"
        publish_task_update(
            ctx_d.pub,
            ctx_d.project_id,
            ctx_d.epic_id,
            ctx_d.run_id,
            task.id,
            "in_progress",
            task.title,
        )
        await tasks_repo.save_tasks(ctx_d.root, ctx_d.project_id, ctx_d.epic_id, tf)

    # Pause-check BEFORE acquiring the scheduler slot so that a paused
    # run does not hold the semaphore/repo-lock indefinitely.
    await ctx_d.hooks.checkpoint()

    # Run one attempt (worker → evaluator, per the item's agents composition)
    # inside the scheduler slot.
    async with ctx_d.scheduler.slot(repo_name):
        if ctx_d.is_stopped():
            async with ctx_d.state_lock:
                task.status = "todo"
                publish_task_update(
                    ctx_d.pub,
                    ctx_d.project_id,
                    ctx_d.epic_id,
                    ctx_d.run_id,
                    task.id,
                    "todo",
                    task.title,
                )
                await tasks_repo.save_tasks(ctx_d.root, ctx_d.project_id, ctx_d.epic_id, tf)
            results[idx] = _verdict(task_id, accepted=False, status="stopped")
            return

        # Ensure worktree inside repo lock (same repo → no race).
        worktree_path = await ensure_worktree_for_repo(
            ctx_d.root,
            ctx_d.project_id,
            ctx_d.epic_id,
            ctx_d.manager_trial_id,
            ctx_d.manager_branch,
            repo_name,
            ctx_d.state_lock,
            ctx_d.epic,
        )

        try:
            attempt_result = await run_one_attempt(
                ctx_d=ctx_d,
                task=task,
                repo_name=repo_name,
                worktree_path=worktree_path,
                feedback=feedback,
                include_worker=include_worker,
                include_evaluator=include_evaluator,
            )
        except asyncio.CancelledError:
            raise
        except Exception as host_exc:
            # Safety net: a host/tool bug inside the attempt must not abort the
            # whole dispatch call and strand the task in in_progress.  Report it
            # as an explicit attempt failure instead.
            from yukar.agents.dispatch_attempt import AttemptResult
            from yukar.agents.failures import classify_agent_failure, failure_guidance

            logger.exception("dispatch attempt for %s crashed", task_id)
            host_failure = classify_agent_failure(host_exc)
            attempt_result = AttemptResult(
                accepted=False,
                worker_id=None,
                eval_id=None,
                feedback=failure_guidance(host_failure, role="worker", task_id=task_id),
                files_changed=0,
                failure=host_failure,
                failure_role="host",
            )

    # worker_finalized=True means run_one_attempt already marked the worker thread as failed
    # (exception path) — skip double-update for that thread.
    accepted = attempt_result.accepted
    worker_id = attempt_result.worker_id
    eval_id = attempt_result.eval_id
    feedback_out = attempt_result.feedback
    files_changed = attempt_result.files_changed
    worker_finalized = attempt_result.worker_finalized
    failure = attempt_result.failure

    # Update task status and shared state under lock.
    async with ctx_d.state_lock:
        # Failure memory: remember an infrastructure failure for the verbatim-
        # retry gate; a normal outcome (accept or a real evaluator rejection)
        # supersedes any earlier failure record.  A provider-auth failure
        # suspends dispatch for the rest of the run.
        if failure is not None:
            ctx_d.failure_log[task_id] = FailedAttempt(
                failure=failure,
                role=attempt_result.failure_role or "worker",
                fingerprint=fingerprint,
                subject_files=frozenset(attempt_result.subject_files),
                contract=task.contract,
            )
            if failure.run_fatal and not ctx_d.provider_fault:
                ctx_d.provider_fault.append(f"{failure.exc_name}: {failure.detail}")
                logger.error(
                    "dispatch suspended for run %s after provider auth failure: %s",
                    ctx_d.run_id,
                    ctx_d.provider_fault[0],
                )
        else:
            # A real outcome (accept or a code rejection) supersedes the
            # failure memory.
            ctx_d.failure_log.pop(task_id, None)

        if accepted:
            task.status = "done"
            completed_ids.add(task.id)
            publish_task_update(
                ctx_d.pub,
                ctx_d.project_id,
                ctx_d.epic_id,
                ctx_d.run_id,
                task.id,
                "done",
                task.title,
            )
            await _update_thread_statuses(ctx_d, worker_id, eval_id, "resolved")
        else:
            # Return task to todo so Manager can re-dispatch with a new Worker.
            task.status = "todo"
            publish_task_update(
                ctx_d.pub,
                ctx_d.project_id,
                ctx_d.epic_id,
                ctx_d.run_id,
                task.id,
                "todo",
                task.title,
            )
            if worker_finalized:
                # Worker thread status was already set to "failed" inside run_one_attempt
                # (exception path).  Only update the evaluator thread (if any).
                await _update_thread_statuses(ctx_d, None, eval_id, "failed")
            elif failure is not None and attempt_result.failure_role == "evaluator":
                # The Worker finished normally; it was the EVALUATION that
                # aborted (exception / no verdict).  The worker thread is
                # resolved (it did its job — leaving it "active" would show a
                # forever-running worker), only the evaluator thread failed.
                await _update_thread_statuses(ctx_d, worker_id, None, "resolved")
                await _update_thread_statuses(ctx_d, None, eval_id, "failed")
            else:
                await _update_thread_statuses(ctx_d, worker_id, eval_id, "failed")

        done_count = sum(1 for t in tf.tasks if t.status == "done")
        tf.progress = TaskProgress(done=done_count, total=len(tf.tasks))
        await tasks_repo.save_tasks(ctx_d.root, ctx_d.project_id, ctx_d.epic_id, tf)

    # Drain any HITL for the manager and append to result.
    hitl_messages: list[str] = [
        text for (tid, text) in ctx_d.hooks.drain_pending() if tid == ctx_d.manager_thread_id
    ]

    extra_kw: dict[str, Any] = {"diff_files_changed": files_changed}
    if hitl_messages:
        extra_kw["hitl_messages"] = hitl_messages
    if failure is not None:
        # status="failed" (not needs_fix): the attempt aborted for an
        # infrastructure reason and says nothing about the code.  ``reason``
        # names the kind; ``feedback`` carries the host's guidance.
        results[idx] = _verdict(
            task_id,
            accepted=False,
            status="failed",
            feedback=feedback_out,
            worker_id=worker_id,
            eval_id=eval_id,
            reason=f"{attempt_result.failure_role or 'worker'} failed: {failure.kind}",
            **extra_kw,
        )
        return
    results[idx] = _verdict(
        task_id,
        accepted=accepted,
        status="done" if accepted else "needs_fix",
        feedback=feedback_out,
        worker_id=worker_id,
        eval_id=eval_id,
        **extra_kw,
    )


# ---------------------------------------------------------------------------
# Main dispatch entry point
# ---------------------------------------------------------------------------


async def run_dispatch(
    ctx_d: DispatchContext,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Host-side implementation of the dispatch effector tool.

    Validates each item (including its ``agents`` composition), enforces
    attempt limits, runs the attempt via scheduler.slot, and returns
    per-item verdicts.
    """
    tf = ctx_d.tasks_holder[0]
    completed_ids = {t.id for t in tf.tasks if t.status == "done"}

    # Dedup: reject duplicate task_ids within the same dispatch call.
    seen_ids: set[str] = set()
    deduped_items: list[dict[str, Any]] = []
    for item in items:
        tid = item.get("task_id", "")
        if tid in seen_ids:
            logger.warning("dispatch: duplicate task_id %r in items list; skipping", tid)
            continue
        seen_ids.add(tid)
        deduped_items.append(item)
    items = deduped_items

    # One independent dict slot per item (avoid aliasing a single shared {}).
    results: list[dict[str, Any]] = [{} for _ in items]

    # Emit DelegationEvent before Workers start.
    if items:
        delegation_items: list[DelegationItem] = []
        for item in items:
            task_id_d = item.get("task_id", "")
            repo_d: str | None = item.get("repo")
            task_d = next((t for t in tf.tasks if t.id == task_id_d), None)
            delegation_items.append(
                DelegationItem(
                    task_id=task_id_d,
                    repo=repo_d or (task_d.repo if task_d else None),
                    title=task_d.title if task_d else None,
                )
            )
        ctx_d.pub(
            DelegationEvent(
                project_id=ctx_d.project_id,
                epic_id=ctx_d.epic_id,
                run_id=ctx_d.run_id,
                items=delegation_items,
            )
        )

    # Run all items in parallel (scheduler enforces repo serialisation).
    if len(items) == 1:
        await _handle_dispatch_item(0, items[0], ctx_d, tf, completed_ids, results)
    else:
        async with asyncio.TaskGroup() as tg:
            for idx, item in enumerate(items):
                tg.create_task(_handle_dispatch_item(idx, item, ctx_d, tf, completed_ids, results))

    return results


# ---------------------------------------------------------------------------
# Re-exports for backwards compatibility
# ---------------------------------------------------------------------------
# These names must remain importable from ``yukar.agents.dispatch``.

__all__ = [
    "DispatchContext",
    "FailedAttempt",
    "OrchestratorHooks",
    "run_dispatch",
    "publish_task_update",
    "register_agent_thread",
]
