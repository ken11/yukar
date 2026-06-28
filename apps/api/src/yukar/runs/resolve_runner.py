"""ResolveRunner — agent-assisted conflict resolution run (spec §5.2).

Design
------
When ``POST /git/resolve`` is called after a merge conflict, the supervisor
creates a ResolveRunner for the (project, epic, repo) triple.  The runner:

1. Ensures the epic worktree exists for the given repo.
2. Calls ``start_conflict_merge(worktree, default_branch)`` to merge the
   default branch INTO the worktree branch (reverse direction), leaving
   conflict markers in the worktree where the sandboxed agent can touch them.
3. If no conflicts materialise (clean merge), publishes RunCompleted and exits.
4. Builds an ``AgentContext`` (worktree sandbox) and runs a single Worker-style
   resolve agent with fs/cmd/git tools and a conflict-resolution prompt.
5. After the agent finishes, validates the worktree: ``list_unmerged_files``
   must be empty and ``merge_in_progress`` must be False.  If still broken,
   calls ``abort_merge`` and publishes RunFailedEvent.
6. Publishes events using the same types as the regular EpicOrchestrator so the
   SSE stream and UI need no special handling.

Invariants maintained
---------------------
- Worker has no FileSessionManager (invariant §6.4).
- epic.yaml.status is NOT touched (resolve run is orthogonal to epic lifecycle).
- state.yaml is written so the SSE /runs endpoint can show progress.
- Path-guard sandbox is enforced via AgentContext (worktree outside access
  blocked structurally).
- asyncio.CancelledError propagates naturally for supervisor.stop().

Conversation persistence
------------------------
The resolve run has no orchestrator, so there is no full snapshot to Strands sessions/
(preserving the FileSessionManager ownership model of §6.4). Instead, only the final
message is persisted via session_store.append_message as an index + final message entry.
This is intentional design; use a normal epic run when a full conversation history is needed.

Cooperative cancellation (stop)
--------------------------------
``stop()`` sets the ``_stopped`` flag. This flag is checked after each turn in the
``stream_async`` loop inside ``_run_resolve_agent``; if set, the loop exits early.
``start()`` then detects any in-progress merge state and calls ``abort_merge`` to restore
the worktree (shared with the validation → abort path).
This coexists with the supervisor's 5-second timeout fallback to Task.cancel(), and
CancelledError propagates naturally.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from yukar.agents.context import AgentContext
from yukar.agents.dispatch import register_agent_thread
from yukar.agents.prompts import _RESOLVE_SYSTEM_PROMPT, _build_resolve_prompt
from yukar.config import paths as p
from yukar.config.settings import LLMSettings
from yukar.events import bus as event_bus
from yukar.git.resolve import (
    abort_merge,
    list_unmerged_files,
    merge_in_progress,
    start_conflict_merge,
)
from yukar.git.runner import git_author_env
from yukar.git.worktree import ensure_worktree
from yukar.llm.factory import create_model
from yukar.models.events import (
    RunCompletedEvent,
    RunFailedEvent,
    RunStartedEvent,
    WorkerCompletedEvent,
    WorkerStartedEvent,
)
from yukar.models.run import RunState
from yukar.runs.common import run_single_sandbox_agent, save_and_publish_state
from yukar.storage import state_repo
from yukar.storage.epic_repo import get_epic
from yukar.storage.project_repo import get_repo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ResolveRunner
# ---------------------------------------------------------------------------


class ResolveRunner:
    """RunnerProtocol implementation for agent-assisted conflict resolution.

    One instance handles a single (project_id, epic_id, repo_name) resolve run.
    """

    def __init__(
        self,
        llm_settings: LLMSettings,
        repo_name: str,
        git_author_name: str = "yukar",
        git_author_email: str = "yukar@localhost",
    ) -> None:
        self._llm = llm_settings
        self._repo_name = repo_name
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
        epic_id: str,
        run_id: str,
    ) -> None:
        """Run the conflict resolution to completion."""

        def pub(event: object) -> None:
            event_bus.publish(project_id, epic_id, event)

        state = RunState(
            run_id=run_id,
            status="running",
            started_at=datetime.now(UTC),
        )
        await state_repo.save_state(root, project_id, epic_id, state)

        # worktree_path is set inside the try block; we keep a reference here so
        # that the finally clause can abort any in-progress merge even when
        # CancelledError is raised before the validation path executes.
        worktree_path: Path | None = None

        try:
            pub(RunStartedEvent(project_id=project_id, epic_id=epic_id, run_id=run_id))

            epic = await get_epic(root, project_id, epic_id)
            if epic is None:
                raise RuntimeError(f"Epic not found: {epic_id}")

            repo_obj = await get_repo(root, project_id, self._repo_name)
            if repo_obj is None:
                raise RuntimeError(f"Repo not found: {self._repo_name}")

            repo_path = Path(repo_obj.path)
            # Resolve the active manager trial's worktree id.
            # Ghost-worktree guard is centralised in agents.trials.resolve_active_trial_id.
            from yukar.agents.trials import resolve_active_trial_id

            _resolved = await resolve_active_trial_id(root, project_id, epic_id, epic)
            if _resolved is None:
                raise RuntimeError(
                    "Epic has no active manager trial (all trials are archived). "
                    "Create a new trial before starting a resolve run."
                )
            active_trial_id: str = _resolved

            worktree_path = p.worktree_dir(
                root, project_id, epic_id, active_trial_id, self._repo_name
            )
            default_branch = repo_obj.default_branch

            # Ensure worktree exists (idempotent).
            await ensure_worktree(
                repo_path=repo_path,
                worktree_path=worktree_path,
                branch=epic.branch,
                default_branch=default_branch,
            )

            # Attempt to merge default_branch into the worktree branch.
            conflict_files = await start_conflict_merge(
                worktree_path=worktree_path,
                default_branch=default_branch,
                env=git_author_env(self._git_author_name, self._git_author_email),
            )

            if not conflict_files:
                # Clean merge — nothing left for the agent to do.
                logger.info("Resolve run %s: no conflicts after merge, completing cleanly.", run_id)
                await save_and_publish_state(
                    root,
                    project_id,
                    epic_id,
                    state,
                    "completed",
                    RunCompletedEvent(project_id=project_id, epic_id=epic_id, run_id=run_id),
                    pub,
                )
                return

            # Build agent context (sandboxed to worktree).
            allow_cmds = list(repo_obj.commands.allow)
            deny_cmds = list(repo_obj.commands.deny)

            ctx = await AgentContext.create(
                project_id=project_id,
                epic_id=epic_id,
                repo_name=self._repo_name,
                worktree_path=worktree_path,
                workspace_root=root,
                allow=allow_cmds,
                deny=deny_cmds,
            )

            # Register the resolver thread in threads.yaml index.
            resolver_id = f"resolver-{uuid.uuid4().hex[:8]}"
            await register_agent_thread(
                root,
                project_id,
                epic_id,
                thread_id=resolver_id,
                role="worker",
                repo=self._repo_name,
                title=f"Conflict Resolver {resolver_id}",
            )

            pub(
                WorkerStartedEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                    worker_id=resolver_id,
                    task_id=None,
                    repo=self._repo_name,
                )
            )

            # Run the resolve agent.
            await self._run_resolve_agent(
                project_id=project_id,
                epic_id=epic_id,
                run_id=run_id,
                resolver_id=resolver_id,
                conflict_files=conflict_files,
                ctx=ctx,
            )

            pub(
                WorkerCompletedEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                    worker_id=resolver_id,
                    task_id=None,
                    repo=self._repo_name,
                )
            )

            # Post-resolution validation.
            remaining = await list_unmerged_files(worktree_path)
            still_in_progress = await merge_in_progress(worktree_path)

            if remaining or still_in_progress:
                logger.warning(
                    "Resolve run %s: agent did not fully resolve conflicts; "
                    "unmerged=%s, merge_in_progress=%s. Aborting.",
                    run_id,
                    remaining,
                    still_in_progress,
                )
                await abort_merge(worktree_path)
                await save_and_publish_state(
                    root,
                    project_id,
                    epic_id,
                    state,
                    "error",
                    RunFailedEvent(
                        project_id=project_id,
                        epic_id=epic_id,
                        run_id=run_id,
                        error=f"Conflicts not fully resolved: {remaining}",
                    ),
                    pub,
                )
                return

            await save_and_publish_state(
                root,
                project_id,
                epic_id,
                state,
                "completed",
                RunCompletedEvent(project_id=project_id, epic_id=epic_id, run_id=run_id),
                pub,
            )

        except Exception as exc:
            logger.exception("ResolveRunner error for epic %s", epic_id)
            await save_and_publish_state(
                root,
                project_id,
                epic_id,
                state,
                "error",
                RunFailedEvent(
                    project_id=project_id,
                    epic_id=epic_id,
                    run_id=run_id,
                    error=str(exc),
                ),
                pub,
            )
            raise

        finally:
            # Hard-cancel path: CancelledError is a BaseException subclass and
            # bypasses the `except Exception` block above.  We must ensure the
            # worktree is clean even in that case, so we check for an in-progress
            # merge here.  We only abort if the agent has NOT already committed
            # (i.e. merge is still in progress) to avoid corrupting a successful
            # clean merge.
            if worktree_path is not None:
                try:
                    still_in_progress = await merge_in_progress(worktree_path)
                    if still_in_progress:
                        logger.warning(
                            "ResolveRunner finally: merge still in progress for run %s; aborting.",
                            run_id,
                        )
                        await abort_merge(worktree_path)
                except Exception:
                    # Best-effort cleanup — never let a secondary error mask the
                    # original exception (CancelledError or otherwise).
                    logger.exception(
                        "ResolveRunner finally: error during abort_merge cleanup for run %s",
                        run_id,
                    )

            # Sentinel closes all SSE streams for this (project, epic).
            event_bus.publish(project_id, epic_id, None)

    async def pause(self) -> None:
        pass  # Resolve run does not support pause

    async def resume(self) -> None:
        pass

    async def stop(self) -> None:
        """Request cooperative cancellation of the resolve run.

        Sets ``_stopped = True`` so that the ``stream_async`` loop in
        ``_run_resolve_agent`` exits at the next agent turn boundary.  After
        the loop returns, ``start()`` detects any remaining merge state via
        ``list_unmerged_files`` / ``merge_in_progress`` and calls
        ``abort_merge`` to restore the worktree — the same abort path used
        when the agent fails to resolve all conflicts.

        If the task does not exit within the supervisor's 5-second window,
        ``asyncio.Task.cancel()`` is called as a hard fallback and
        CancelledError propagates naturally through ``stream_async``.
        """
        self._stopped = True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_resolve_agent(
        self,
        project_id: str,
        epic_id: str,
        run_id: str,
        resolver_id: str,
        conflict_files: list[str],
        ctx: AgentContext,
    ) -> None:
        """Run a single resolve agent that fixes conflict markers in the worktree."""
        model = create_model(self._llm, role="worker")
        prompt = _build_resolve_prompt(conflict_files, ctx.worktree_path)
        await run_single_sandbox_agent(
            ctx=ctx,
            model=model,
            agent_id=resolver_id,
            role="worker",
            system_prompt=_RESOLVE_SYSTEM_PROMPT,
            prompt=prompt,
            project_id=project_id,
            epic_id=epic_id,
            run_id=run_id,
            usage_epic_id=epic_id,
            git_author_name=self._git_author_name,
            git_author_email=self._git_author_email,
            is_stopped=lambda: self._stopped,
        )
