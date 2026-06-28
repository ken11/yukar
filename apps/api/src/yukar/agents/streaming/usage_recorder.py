"""AgentUsageRecorder — records Strands agent usage to the token tracker.

Extracted from :mod:`~yukar.agents.streaming` for readability.  All public
names continue to be importable from ``yukar.agents.streaming`` via the
package ``__init__.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from strands import Agent

from yukar.agents.streaming.helpers import (
    is_budget_enforcement_active,
    is_zero_delta,
    resolve_model_id,
    usage_delta,
    usage_snapshot,
)
from yukar.usage.tracker import UsageDelta

logger = logging.getLogger(__name__)


class AgentUsageRecorder:
    """Record Strands usage as soon as each assistant message completes.

    The recorder is bound after Agent construction so it can wrap the Agent's
    existing callback handler while reading that same Agent's cumulative event
    loop metrics. Snapshot advancement happens synchronously in the callback;
    only the tracker write is scheduled, preventing duplicate accounting when
    multiple callbacks arrive before an earlier write completes.

    Inference profile resolution
    ----------------------------
    When the model is a Bedrock application inference profile ARN (e.g.
    ``arn:aws:bedrock:…:application-inference-profile/<id>``), the pricing
    table cannot match the opaque ARN.  On the first :meth:`_record` call,
    :func:`~yukar.llm.inference_profile.resolve_model_id_for_pricing` is
    awaited to translate the ARN to its underlying foundation model ID, which
    is then used for all subsequent tracker writes.  Subsequent parallel
    ``_record`` tasks that arrive before resolution completes each set
    ``self._model_id`` independently — because the module-level cache + lock
    inside the resolver makes the actual Bedrock API call exactly once, this
    is idempotent and safe without additional locking here.
    """

    def __init__(
        self,
        *,
        project_id: str,
        epic_id: str,
        run_id: str,
        role: str,
    ) -> None:
        self._project_id = project_id
        self._epic_id = epic_id
        self._run_id = run_id
        self._role = role
        self._agent: Agent | None = None
        self._model_id = "unknown"
        self._is_bedrock: bool = False
        self._region: str | None = None
        self._model_resolved: bool = False
        self._wrapped_callback: Any = None
        self._snapshot: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._pending: set[asyncio.Task[None]] = set()

    @property
    def pending_count(self) -> int:
        """Return the number of tracker writes that have not completed."""
        return len(self._pending)

    def bind(self, agent: Agent) -> AgentUsageRecorder:
        """Wrap ``agent.callback_handler`` and initialise its usage snapshot."""
        if self._agent is not None:
            raise RuntimeError("AgentUsageRecorder is already bound")
        self._agent = agent
        self._model_id = resolve_model_id(agent.model)
        self._is_bedrock = type(agent.model).__name__ == "BedrockModel"
        self._region = getattr(
            getattr(getattr(agent.model, "client", None), "meta", None),
            "region_name",
            None,
        )
        self._wrapped_callback = agent.callback_handler
        self._snapshot = usage_snapshot(agent)
        agent.callback_handler = self.callback
        return self

    def callback(self, **kwargs: Any) -> None:
        """Forward a Strands callback and schedule assistant-message usage."""
        self._wrapped_callback(**kwargs)
        message = kwargs.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return
        self._schedule_increment()

    async def flush(self) -> None:
        """Wait for pending writes unless budget enforcement is stopping this run.

        A record task that breaches the budget calls ``supervisor.stop`` and may
        wait for this same run to finish. Waiting for that task here would form
        a cycle and force the supervisor's five-second hard cancellation. Once
        the tracker reports that the budget is exceeded, leave pending writes
        running; their done callbacks still collect exceptions and remove them.
        """
        while self._pending:
            if is_budget_enforcement_active():
                return
            await asyncio.wait(tuple(self._pending), timeout=0.05)

    def _schedule_increment(self) -> None:
        agent = self._agent
        if agent is None:
            return
        current = usage_snapshot(agent)
        delta = usage_delta(self._snapshot, current)
        self._snapshot = current
        if is_zero_delta(delta):
            return
        task = asyncio.create_task(self._record(delta))
        self._pending.add(task)
        task.add_done_callback(self._on_record_done)

    async def _record(self, delta: UsageDelta) -> None:
        try:
            # Resolve application inference profile ARN to a foundation model ID
            # on the first record call.  The module-level cache + lock inside
            # resolve_model_id_for_pricing ensures the Bedrock API is called at
            # most once per unique ARN across the entire process.
            if not self._model_resolved:
                from yukar.llm.inference_profile import resolve_model_id_for_pricing

                self._model_id = await resolve_model_id_for_pricing(
                    self._model_id,
                    region=self._region,
                    provider_is_bedrock=self._is_bedrock,
                )
                self._model_resolved = True

            from yukar.usage.tracker import get_tracker

            await get_tracker().record(
                project_id=self._project_id,
                epic_id=self._epic_id,
                run_id=self._run_id,
                role=self._role,
                model_id=self._model_id,
                delta=delta,
            )
        except Exception:
            logger.debug("Usage tracking failed", exc_info=True)

    def _on_record_done(self, task: asyncio.Task[None]) -> None:
        self._pending.discard(task)
        with contextlib.suppress(asyncio.CancelledError):
            exception = task.exception()
            if exception is not None:
                logger.debug("Usage tracking task failed", exc_info=exception)
