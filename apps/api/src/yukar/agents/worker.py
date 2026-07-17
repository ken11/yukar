"""Worker agent runner.

The ``run_worker`` coroutine executes a single Worker agent turn.  The caller
(EpicOrchestrator) is responsible for creating the model via ``create_model``
so that test patches on ``yukar.agents.orchestrator.create_model`` remain
effective.  The model is passed in as an argument.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from strands import Agent
from strands.types.agent import Limits

from yukar.agents.context import AgentContext
from yukar.agents.prompts import _WORKER_SYSTEM_PROMPT, _build_worker_prompt
from yukar.agents.streaming import AgentUsageRecorder, StreamTranslator, extract_final_text
from yukar.agents.tools.browser_tools import make_browser_tools_if_configured
from yukar.agents.tools.command import make_command_tools
from yukar.agents.tools.fs import make_fs_tools
from yukar.agents.tools.fs_edit import make_fs_edit_tools
from yukar.agents.tools.git_tools import make_git_tools
from yukar.agents.tools.grep_tools import make_grep_tools
from yukar.agents.tools.repo_tools import make_repo_tools
from yukar.config import paths
from yukar.models.task import Task
from yukar.preview.browser import SessionKey, get_browser_session_manager
from yukar.storage import session_store

logger = logging.getLogger(__name__)


# Alias kept so that ``from yukar.agents.worker import _extract_agent_final_text``
# and ``yukar.agents.orchestrator`` import still work (test patch point).
_extract_agent_final_text = extract_final_text


async def run_worker(
    *,
    project_id: str,
    epic_id: str,
    run_id: str,
    worker_id: str,
    task: Task,
    ctx: AgentContext,
    feedback: str,
    hitl_prefix: str,
    worker_model: Any,
    conversation_manager: Any | None,
    indexer_service: Any | None,
    git_author_name: str,
    git_author_email: str,
    max_turns: int = 60,
    max_total_tokens: int | None = None,
    extra_system_prompt: str = "",
    extra_tools: list[Any] | None = None,
    plugins: list[Any] | None = None,
    ensure_tree: Any = None,
) -> dict[str, Any]:
    """Run a Worker agent for one task attempt.

    Workers do NOT get a session_manager (invariant §6.4).
    We record their messages via append_message after the fact.

    Args:
        worker_model: Pre-built model object (created by orchestrator so that
            ``patch("yukar.agents.orchestrator.create_model", ...)`` works).
        conversation_manager: Optional conversation manager for history
            summarisation.  Created by the orchestrator (new instance per call)
            so that the ``_summary_message`` state is never shared.
    """
    translator = StreamTranslator(
        project_id=project_id,
        epic_id=epic_id,
        run_id=run_id,
        thread_id=worker_id,
    )

    fs_tools = make_fs_tools(ctx)
    fs_edit_tools = make_fs_edit_tools(ctx)
    cmd_tools = make_command_tools(ctx)
    git_tools_list = make_git_tools(ctx, git_author_name, git_author_email, include_commit=False)

    # Worker gets repo_search / repo_summarize scoped to its assigned repo only.
    worker_repo_tools: list[Any] = []
    if indexer_service is not None:
        worker_repo_tools = make_repo_tools(
            ctx.project_id,
            indexer_service,
            repo_name=ctx.repo_name,  # Worker: single repo, structurally enforced
        )

    worker_system_prompt = _WORKER_SYSTEM_PROMPT
    if extra_system_prompt:
        worker_system_prompt = worker_system_prompt + extra_system_prompt

    _extra = list(extra_tools) if extra_tools else []
    _plugins = list(plugins) if plugins else []

    grep_tools_list = make_grep_tools(ctx)

    # Browser verification bundle — present only when the assigned repo
    # declares a dev_server config (empty list otherwise).
    browser_tools_list = await make_browser_tools_if_configured(ctx, worker_id, ensure_tree)

    worker_agent = Agent(
        model=worker_model,
        agent_id=worker_id,
        conversation_manager=conversation_manager,
        tools=[
            *fs_tools,
            *fs_edit_tools,
            *cmd_tools,
            *git_tools_list,
            *grep_tools_list,
            *worker_repo_tools,
            *browser_tools_list,
            *_extra,
        ],
        callback_handler=translator.callback,
        system_prompt=worker_system_prompt,
        **({"plugins": _plugins} if _plugins else {}),  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    )
    usage_recorder = AgentUsageRecorder(
        project_id=project_id,
        epic_id=epic_id,
        run_id=run_id,
        role="worker",
    ).bind(worker_agent)

    prompt = _build_worker_prompt(task, Path(str(ctx.worktree_path)), feedback, hitl_prefix)

    limits: Limits = {"turns": max_turns}
    if max_total_tokens is not None:
        limits["total_tokens"] = max_total_tokens

    try:
        async for _ in worker_agent.stream_async(prompt, limits=limits):
            pass
    finally:
        # Close this worker's browser page (if it opened one) — the trial's
        # dev servers stay up for later attempts; run-end stops them.
        _browser_sessions = get_browser_session_manager()
        if browser_tools_list and _browser_sessions is not None:
            # CancelledError included: a cancel arriving during this shielded
            # close must not skip the flush/persist below (the shield lets the
            # close finish; asyncio re-delivers the cancel at the next await).
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.shield(
                    _browser_sessions.close(
                        SessionKey(
                            project_id=project_id,
                            epic_id=epic_id,
                            trial_id=paths.trial_id_of_worktree(ctx.worktree_path),
                            owner_id=worker_id,
                        )
                    )
                )
        await usage_recorder.flush()
        # Persist the Worker's full conversation (handoff prompt + tool-use
        # activity + final reply) so the thread retains its activity log on
        # reload — Workers have no FileSessionManager (§6.4), so we write
        # agent.messages verbatim here (issue③ + tool-log persistence).
        # In `finally` so a partial conversation is still kept if the run is
        # interrupted (e.g. token-limit exception) mid-stream.
        await session_store.persist_agent_messages(
            ctx.workspace_root,
            project_id,
            epic_id,
            worker_id,
            worker_agent.messages,
        )

    final_text = extract_final_text(worker_agent)
    return {"result": final_text}
