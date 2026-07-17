"""Evaluator agent runner.

The ``run_evaluator`` coroutine executes a single Evaluator agent turn.  The
caller (EpicOrchestrator) is responsible for creating the model via
``create_model`` so that test patches on
``yukar.agents.orchestrator.create_model`` remain effective.  The model is
passed in as an argument.
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
from yukar.agents.prompts import _EVALUATOR_SYSTEM_PROMPT, _build_evaluator_prompt
from yukar.agents.streaming import AgentUsageRecorder, StreamTranslator
from yukar.agents.tools.browser_tools import make_browser_tools_if_configured
from yukar.agents.tools.evaluator_tools import make_evaluator_tools
from yukar.agents.tools.grep_tools import make_grep_tools
from yukar.config import paths
from yukar.models.epic import Epic
from yukar.models.task import Task
from yukar.preview.browser import SessionKey, get_browser_session_manager
from yukar.storage import session_store

logger = logging.getLogger(__name__)


def _make_submit_verdict_tool(verdict_holder: list[dict[str, Any]]) -> Any:
    """Return a Strands tool that lets the Evaluator submit its verdict."""
    from strands import tool

    @tool
    def submit_verdict(accepted: bool, feedback: str = "") -> dict[str, Any]:
        """Submit the evaluation verdict for the Worker's implementation.

        Args:
            accepted: ``True`` if the implementation is acceptable.
            feedback: Required when ``accepted=False``; specific fix instructions.

        Returns:
            Confirmation dict.
        """
        verdict_holder[0] = {"accepted": accepted, "feedback": feedback}
        return {"recorded": True, "accepted": accepted}

    return submit_verdict


async def run_evaluator(
    *,
    project_id: str,
    epic_id: str,
    run_id: str,
    eval_id: str,
    task: Task,
    ctx: AgentContext,
    worker_id: str,
    eval_model: Any,
    conversation_manager: Any | None,
    epic: Epic | None = None,
    indexer_service: Any | None = None,
    max_turns: int = 20,
    max_total_tokens: int | None = None,
    extra_system_prompt: str = "",
    extra_tools: list[Any] | None = None,
    plugins: list[Any] | None = None,
    ensure_tree: Any = None,
) -> dict[str, Any]:
    """Run an Evaluator agent.

    Evaluators do NOT get a session_manager (invariant §6.4).
    The ``submit_verdict`` tool captures the structured verdict.

    Args:
        eval_model: Pre-built model object (created by orchestrator so that
            ``patch("yukar.agents.orchestrator.create_model", ...)`` works).
        conversation_manager: Optional conversation manager for history
            summarisation.  Created by the orchestrator (new instance per call)
            so that the ``_summary_message`` state is never shared.
        epic: The Epic being evaluated — used to inject acceptance_criteria into
            the evaluator prompt so it can make objective decisions.
        indexer_service: Optional IndexerService — if provided, the Evaluator
            gets repo_search / repo_summarize tools (spec audit F4).
    """
    from yukar.agents.tools.repo_tools import make_repo_tools

    verdict_holder: list[dict[str, Any]] = [{"accepted": True, "feedback": ""}]

    verdict_tool = _make_submit_verdict_tool(verdict_holder)
    eval_tools = make_evaluator_tools(ctx)

    # Evaluator gets repo_grep (always) for live worktree search (issue⑤).
    eval_tools = [*eval_tools, *make_grep_tools(ctx)]

    # Evaluator gets repo_search / repo_summarize if IndexerService is available (spec F4).
    if indexer_service is not None:
        eval_repo_tools = make_repo_tools(
            ctx.project_id,
            indexer_service,
            repo_name=ctx.repo_name,  # Evaluator: scoped to same repo as the Worker
        )
        eval_tools = [*eval_tools, *eval_repo_tools]

    # Browser verification bundle (only when the repo declares dev_server).
    # Interaction included by design: the Evaluator's read-only invariant is
    # about the repository, and browser use never writes the worktree.
    browser_tools_list = await make_browser_tools_if_configured(ctx, eval_id, ensure_tree)
    eval_tools = [*eval_tools, *browser_tools_list]

    translator = StreamTranslator(
        project_id=project_id,
        epic_id=epic_id,
        run_id=run_id,
        thread_id=eval_id,
    )

    eval_system_prompt = _EVALUATOR_SYSTEM_PROMPT
    if extra_system_prompt:
        eval_system_prompt = eval_system_prompt + extra_system_prompt

    _extra = list(extra_tools) if extra_tools else []
    _plugins = list(plugins) if plugins else []

    eval_agent = Agent(
        model=eval_model,
        agent_id=eval_id,
        conversation_manager=conversation_manager,
        tools=[*eval_tools, verdict_tool, *_extra],
        callback_handler=translator.callback,
        system_prompt=eval_system_prompt,
        **({"plugins": _plugins} if _plugins else {}),  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    )
    usage_recorder = AgentUsageRecorder(
        project_id=project_id,
        epic_id=epic_id,
        run_id=run_id,
        role="evaluator",
    ).bind(eval_agent)

    prompt = _build_evaluator_prompt(task, Path(str(ctx.worktree_path)), epic=epic)

    limits: Limits = {"turns": max_turns}
    if max_total_tokens is not None:
        limits["total_tokens"] = max_total_tokens

    try:
        async for _ in eval_agent.stream_async(prompt, limits=limits):
            pass
    finally:
        # Close this evaluator's browser page (if it opened one); the trial's
        # dev servers stay up for later attempts.
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
                            owner_id=eval_id,
                        )
                    )
                )
        await usage_recorder.flush()
        # Persist the Evaluator's full conversation (handoff prompt + read_diff /
        # run_tests / submit_verdict tool activity + final reply) so the thread
        # retains its activity log on reload.  Evaluators have no
        # FileSessionManager (§6.4), so we write agent.messages verbatim here.
        await session_store.persist_agent_messages(
            ctx.workspace_root,
            project_id,
            epic_id,
            eval_id,
            eval_agent.messages,
        )

    return verdict_holder[0]
