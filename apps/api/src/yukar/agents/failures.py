"""Classification of agent-run failures (Worker / Evaluator exceptions).

When a Worker or Evaluator attempt aborts with an exception, the Manager must
be told *what kind* of failure it was — an infrastructure failure says nothing
about the code, and the right reaction differs per kind:

- ``max_tokens`` / ``context_overflow``: the task is too big for one attempt.
  Re-dispatching it verbatim is futile (the hermetic reset discards the partial
  work and the same limit is hit again) — the task must be split or the
  approach changed.
- ``provider_auth``: the LLM provider rejected the credentials (expired STS
  token, missing/invalid API key, …).  EVERY further model call in this run
  will fail the same way, so dispatch is suspended for the rest of the run
  and the Manager must report to the user.
- ``throttled``: the provider throttled the request even after the SDK's own
  retries.  A later retry may succeed; the Manager should report rather than
  hammer the provider.
- ``provider_error``: some other provider/API error (5xx, connection).
- ``other``: an unexpected host/tool exception.

Everything here is pure (no I/O).  Detection is name/duck-typed so this module
imports neither ``botocore`` nor ``anthropic``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FailureKind = Literal[
    "max_tokens",
    "context_overflow",
    "throttled",
    "provider_auth",
    "provider_error",
    "no_verdict",
    "other",
]

# Exception class names (any provider) that mean "credentials are not usable".
_AUTH_EXC_NAMES: frozenset[str] = frozenset(
    {
        # botocore
        "NoCredentialsError",
        "PartialCredentialsError",
        "CredentialRetrievalError",
        "TokenRetrievalError",
        "NoAuthTokenError",
        "SSOTokenLoadError",
        "UnauthorizedSSOTokenError",
        "LoginTokenLoadError",
        "LoginAuthorizationCodeError",
        "UnknownCredentialError",
        # anthropic
        "AuthenticationError",
        "PermissionDeniedError",
    }
)

# botocore ``ClientError`` codes that mean the same thing.
_AUTH_ERROR_CODES: frozenset[str] = frozenset(
    {
        "ExpiredToken",
        "ExpiredTokenException",
        "UnrecognizedClientException",
        "InvalidSignatureException",
        "InvalidClientTokenId",
        "InvalidIdentityToken",
        "AccessDeniedException",
        "AccessDenied",
        "AuthFailure",
        "UnauthorizedException",
        "UnauthorizedAccess",
    }
)

_THROTTLE_EXC_NAMES: frozenset[str] = frozenset(
    {"ModelThrottledException", "RateLimitError", "OverloadedError"}
)
_THROTTLE_ERROR_CODES: frozenset[str] = frozenset(
    {"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"}
)

_PROVIDER_EXC_NAMES: frozenset[str] = frozenset(
    {
        "ClientError",
        "BotoCoreError",
        "EndpointConnectionError",
        "ReadTimeoutError",
        "ConnectTimeoutError",
        "APIStatusError",
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "ServiceUnavailableError",
        "BadRequestError",
        "ModelErrorException",
    }
)


@dataclass(frozen=True, slots=True)
class AgentFailure:
    """One classified failure of a Worker / Evaluator attempt."""

    kind: FailureKind
    #: Class name of the innermost recognised exception (for logs / events).
    exc_name: str
    #: Short human-readable detail (``str(exc)`` truncated).
    detail: str

    @property
    def run_fatal(self) -> bool:
        """True when every further model call in this run would fail the same way."""
        return self.kind == "provider_auth"

    @property
    def retry_verbatim_futile(self) -> bool:
        """True when re-dispatching the identical item cannot be expected to succeed.

        Size failures deterministically recur; an Evaluator that ended without
        a verdict (turn/token limit) is in the same position — the way through
        is a smaller unit of work (a split task / narrower contract), not the
        same call again.
        """
        return self.kind in ("max_tokens", "context_overflow", "no_verdict")


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """Return *exc* followed by its causes/contexts (bounded, no cycles)."""
    seen: list[BaseException] = []
    pending: list[BaseException] = [exc]
    while pending and len(seen) < 12:
        cur = pending.pop(0)
        if cur in seen:
            continue
        seen.append(cur)
        # strands' EventLoopException carries the wrapped error as an attribute;
        # the wrapped error may itself have an explicit cause — follow both.
        original = getattr(cur, "original_exception", None)
        if isinstance(original, BaseException):
            pending.append(original)
        # Explicit causes only (``raise X from Y``).  The implicit ``__context__``
        # is deliberately NOT followed: an unrelated error that happened to be
        # in flight inside an ``except`` block (an S3 AccessDenied during
        # cleanup, say) must not be mistaken for the model call's failure and
        # suspend dispatch for the whole run.
        if cur.__cause__ is not None:
            pending.append(cur.__cause__)
    return seen


def _client_error_code(exc: BaseException) -> str:
    """Return the ``Error.Code`` of a botocore ``ClientError``-shaped exception."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error = response.get("Error")
    if not isinstance(error, dict):
        return ""
    code = error.get("Code")
    return str(code) if code else ""


def _short(exc: BaseException, limit: int = 300) -> str:
    text = str(exc).strip().splitlines()
    first = text[0] if text else ""
    return first[:limit]


def classify_agent_failure(exc: BaseException) -> AgentFailure:
    """Classify an exception raised by a Worker / Evaluator run.

    Walks the cause/context chain so that wrapped provider errors (e.g. a
    ``ClientError`` behind strands' ``EventLoopException``) are recognised.
    """
    chain = _exception_chain(exc)
    names = [type(e).__name__ for e in chain]
    codes = [_client_error_code(e) for e in chain]

    if "MaxTokensReachedException" in names:
        return AgentFailure("max_tokens", "MaxTokensReachedException", _short(exc))
    if "ContextWindowOverflowException" in names:
        return AgentFailure("context_overflow", "ContextWindowOverflowException", _short(exc))

    for e, name, code in zip(chain, names, codes, strict=True):
        if name in _AUTH_EXC_NAMES or code in _AUTH_ERROR_CODES:
            return AgentFailure("provider_auth", name, _short(e))
    for e, name, code in zip(chain, names, codes, strict=True):
        if name in _THROTTLE_EXC_NAMES or code in _THROTTLE_ERROR_CODES:
            return AgentFailure("throttled", name, _short(e))
    for e, name, code in zip(chain, names, codes, strict=True):
        if name in _PROVIDER_EXC_NAMES or code:
            return AgentFailure("provider_error", name, _short(e))

    return AgentFailure("other", type(exc).__name__, _short(exc))


def no_verdict_failure(last_text: str) -> AgentFailure:
    """Failure for an Evaluator that ended without calling ``submit_verdict``."""
    return AgentFailure("no_verdict", "NoVerdict", last_text.strip()[:300])


# ---------------------------------------------------------------------------
# Manager-facing guidance
# ---------------------------------------------------------------------------

Role = Literal["worker", "evaluator"]


def failure_guidance(failure: AgentFailure, *, role: Role, task_id: str) -> str:
    """Return the ``feedback`` text the Manager receives for a failed attempt.

    The text is deliberately explicit about two things the Manager tends to get
    wrong: (1) an infrastructure failure says NOTHING about the code — nothing
    was verified and nothing was committed; (2) what the sensible next step is.
    """
    who = "Worker" if role == "worker" else "Evaluator"
    head = (
        f"ATTEMPT FAILED ({failure.kind}) — the {who} for {task_id} aborted before "
        f"finishing. This is an infrastructure/agent failure, NOT a verdict on the "
        f"code: nothing was verified and nothing was committed. Do not report this "
        f"task as done or claim anything about its tests."
    )
    if failure.detail:
        head += f" Error: {failure.exc_name}: {failure.detail}"

    if failure.kind in ("max_tokens", "context_overflow"):
        if role == "worker":
            return (
                head + " The Worker's output was cut off by the per-response token limit "
                "(or its context overflowed), so the task is too large for one attempt; "
                "the host discarded its half-written files (a truncated file must never "
                "be evaluated or committed). Do NOT "
                "re-dispatch it unchanged — the host rejects a verbatim retry. Split it "
                "into smaller tasks with `task_update` (new IDs; the plan then needs "
                "re-approval), or change the approach via `feedback` (e.g. write files "
                "in smaller pieces with `fs_edit`)."
            )
        return (
            head + " The Evaluator ran out of output tokens / context before it could "
            "judge the diff: this task's change is too large to review in ONE "
            "evaluation. Re-running the evaluation of the same files is futile and the "
            "host rejects it. Make the unit of work smaller instead: split the task with "
            "`task_update` into smaller tasks (new IDs; the plan then needs re-approval) "
            "so each Worker produces a change the Evaluator can review, or narrow this "
            "task's contract — then run the worker+evaluator cycle again. The staged "
            "files of this attempt are listed below."
        )
    if failure.kind == "provider_auth":
        return (
            head + " The LLM provider rejected the call for credential/permission reasons "
            "(expired session token, missing or invalid API key, or access to the model "
            "denied). Every further Worker/"
            "Evaluator call in this run will fail the same way, so the host has "
            "SUSPENDED dispatch for the rest of this run. Do not retry. Tell the user "
            "exactly what happened (quote the error) and end your turn; once they have "
            "fixed the credentials, their next message starts a fresh run."
        )
    if failure.kind == "throttled":
        return (
            head + " The provider throttled the request even after the SDK's own "
            "retries with backoff. Do not hammer it: report the throttling to the user "
            "and ask whether to retry later, then end your turn."
        )
    if failure.kind == "no_verdict":
        return (
            head + " The Evaluator ended its run WITHOUT calling `submit_verdict` "
            "(turn/token limit reached, or it only wrote text). No verdict exists. The "
            "Worker's changes are still staged in the worktree (uncommitted). "
            "Re-running the evaluation of the same files is futile and the host rejects "
            "it: split the task with `task_update` into smaller tasks (new IDs; the plan "
            "then needs re-approval) or narrow this task's contract, then run the "
            "worker+evaluator cycle again. The staged files of this attempt are listed "
            "below."
            + (f" Last Evaluator text: {failure.detail}" if failure.detail else "")
        )
    if failure.kind == "provider_error":
        return (
            head + " The provider/API call failed (see the error). If it happens "
            "again on retry, stop and report it to the user instead of retrying further."
        )
    return (
        head + " This is an unexpected host or tool error. You may retry once; if it "
        "recurs, report the error to the user and end your turn."
    )


def dispatch_suspended_reason(detail: str) -> str:
    """Reason text for dispatch items rejected while the run is provider-faulted."""
    return (
        "Dispatch is SUSPENDED for the rest of this run: the LLM provider rejected "
        "the credentials on an earlier attempt, so every further Worker/Evaluator "
        "call would fail the same way. No agent was started and no attempt was "
        "consumed. Report the credential problem to the user (quote the error) and "
        "end your turn; their next message after fixing it starts a fresh run. "
        f"Original error: {detail}"
    )


def verbatim_retry_reason(task_id: str, kind: str, *, role: str = "worker") -> str:
    """Reason text for a rejected futile re-dispatch after a size / no-verdict failure."""
    if role == "evaluator":
        return (
            f"Rejected: the previous evaluation of {task_id} failed with {kind}, and this "
            "item would evaluate the very same subject (same contract, same set of changed "
            "files) — it would hit the same limit. No attempt was consumed. Make the unit "
            "of work smaller: split the task with `task_update` (new IDs; the plan then "
            "needs re-approval) or narrow the contract, then run worker+evaluator again."
        )
    return (
        f"Rejected: the previous attempt for {task_id} failed with {kind} and this "
        "dispatch item is identical (same contract, same feedback, same agents). "
        "Retrying verbatim would hit the same limit — the half-written work was "
        "discarded, so nothing carries over. No attempt was consumed. Split the task "
        "into smaller tasks with `task_update` (new IDs; the plan then needs "
        "re-approval), or change the approach with a different `feedback`."
    )
