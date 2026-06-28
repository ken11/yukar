"""ArbiterRunner — batch merge of selected epics into main (Feature 2).

Design
------
``ArbiterRunner`` implements ``RunnerProtocol`` and is registered under the
sentinel key ``(project_id, MERGE_SENTINEL)`` in the supervisor so that only
one batch-merge may run per project at a time.

The ``epic_id`` parameter passed to ``start()`` by the supervisor is always
``MERGE_SENTINEL`` (not a real epic).  The runner iterates ``self._epic_ids``
and processes each real epic independently:

For each epic IN ORDER, for each repo in ``epic.touched_repos``:
  1. ``ensure_worktree``
  2. ``start_conflict_merge(worktree, default_branch)`` — reverse-merge latest
     main INTO the epic worktree so cross-epic conflicts surface here.
  3. If conflicts: run ONE sandboxed arbiter agent to resolve markers + commit.
     Validate ``list_unmerged_files()==[]`` AND ``not merge_in_progress()``.
     If still broken → ``abort_merge``, mark epic failed, skip its remaining
     repos.
  4. Before forward-merge: verify the main checkout is on the right branch and
     clean (never disturb the user's real working tree).
  5. Forward-merge: ``git/diff.py merge(repo_path, branch=epic.branch, …)``
     (epic→main).  ``MergeConflictError`` (shouldn't happen) and
     ``GitVettingError`` are treated as per-epic failure (not 500).
  6. After all repos succeed → ``epic.status="merged"`` via ``save_epic``.

On any per-epic failure: record the result, CONTINUE with the next epic.
At the end, publish final ``EpicMergeProgressEvent(phase="finished")``.

Events
------
- Per-epic run events on the REAL epic channel (RunStarted/Completed/Failed,
  WorkerStarted/Completed) so the per-epic UI shows activity.
- Project-level progress via ``EpicMergeProgressEvent`` (lifecycle type → fans
  out via ``_project_queues`` to ``GET /api/projects/{p}/events``).

Invariants
----------
- No ``FileSessionManager`` on the arbiter agent (spec §6.4).
- Agent sandbox is confined to its assigned worktree via ``AgentContext``.
- Single semaphore slot (serial: arbiter is never parallel with other runs).
- YAML writes go through ``save_epic`` / ``save_state`` (storage layer).
- ``subprocess`` only via ``run_git`` / ``create_subprocess_exec`` (the git
  tools already use these; nothing new here).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from yukar.agents.context import AgentContext
from yukar.agents.dispatch import register_agent_thread
from yukar.agents.prompts import _ARBITER_SYSTEM_PROMPT, _build_arbiter_prompt
from yukar.config import paths as p
from yukar.config.settings import LLMSettings
from yukar.events import bus as event_bus
from yukar.git.diff import GitVettingError, MergeConflictError, merge
from yukar.git.resolve import (
    abort_merge,
    list_unmerged_files,
    merge_in_progress,
    start_conflict_merge,
)
from yukar.git.runner import git_author_env, run_git
from yukar.git.worktree import ensure_worktree
from yukar.llm.factory import create_model
from yukar.models.epic import Epic
from yukar.models.events import (
    EpicMergeProgressEvent,
    EpicMergeResult,
    EpicStatusChangedEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunStartedEvent,
    WorkerCompletedEvent,
    WorkerStartedEvent,
)
from yukar.models.run import RunState
from yukar.runs.common import run_single_sandbox_agent, save_and_publish_state
from yukar.storage import state_repo
from yukar.storage.epic_repo import get_epic, save_epic
from yukar.storage.project_repo import get_repo
from yukar.usage.tracker import ARBITER_EPIC_SENTINEL

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ArbiterRunner
# ---------------------------------------------------------------------------


class ArbiterRunner:
    """RunnerProtocol implementation for multi-epic batch merge.

    One instance handles an entire batch merge for a project.  It is
    registered under the sentinel epic key ``MERGE_SENTINEL`` in the supervisor
    so at most one batch-merge runs per project.
    """

    def __init__(
        self,
        llm_settings: LLMSettings,
        epic_ids: list[str],
        git_author_name: str = "yukar",
        git_author_email: str = "yukar@localhost",
    ) -> None:
        self._llm = llm_settings
        self._epic_ids = list(epic_ids)
        self._git_author_name = git_author_name
        self._git_author_email = git_author_email
        self._stopped: bool = False

    # ------------------------------------------------------------------
    # RunnerProtocol interface
    # ------------------------------------------------------------------

    async def start(
        self,
        root: str,
        project_id: str,
        epic_id: str,  # always MERGE_SENTINEL — ignored
        run_id: str,
    ) -> None:
        """Iterate over selected epics, merging each into main in order."""
        total = len(self._epic_ids)
        completed = 0
        results: list[EpicMergeResult] = []

        # Publish initial progress on the project stream.
        # epic_id="" because we are not yet processing any specific epic.
        event_bus.publish(
            project_id,
            "",
            EpicMergeProgressEvent(
                project_id=project_id,
                epic_id="",
                run_id=run_id,
                total=total,
                completed=0,
                current_epic_id=None,
                phase="started",
                results=[],
            ),
        )

        for _current_idx, real_epic_id in enumerate(self._epic_ids):
            if self._stopped:
                # Remaining epics are skipped.  Use the enumerate index to
                # slice from the current position, not list.index() which
                # returns the *first* occurrence and over-counts when the same
                # epic_id appears more than once in the batch.
                for remaining_id in self._epic_ids[_current_idx:]:
                    results.append(
                        EpicMergeResult(
                            epic_id=remaining_id,
                            status="skipped",
                            detail="batch stopped by user",
                        )
                    )
                break

            event_bus.publish(
                project_id,
                real_epic_id,
                EpicMergeProgressEvent(
                    project_id=project_id,
                    epic_id=real_epic_id,
                    run_id=run_id,
                    total=total,
                    completed=completed,
                    current_epic_id=real_epic_id,
                    phase="started",
                    results=list(results),
                ),
            )

            result = await self._process_epic(
                root=root,
                project_id=project_id,
                real_epic_id=real_epic_id,
                run_id=run_id,
                completed_so_far=completed,
                results_so_far=list(results),
            )
            results.append(result)
            if result.status == "merged":
                completed += 1

            event_bus.publish(
                project_id,
                real_epic_id,
                EpicMergeProgressEvent(
                    project_id=project_id,
                    epic_id=real_epic_id,
                    run_id=run_id,
                    total=total,
                    completed=completed,
                    current_epic_id=real_epic_id,
                    phase="epic_done",
                    results=list(results),
                ),
            )
            # Close per-epic SSE stream after epic is done.
            event_bus.publish(project_id, real_epic_id, None)

        # Final project-level progress event.
        event_bus.publish(
            project_id,
            "",
            EpicMergeProgressEvent(
                project_id=project_id,
                epic_id="",
                run_id=run_id,
                total=total,
                completed=completed,
                current_epic_id=None,
                phase="finished",
                results=list(results),
            ),
        )

    async def pause(self) -> None:
        pass  # Batch merge does not support pause

    async def resume(self) -> None:
        pass

    async def stop(self) -> None:
        """Request cooperative cancellation of the batch merge."""
        self._stopped = True

    # ------------------------------------------------------------------
    # Per-epic logic
    # ------------------------------------------------------------------

    async def _process_epic(
        self,
        root: str,
        project_id: str,
        real_epic_id: str,
        run_id: str,
        completed_so_far: int = 0,
        results_so_far: list[EpicMergeResult] | None = None,
    ) -> EpicMergeResult:
        """Process a single epic: resolve + forward-merge all its repos.

        ``completed_so_far`` and ``results_so_far`` are threaded in from
        ``start()`` so that intermediate progress events (``resolving``,
        ``merging``) carry the real running counts instead of hard-coded zeros,
        preventing the frontend panel from flickering to 0% mid-batch.

        Returns an ``EpicMergeResult`` with ``status`` and ``detail``.
        Never raises — all exceptions are caught and converted to a result.
        """
        _results_so_far: list[EpicMergeResult] = list(results_so_far) if results_so_far else []

        def pub_epic(event: object) -> None:
            event_bus.publish(project_id, real_epic_id, event)

        # ----------------------------------------------------------------
        # Load the epic.
        # ----------------------------------------------------------------
        epic = await get_epic(root, project_id, real_epic_id)
        if epic is None:
            return EpicMergeResult(
                epic_id=real_epic_id,
                status="skipped",
                detail="epic not found",
            )

        if not epic.branch:
            return EpicMergeResult(
                epic_id=real_epic_id,
                status="skipped",
                detail="epic has no branch",
            )

        if not epic.touched_repos:
            return EpicMergeResult(
                epic_id=real_epic_id,
                status="skipped",
                detail="epic has no touched_repos",
            )

        # ----------------------------------------------------------------
        # Initialise per-epic run state.
        # ----------------------------------------------------------------
        state = RunState(
            run_id=run_id,
            status="running",
            started_at=datetime.now(UTC),
        )
        await state_repo.save_state(root, project_id, real_epic_id, state)

        pub_epic(
            RunStartedEvent(
                project_id=project_id,
                epic_id=real_epic_id,
                run_id=run_id,
            )
        )

        merged_repos: list[str] = []
        # Track the currently-open worktree so `finally` can abort a partial merge.
        current_worktree: Path | None = None
        # Track the main checkout being forward-merged so `finally` can abort
        # it on unexpected failures (disk error, interrupt, etc.) that leave
        # MERGE_HEAD on the real main branch.  Set just before merge() is called
        # and cleared immediately after a successful merge (or on paths where
        # merge() already cleaned up itself: MergeConflictError / GitVettingError).
        current_main_repo: Path | None = None

        # ------------------------------------------------------------------
        # Local helpers — capture (root, project_id, real_epic_id, state,
        # run_id, pub_epic) so each call site is a one-liner.
        # ------------------------------------------------------------------

        async def _save_and_publish_failure(error_msg: str) -> None:
            """Set state.status="error", persist, and publish RunFailedEvent."""
            await save_and_publish_state(
                root,
                project_id,
                real_epic_id,
                state,
                "error",
                RunFailedEvent(
                    project_id=project_id,
                    epic_id=real_epic_id,
                    run_id=run_id,
                    error=error_msg,
                ),
                pub_epic,
            )

        async def _cleanup_worktree_safely(wt_path: Path, label: str) -> None:
            """Best-effort abort of any in-progress merge in *wt_path*."""
            try:
                still_in_progress = await merge_in_progress(wt_path)
                if still_in_progress:
                    logger.warning(
                        "ArbiterRunner finally: merge still in progress for %s; aborting.",
                        label,
                    )
                    await abort_merge(wt_path)
            except Exception:
                logger.exception("ArbiterRunner finally: error during abort_merge for %s", label)

        try:
            for repo_name in epic.touched_repos:
                if self._stopped:
                    break

                repo_obj = await get_repo(root, project_id, repo_name)
                if repo_obj is None:
                    logger.warning(
                        "Arbiter: repo %r not found in project %r — skipping",
                        repo_name,
                        project_id,
                    )
                    continue

                repo_path = Path(repo_obj.path)
                # Resolve the active manager trial's worktree id.
                # Ghost-worktree guard is centralised in agents.trials.resolve_active_trial_id.
                from yukar.agents.trials import resolve_active_trial_id

                active_trial_id: str | None = await resolve_active_trial_id(
                    root, project_id, real_epic_id, epic
                )
                if active_trial_id is None:
                    # All trials have been archived; no active trial to merge.
                    detail = (
                        "Epic has no active manager trial (all trials are archived). "
                        "Create a new trial before merging."
                    )
                    logger.warning(
                        "Arbiter: %s for epic %s", detail, real_epic_id
                    )
                    await _save_and_publish_failure(detail)
                    return EpicMergeResult(
                        epic_id=real_epic_id,
                        status="skipped",
                        detail=detail,
                    )

                worktree_path = p.worktree_dir(
                    root, project_id, real_epic_id, active_trial_id, repo_name
                )
                default_branch = repo_obj.default_branch

                current_worktree = worktree_path

                # 1. Ensure worktree.
                await ensure_worktree(
                    repo_path=repo_path,
                    worktree_path=worktree_path,
                    branch=epic.branch,
                    default_branch=default_branch,
                )

                # 2. Reverse merge: pull latest main into the epic worktree.
                event_bus.publish(
                    project_id,
                    real_epic_id,
                    EpicMergeProgressEvent(
                        project_id=project_id,
                        epic_id=real_epic_id,
                        run_id=run_id,
                        total=len(self._epic_ids),
                        completed=completed_so_far,
                        current_epic_id=real_epic_id,
                        phase="resolving",
                        results=list(_results_so_far),
                    ),
                )

                try:
                    conflict_files = await start_conflict_merge(
                        worktree_path=worktree_path,
                        default_branch=default_branch,
                        env=git_author_env(self._git_author_name, self._git_author_email),
                    )
                except GitVettingError as exc:
                    detail = f"vetting_refused (reverse merge) for {repo_name}: {exc}"
                    logger.warning("Arbiter: %s for epic %s", detail, real_epic_id)
                    await _save_and_publish_failure(detail)
                    return EpicMergeResult(
                        epic_id=real_epic_id,
                        status="vetting_refused",
                        detail=detail,
                        repos=merged_repos,
                    )

                if conflict_files:
                    if self._stopped:
                        await abort_merge(worktree_path)
                        current_worktree = None
                        await _save_and_publish_failure("stopped during conflict resolution")
                        return EpicMergeResult(
                            epic_id=real_epic_id,
                            status="error",
                            detail="stopped during conflict resolution",
                        )

                    # 3. Run the arbiter agent to resolve conflicts.
                    arbiter_id = f"arbiter-{uuid.uuid4().hex[:8]}"

                    await register_agent_thread(
                        root,
                        project_id,
                        real_epic_id,
                        thread_id=arbiter_id,
                        role="arbiter",
                        repo=repo_name,
                        title=f"Arbiter {arbiter_id}",
                    )

                    pub_epic(
                        WorkerStartedEvent(
                            project_id=project_id,
                            epic_id=real_epic_id,
                            run_id=run_id,
                            worker_id=arbiter_id,
                            task_id=None,
                            repo=repo_name,
                        )
                    )

                    allow_cmds = list(repo_obj.commands.allow)
                    deny_cmds = list(repo_obj.commands.deny)

                    ctx = await AgentContext.create(
                        project_id=project_id,
                        epic_id=real_epic_id,
                        repo_name=repo_name,
                        worktree_path=worktree_path,
                        workspace_root=root,
                        allow=allow_cmds,
                        deny=deny_cmds,
                    )

                    await self._run_arbiter_agent(
                        project_id=project_id,
                        epic_id=real_epic_id,
                        run_id=run_id,
                        arbiter_id=arbiter_id,
                        epic=epic,
                        conflict_files=conflict_files,
                        ctx=ctx,
                    )

                    pub_epic(
                        WorkerCompletedEvent(
                            project_id=project_id,
                            epic_id=real_epic_id,
                            run_id=run_id,
                            worker_id=arbiter_id,
                            task_id=None,
                            repo=repo_name,
                        )
                    )

                    # Validate resolution.
                    remaining = await list_unmerged_files(worktree_path)
                    still_in_progress = await merge_in_progress(worktree_path)

                    if remaining or still_in_progress:
                        logger.warning(
                            "Arbiter: agent did not fully resolve conflicts for epic %s repo %s; "
                            "unmerged=%s, merge_in_progress=%s. Aborting.",
                            real_epic_id,
                            repo_name,
                            remaining,
                            still_in_progress,
                        )
                        await abort_merge(worktree_path)
                        current_worktree = None
                        await _save_and_publish_failure(
                            f"Conflicts not fully resolved: {remaining}"
                        )
                        return EpicMergeResult(
                            epic_id=real_epic_id,
                            status="conflict_unresolved",
                            detail=f"Conflicts not fully resolved in {repo_name}: {remaining}",
                            repos=merged_repos,
                        )

                # Merge succeeded (or clean merge) — clear tracked worktree.
                current_worktree = None

                # 4. Verify main checkout is on the correct branch and clean.
                event_bus.publish(
                    project_id,
                    real_epic_id,
                    EpicMergeProgressEvent(
                        project_id=project_id,
                        epic_id=real_epic_id,
                        run_id=run_id,
                        total=len(self._epic_ids),
                        completed=completed_so_far,
                        current_epic_id=real_epic_id,
                        phase="merging",
                        results=list(_results_so_far),
                    ),
                )

                head_result = await run_git(
                    "rev-parse",
                    "--abbrev-ref",
                    "HEAD",
                    cwd=repo_path,
                    check=False,
                )
                actual_branch = head_result.stdout.strip()
                if actual_branch != default_branch:
                    detail = (
                        f"main checkout not on {default_branch!r} (got {actual_branch!r}) "
                        "— skipped forward merge"
                    )
                    logger.warning(
                        "Arbiter: %s for repo %s epic %s", detail, repo_name, real_epic_id
                    )
                    await _save_and_publish_failure(detail)
                    return EpicMergeResult(
                        epic_id=real_epic_id,
                        status="error",
                        detail=detail,
                        repos=merged_repos,
                    )

                status_result = await run_git(
                    "status",
                    "--porcelain",
                    cwd=repo_path,
                    check=False,
                )
                if status_result.stdout.strip():
                    detail = (
                        f"main checkout is not clean for repo {repo_name!r} — skipped forward merge"
                    )
                    logger.warning("Arbiter: %s for epic %s", detail, real_epic_id)
                    await _save_and_publish_failure(detail)
                    return EpicMergeResult(
                        epic_id=real_epic_id,
                        status="error",
                        detail=detail,
                        repos=merged_repos,
                    )

                # 5. Forward merge: epic branch → main.
                # Set current_main_repo so finally can abort if this merge is
                # interrupted by an unexpected error leaving MERGE_HEAD on main.
                current_main_repo = repo_path
                try:
                    await merge(
                        repo_path=repo_path,
                        branch=epic.branch,
                        message=f"Merge epic {real_epic_id} '{epic.title}' into {default_branch}",
                        author_name=self._git_author_name,
                        author_email=self._git_author_email,
                    )
                except MergeConflictError as exc:
                    # merge() already ran git merge --abort; main is clean.
                    current_main_repo = None
                    detail = (
                        f"Unexpected conflict during forward merge of {repo_name}: {exc.conflicts}"
                    )
                    logger.error("Arbiter: %s for epic %s", detail, real_epic_id)
                    await _save_and_publish_failure(detail)
                    return EpicMergeResult(
                        epic_id=real_epic_id,
                        status="conflict_unresolved",
                        detail=detail,
                        repos=merged_repos,
                    )
                except GitVettingError as exc:
                    # Vetting ran before any merge attempt; main is untouched.
                    current_main_repo = None
                    detail = f"vetting_refused for {repo_name}: {exc}"
                    logger.warning("Arbiter: %s for epic %s", detail, real_epic_id)
                    await _save_and_publish_failure(detail)
                    return EpicMergeResult(
                        epic_id=real_epic_id,
                        status="vetting_refused",
                        detail=detail,
                        repos=merged_repos,
                    )

                # Merge succeeded; main is no longer mid-merge.
                current_main_repo = None
                merged_repos.append(repo_name)

            # ----------------------------------------------------------------
            # All repos succeeded (or no repos/already stopped).
            # ----------------------------------------------------------------
            if self._stopped:
                await _save_and_publish_failure("batch stopped by user")
                return EpicMergeResult(
                    epic_id=real_epic_id,
                    status="error",
                    detail="batch stopped by user",
                    repos=merged_repos,
                )

            # Mark epic as merged.
            epic.status = "merged"
            await save_epic(root, project_id, epic)

            await save_and_publish_state(
                root,
                project_id,
                real_epic_id,
                state,
                "completed",
                EpicStatusChangedEvent(
                    project_id=project_id,
                    epic_id=real_epic_id,
                    run_id=run_id,
                    status="merged",
                ),
                pub_epic,
            )
            pub_epic(
                RunCompletedEvent(
                    project_id=project_id,
                    epic_id=real_epic_id,
                    run_id=run_id,
                )
            )

            return EpicMergeResult(
                epic_id=real_epic_id,
                status="merged",
                detail="",
                repos=merged_repos,
            )

        except Exception as exc:
            logger.exception("ArbiterRunner: error processing epic %s", real_epic_id)
            await _save_and_publish_failure(str(exc))
            return EpicMergeResult(
                epic_id=real_epic_id,
                status="error",
                detail=str(exc),
                repos=merged_repos,
            )

        finally:
            # Best-effort cleanup: abort any in-progress merge in the worktree.
            if current_worktree is not None:
                await _cleanup_worktree_safely(
                    current_worktree, f"worktree for epic {real_epic_id}"
                )
            # Best-effort cleanup: if the forward merge was interrupted mid-flight
            # (e.g. disk error, CancelledError), the main checkout may have been
            # left with MERGE_HEAD.  Abort it so the user's working tree is clean.
            if current_main_repo is not None:
                await _cleanup_worktree_safely(
                    current_main_repo,
                    f"main checkout for epic {real_epic_id}",
                )

    # ------------------------------------------------------------------
    # Arbiter agent
    # ------------------------------------------------------------------

    async def _run_arbiter_agent(
        self,
        project_id: str,
        epic_id: str,
        run_id: str,
        arbiter_id: str,
        epic: Epic,
        conflict_files: list[str],
        ctx: AgentContext,
    ) -> None:
        """Run a single arbiter agent to resolve conflict markers in the worktree.

        Usage is recorded under ``ARBITER_EPIC_SENTINEL`` so that arbiter costs
        appear in a dedicated bucket rather than inflating the real epic's cost.
        All other references (StreamTranslator, session_store, final-message append)
        use the real *epic_id* so that agent-visibility events appear in the
        correct per-epic UI channel.
        """
        model = create_model(self._llm, role="arbiter")
        prompt = _build_arbiter_prompt(epic, conflict_files, ctx.worktree_path)
        await run_single_sandbox_agent(
            ctx=ctx,
            model=model,
            agent_id=arbiter_id,
            role="arbiter",
            system_prompt=_ARBITER_SYSTEM_PROMPT,
            prompt=prompt,
            project_id=project_id,
            epic_id=epic_id,
            run_id=run_id,
            usage_epic_id=ARBITER_EPIC_SENTINEL,
            git_author_name=self._git_author_name,
            git_author_email=self._git_author_email,
            is_stopped=lambda: self._stopped,
        )
