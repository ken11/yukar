"""Shared helpers for runner modules (runner.py, resolve_runner.py, arbiter_runner.py).

``save_and_publish_state`` extracts the repeated 3-step pattern:
  1. Set ``state.status`` and ``state.last_event_at``.
  2. Persist state via ``state_repo.save_state``.
  3. Publish an event via a ``pub`` callable.

All runners use this identical sequence for terminal-state transitions
(error, completed) and it was duplicated 15+ times across the three files.

``run_single_sandbox_agent`` extracts the single-turn sandboxed agent execution
pattern shared by ResolveRunner and ArbiterRunner.  Both runners:
  - Build a StreamTranslator / Agent / AgentUsageRecorder triple.
  - Stream the agent with a cooperative-cancellation loop.
  - Flush usage and append the final message to the session store.

The only caller-controlled differences are: role, system_prompt, prompt, and
the usage_epic_id (real epic_id vs ARBITER_EPIC_SENTINEL).  Everything else is
identical, so this helper collapses the duplication without adding any
conditional branches.

Note: ``create_model`` is intentionally NOT called inside this helper.  Tests
patch ``yukar.runs.resolve_runner.create_model`` and
``yukar.runs.arbiter_runner.create_model`` at the module level, so the call
must remain in each runner module.  The caller constructs the model and passes
it as *model*.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from strands import Agent

from yukar.agents.context import AgentContext
from yukar.agents.streaming import AgentUsageRecorder, StreamTranslator, extract_final_text
from yukar.agents.tools.command import make_command_tools
from yukar.agents.tools.fs import make_fs_tools
from yukar.agents.tools.git_tools import make_git_tools
from yukar.models.run import RunState
from yukar.storage import session_store, state_repo

_RunStatus = Literal["running", "paused", "waiting", "error", "completed"]


async def save_and_publish_state(
    root: str,
    project_id: str,
    epic_id: str,
    state: RunState,
    status: _RunStatus,
    event: Any,
    pub: Callable[[Any], None],
) -> None:
    """Set state status + last_event_at, persist to YAML, and publish *event*.

    This is the canonical helper for terminal-state transitions in all runners.
    The three steps are always performed together and in this order:
      1. ``state.status = status``
      2. ``state.last_event_at = datetime.now(UTC)``
      3. ``await state_repo.save_state(root, project_id, epic_id, state)``
      4. ``pub(event)``

    Args:
        root: Workspace root path.
        project_id: Project identifier.
        epic_id: Epic identifier.
        state: Mutable ``RunState`` object whose ``status`` and
            ``last_event_at`` fields are updated in place.
        status: New status string (e.g. ``"error"``, ``"completed"``).
        event: Event object to pass to *pub*.
        pub: Callable that publishes the event (e.g. the local ``pub``
            closure in each runner's ``start()``).
    """
    state.status = status
    state.last_event_at = datetime.now(UTC)
    await state_repo.save_state(root, project_id, epic_id, state)
    pub(event)


async def run_single_sandbox_agent(
    *,
    ctx: AgentContext,
    model: Any,
    agent_id: str,
    role: str,
    system_prompt: str,
    prompt: str,
    project_id: str,
    epic_id: str,
    run_id: str,
    usage_epic_id: str,
    git_author_name: str,
    git_author_email: str,
    is_stopped: Callable[[], bool],
) -> str | None:
    """Run a single sandboxed agent (resolve or arbiter) to completion.

    This helper encapsulates the streaming scaffold shared by
    ``ResolveRunner._run_resolve_agent`` and
    ``ArbiterRunner._run_arbiter_agent``.  The two callers differ only in
    *role*, *system_prompt*, *prompt*, and *usage_epic_id*; all structural
    wiring is identical and lives here.

    The caller is responsible for constructing *model* via ``create_model``
    before calling this function.  This keeps the ``create_model`` call in each
    runner module so that existing test patches remain valid:
      - ``patch("yukar.runs.resolve_runner.create_model", ...)``
      - ``patch("yukar.runs.arbiter_runner.create_model", ...)``

    Tool set: fs + command + git (with commit, matching both callers' defaults).

    Usage recording uses *usage_epic_id*, which may differ from *epic_id*.
    For resolve runs both are the real epic id.  For arbiter runs *epic_id*
    is the real epic id (used by StreamTranslator, session_store, and the
    final-message append) while *usage_epic_id* is ``ARBITER_EPIC_SENTINEL``
    so that arbiter costs are tracked in a dedicated bucket.

    Args:
        ctx: Agent context (worktree sandbox, path guard, command config).
        model: Pre-constructed Strands model (caller must call create_model).
        agent_id: Unique agent / thread identifier (UUID string).
        role: Role label for usage recording (``"worker"`` or ``"arbiter"``).
        system_prompt: System prompt for this agent role.
        prompt: User-turn prompt to pass to ``agent.stream_async``.
        project_id: Project identifier.
        epic_id: Real epic identifier used for streaming, session, and append.
        run_id: Run identifier for usage recording and streaming.
        usage_epic_id: Epic id used for usage recording only.  Pass the real
            epic id for worker-style runs, or ``ARBITER_EPIC_SENTINEL`` for
            arbiter runs.
        git_author_name: Git commit author name passed to ``make_git_tools``.
        git_author_email: Git commit author email passed to ``make_git_tools``.
        is_stopped: Zero-argument callable that returns ``True`` when the
            runner has been asked to stop.  Checked between agent turns.

    Returns:
        The agent's final text message, or ``None`` if the agent produced no
        text output.
    """
    translator = StreamTranslator(
        project_id=project_id,
        epic_id=epic_id,
        run_id=run_id,
        thread_id=agent_id,
    )

    fs_tools = make_fs_tools(ctx)
    cmd_tools = make_command_tools(ctx)
    git_tools_list = make_git_tools(ctx, git_author_name, git_author_email)

    agent = Agent(
        model=model,
        agent_id=agent_id,
        tools=[*fs_tools, *cmd_tools, *git_tools_list],
        callback_handler=translator.callback,
        system_prompt=system_prompt,
    )
    usage_recorder = AgentUsageRecorder(
        project_id=project_id,
        epic_id=usage_epic_id,
        run_id=run_id,
        role=role,
    ).bind(agent)

    try:
        async for _ in agent.stream_async(prompt):
            if is_stopped():
                break
    finally:
        await usage_recorder.flush()

    final_text = extract_final_text(agent)
    if final_text:
        await session_store.append_message(
            ctx.workspace_root,
            project_id,
            epic_id,
            agent_id,
            role="assistant",
            text=final_text,
        )
    return final_text
