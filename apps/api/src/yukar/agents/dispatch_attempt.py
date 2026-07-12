"""Dispatch attempt layer — worktree setup and one dispatch attempt.

Contains:
- ``ensure_worktree_for_repo``: lazily create git worktree, update epic.touched_repos.
- ``run_one_attempt``: execute one attempt with the requested agent composition
  (worker+evaluator by default; worker-only or evaluator-only via the dispatch
  item's ``agents`` argument — execution order is always worker → evaluator).

Both functions operate on the explicit ``DispatchContext`` defined in
``dispatch.py``; they carry no hidden ``self`` reference.

Profile resolution (BE-B)
-------------------------
Profile resolution is performed **once per attempt** in ``run_one_attempt``.
The resolved profile (or ``None`` when absent / base_role mismatches) is passed
to the worker/evaluator hooks so that all three profile dimensions
(instructions / skills / MCP) receive the same resolved value.  Command
permissions are NOT a profile dimension — they come solely from the repo-level
allow/deny list, independent of any profile.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from yukar.agents.context import AgentContext
from yukar.agents.dispatch_helpers import (
    publish_diff_update,
    register_agent_thread,
)
from yukar.config import paths as p
from yukar.git.runner import git_author_env, run_git
from yukar.git.worktree import ensure_worktree
from yukar.models.agent_profile import AgentProfile
from yukar.models.epic import Epic
from yukar.models.events import (
    EvalResultEvent,
    EvaluatorStartedEvent,
    WorkerCompletedEvent,
    WorkerFailedEvent,
    WorkerStartedEvent,
)
from yukar.models.run import ActiveWorker
from yukar.storage import state_repo, threads_repo
from yukar.storage.epic_repo import save_epic
from yukar.storage.project_repo import get_repo

if TYPE_CHECKING:
    from yukar.agents.dispatch import DispatchContext
    from yukar.models.task import Task

logger = logging.getLogger(__name__)


def _resolve_profile(
    root: str,
    project_id: str,
    task: Task,
    expected_role: str,
) -> AgentProfile | None:
    """Resolve the agent profile for *task*, validating base_role.

    Returns the profile when ``task.agent`` is set **and** the profile exists
    **and** ``profile.base_role == expected_role``.  Returns ``None`` and logs
    a warning in every other case so that all four profile dimensions
    (instructions / skills / MCP / commands) are consistently absent.

    This function is the single resolution point per attempt — callers must
    not call ``get_profile`` again for the same task.
    """
    if not task.agent:
        return None

    from yukar.storage.agent_profiles_repo import get_profile

    profile = get_profile(root, project_id, task.agent)
    if profile is None:
        logger.warning(
            "dispatch_attempt: profile %r not found for task %s (role=%s) — using defaults",
            task.agent,
            task.id,
            expected_role,
        )
        return None

    if profile.base_role != expected_role:
        logger.warning(
            "dispatch_attempt: profile %r has base_role=%r (expected %r) for task %s"
            " — ignoring profile (all 4 dimensions)",
            task.agent,
            profile.base_role,
            expected_role,
            task.id,
        )
        return None

    return profile


async def ensure_worktree_for_repo(
    root: str,
    project_id: str,
    epic_id: str,
    trial_id: str,
    manager_branch: str,
    repo_name: str,
    state_lock: asyncio.Lock,
    epic: Epic,
) -> Path:
    """Lazily create worktree for repo under the given trial; update epic.touched_repos.

    The worktree is placed at:
        epics/{epic_id}/worktrees/{trial_id}/{repo_name}

    ``trial_id`` keys the (branch+worktree) line of work: manager conversations
    that continue the same trial share this worktree.  ``manager_branch`` is the
    branch the active trial uses (from ThreadEntry.branch or epic.branch as
    fallback).  ``epic`` is used only for touched_repos tracking.
    """
    repo_obj = await get_repo(root, project_id, repo_name)
    if repo_obj is None:
        raise RuntimeError(f"Repo not found: {repo_name}")

    repo_path = Path(repo_obj.path)
    worktree_path = p.worktree_dir(root, project_id, epic_id, trial_id, repo_name)
    default_branch = repo_obj.default_branch

    result = await ensure_worktree(
        repo_path=repo_path,
        worktree_path=worktree_path,
        branch=manager_branch,
        default_branch=default_branch,
    )

    # Update epic.touched_repos if needed.  Guard the read-modify-write and
    # the epic.yaml save with state_lock so parallel dispatch items on
    # different repos don't race on the shared epic object / epic.yaml write.
    # (The slow ensure_worktree above is intentionally left outside the lock
    # so worktree creation for distinct repos still runs in parallel.)
    async with state_lock:
        if repo_name not in epic.touched_repos:
            epic.touched_repos.append(repo_name)
            epic.updated_at = datetime.now(UTC)
            await save_epic(root, project_id, epic)

    return result


async def run_one_attempt(
    ctx_d: DispatchContext,
    task: Task,
    repo_name: str,
    worktree_path: Path,
    feedback: str,
    *,
    include_worker: bool = True,
    include_evaluator: bool = True,
) -> tuple[bool, str | None, str | None, str, int, bool]:
    """Run one attempt for a task with the requested agent composition.

    The composition comes from the dispatch item's ``agents`` argument
    (validated by the caller).  Execution order is always worker → evaluator:

    - worker + evaluator (default): the classic full cycle — the host commits
      only when the Evaluator accepts.
    - worker only: no evaluation, no host commit.  The Worker's final report
      text is returned as ``feedback_out`` and ``accepted`` is ``True`` (the
      report itself is the deliverable).
    - evaluator only: no Worker and — crucially — NO hermetic reset (the
      uncommitted worktree contents ARE the evaluation subject).  The current
      worktree is staged and evaluated against the task contract; acceptance
      triggers the usual host commit.

    Returns:
        ``(accepted, worker_id, eval_id, feedback_out, files_changed, worker_finalized)``

        *worker_finalized* is ``True`` when this function already marked the worker
        thread as "failed" (Worker exception path).  The caller must not perform a
        second status update for that thread.
    """
    root = ctx_d.root
    project_id = ctx_d.project_id
    epic_id = ctx_d.epic_id
    run_id = ctx_d.run_id
    state = ctx_d.state

    # Repo-level command allow/deny — resolved once, shared by the Worker's and
    # the Evaluator's AgentContexts (fresh copies each, never aliased).
    repo_obj = await get_repo(root, project_id, repo_name)
    repo_allow = list(repo_obj.commands.allow) if repo_obj else []
    repo_deny = list(repo_obj.commands.deny) if repo_obj else []

    worker_id: str | None = None
    worker_summary: str = ""

    if include_worker:
        worker_id = f"worker-{uuid.uuid4().hex[:8]}"

        # Register worker thread (parent = active manager trial in the tree).
        await register_agent_thread(
            root,
            project_id,
            epic_id,
            thread_id=worker_id,
            role="worker",
            repo=repo_name,
            task_id=task.id,
            parent_thread_id=ctx_d.manager_thread_id,
        )

        # Update state.yaml active_workers — append this worker.
        async with ctx_d.state_lock:
            state.active_workers.append(
                ActiveWorker(
                    worker_id=worker_id,
                    task_id=task.id,
                    repo=repo_name,
                )
            )
            state.status = ctx_d.run_status
            state.last_event_at = datetime.now(UTC)
            await state_repo.save_state(root, project_id, epic_id, state)

        ctx_d.pub(
            WorkerStartedEvent(
                project_id=project_id,
                epic_id=epic_id,
                run_id=run_id,
                worker_id=worker_id,
                task_id=task.id,
                repo=repo_name,
            )
        )

        # Resolve agent profile once for this attempt (BE-B).
        # base_role must match "worker"; mismatch or missing → all 4 dimensions ignored.
        resolved_profile = _resolve_profile(root, project_id, task, expected_role="worker")

        # Command permissions come solely from the repo-level allow/deny list; a
        # profile never narrows or grants commands.  Fresh copies so this worker's
        # AgentContext never aliases the list reused for the Evaluator below.
        allow_cmds, deny_cmds = list(repo_allow), list(repo_deny)

        ctx = await AgentContext.create(
            project_id=project_id,
            epic_id=epic_id,
            repo_name=repo_name,
            worktree_path=worktree_path,
            workspace_root=root,
            allow=allow_cmds,
            deny=deny_cmds,
        )

        # Drain HITL for this worker.
        pending = ctx_d.hooks.drain_pending()
        hitl_msgs = [text for (tid, text) in pending if tid == worker_id]
        hitl_prefix = "\n".join(f"[User]: {m}" for m in hitl_msgs)
        if hitl_prefix:
            hitl_prefix = "\n" + hitl_prefix + "\n"

        # Hermetic attempt: the worktree is shared across all tasks/attempts of this
        # (epic, repo).  Because the host now commits only on accept (not the Worker),
        # a rejected or abandoned prior attempt leaves uncommitted residue in the tree.
        # Reset to HEAD (= accepted work, which is committed and preserved) and clean
        # untracked-but-not-ignored files so the Evaluator's --cached diff and the
        # host commit are scoped to THIS attempt only — preventing cross-task
        # contamination (a later task committing a previous task's leftovers).
        # Runs only on the per-epic worktree; the scheduler serialises same-repo slots
        # so no concurrent attempt is touching this tree.
        # NOTE: this reset runs only when the attempt INCLUDES a worker — an
        # evaluator-only attempt evaluates the current (uncommitted) worktree
        # contents, which the reset would destroy.  It also means files written
        # by a previous worker-only attempt (never committed) are discarded here.
        await run_git("reset", "--hard", "HEAD", cwd=worktree_path, check=False)
        await run_git("clean", "-fd", cwd=worktree_path, check=False)

        # Run Worker — pass the resolved profile so orchestrator avoids a second
        # get_profile call.  Wrap in try/except so that Worker exceptions (e.g.
        # MaxTokensReachedException, ContextWindowOverflowException) are caught,
        # the worker thread is marked failed, and a WorkerFailedEvent is published.
        # asyncio.CancelledError is always re-raised (pause/stop path).
        try:
            worker_result = await ctx_d.hooks.run_worker(
                project_id=project_id,
                epic_id=epic_id,
                run_id=run_id,
                worker_id=worker_id,
                task=task,
                ctx=ctx,
                feedback=feedback,
                hitl_prefix=hitl_prefix,
                resolved_profile=resolved_profile,
            )
            worker_summary = (
                worker_result.get("result", "")
                if isinstance(worker_result, dict)
                else ""
            )
        except asyncio.CancelledError:
            raise
        except Exception as worker_exc:
            exc_name = type(worker_exc).__name__
            if exc_name == "MaxTokensReachedException":
                reason = "max_tokens"
            elif exc_name == "ContextWindowOverflowException":
                reason = "context_overflow"
            else:
                reason = exc_name
            logger.warning("worker %s failed with %s: %s", worker_id, exc_name, worker_exc)

            # Remove from active_workers and persist state (mirrors normal completion path).
            async with ctx_d.state_lock:
                state.active_workers = [
                    w for w in state.active_workers if w.worker_id != worker_id
                ]
                state.status = ctx_d.run_status
                state.last_event_at = datetime.now(UTC)
                await state_repo.save_state(root, project_id, epic_id, state)

            await threads_repo.update_thread_status(root, project_id, epic_id, worker_id, "failed")
            ctx_d.pub(
                WorkerFailedEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                    worker_id=worker_id,
                    task_id=task.id,
                    repo=repo_name,
                    reason=reason,
                )
            )
            return (False, worker_id, None, f"worker failed: {reason}", 0, True)

        ctx_d.pub(
            WorkerCompletedEvent(
                project_id=project_id,
                epic_id=epic_id,
                run_id=run_id,
                worker_id=worker_id,
                task_id=task.id,
                repo=repo_name,
            )
        )

        # Remove this worker from active_workers.
        async with ctx_d.state_lock:
            state.active_workers = [w for w in state.active_workers if w.worker_id != worker_id]
            state.status = ctx_d.run_status
            state.last_event_at = datetime.now(UTC)
            await state_repo.save_state(root, project_id, epic_id, state)

    # Host stage: git add -A to stage the changes to be evaluated (including new
    # files) so the Evaluator's read_diff (--cached) sees the complete diff.
    # In the worker+evaluator cycle this stages what the Worker just produced;
    # in an evaluator-only attempt it stages the current worktree contents
    # (e.g. the output of earlier worker-only attempts).  A worker-only attempt
    # also stages so the diff signal reflects the produced files — but nothing
    # is committed (no Evaluator acceptance → no host commit).
    await run_git("add", "-A", cwd=worktree_path, check=False)

    # Publish diff update using the staged diff (see dispatch_helpers).
    files_changed = await publish_diff_update(
        project_id, epic_id, run_id, repo_name, worktree_path, ctx_d.pub
    )

    if not include_evaluator:
        # Worker-only attempt: no evaluation and no host commit.  The Worker's
        # final report text is the deliverable — return it as feedback and
        # report the attempt as accepted so the caller marks the task done.
        # A silent Worker has produced NO deliverable: reject instead of
        # silently marking the task done with an empty report (the Manager
        # decides whether to retry or replan).
        if not worker_summary.strip():
            return (
                False,
                worker_id,
                None,
                "Worker-only attempt produced no report text — the report IS the "
                "deliverable of a worker-only dispatch, so the task was not marked "
                "done. Retry, or dispatch with an evaluator if the deliverable is "
                "a code change.",
                files_changed,
                False,
            )
        return (True, worker_id, None, worker_summary, files_changed, False)

    # Run Evaluator.
    eval_id = f"eval-{uuid.uuid4().hex[:8]}"
    await register_agent_thread(
        root,
        project_id,
        epic_id,
        thread_id=eval_id,
        role="evaluator",
        repo=repo_name,
        task_id=task.id,
        parent_thread_id=worker_id if worker_id is not None else ctx_d.manager_thread_id,
    )

    # worker_id is "" for an evaluator-only attempt (no Worker ran).
    ctx_d.pub(
        EvaluatorStartedEvent(
            project_id=project_id,
            epic_id=epic_id,
            run_id=run_id,
            eval_id=eval_id,
            worker_id=worker_id or "",
            task_id=task.id,
            repo=repo_name,
        )
    )

    # Resolve evaluator profile (separate resolution — evaluator may have its own profile).
    resolved_eval_profile = _resolve_profile(root, project_id, task, expected_role="evaluator")

    # Build a dedicated AgentContext for the Evaluator.  The same worktree_path
    # is used — path_guard / git escape guard / gitignore are all anchored to the
    # worktree, satisfying the sandbox invariant.  Command permissions come solely
    # from the repo-level allow/deny list (fetched above for the Worker; reused
    # here to avoid a second get_repo call).  Fresh copies so the two AgentContexts
    # never alias the same list.
    eval_allow, eval_deny = list(repo_allow), list(repo_deny)
    eval_ctx = await AgentContext.create(
        project_id=project_id,
        epic_id=epic_id,
        repo_name=repo_name,
        worktree_path=worktree_path,
        workspace_root=root,
        allow=eval_allow,
        deny=eval_deny,
    )

    verdict = await ctx_d.hooks.run_evaluator(
        project_id=project_id,
        epic_id=epic_id,
        run_id=run_id,
        eval_id=eval_id,
        task=task,
        ctx=eval_ctx,
        worker_id=worker_id or "",
        resolved_profile=resolved_eval_profile,
    )

    accepted = verdict.get("accepted", False)
    feedback_out: str = verdict.get("feedback", "")

    # Host commit on accept.  Commit exactly what the Evaluator evaluated — i.e.
    # what the post-Worker `git add -A` staged and what read_diff(--cached) showed.
    # We deliberately do NOT re-stage here: the Evaluator's run_tests may have
    # produced untracked artifacts (__pycache__, .pytest_cache, .coverage, build
    # output) and a second `git add -A` would bake them into the task commit.
    if accepted:
        # Are there staged changes to commit?  (--quiet --exit-code: rc 0 = none.)
        staged = await run_git(
            "diff", "--cached", "--quiet", cwd=worktree_path, check=False
        )
        if staged.returncode == 0:
            # Worker produced no staged changes — nothing to commit.
            logger.info("host commit skipped for task %s: no staged changes", task.id)
        else:
            subject = f"{task.id}: {task.title}"
            # Evaluator-only attempts have no worker summary — use the
            # evaluator's verdict feedback so the commit body is not empty.
            body = worker_summary or feedback_out
            commit_res = await run_git(
                "commit",
                "-m",
                subject,
                "-m",
                body,
                cwd=worktree_path,
                env=git_author_env(ctx_d.git_author_name, ctx_d.git_author_email),
                check=False,
            )
            if commit_res.returncode != 0:
                # Real commit failure: do NOT mark the task done — the staged
                # (accepted) work would otherwise be silently discarded by the
                # next attempt's `git reset --hard HEAD`.  Reject so it is retried.
                logger.error(
                    "host commit failed for task %s rc=%s: %s",
                    task.id,
                    commit_res.returncode,
                    commit_res.stderr,
                )
                accepted = False
                feedback_out = (
                    f"host commit failed (rc={commit_res.returncode}); "
                    "the change was not persisted and will be retried."
                )

    ctx_d.pub(
        EvalResultEvent(
            project_id=project_id,
            epic_id=epic_id,
            run_id=run_id,
            worker_id=worker_id or "",
            eval_id=eval_id,
            accepted=accepted,
            feedback=feedback_out,
        )
    )

    return (accepted, worker_id, eval_id, feedback_out, files_changed, False)
