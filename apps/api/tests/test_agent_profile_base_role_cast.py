"""Finding verification: base-role-cast

finding[base-role-cast]: write_agent_profile in agents/tools/agent_profile_tools.py
does not validate base_role; it narrows it with cast instead.

    _base_role = cast(Literal["worker", "evaluator"], base_role)  # L117
    profile = AgentProfile(..., base_role=_base_role, ...)         # L118-L129

cast() is a complete no-op at runtime in CPython. Invalid values pass through
until AgentProfile's pydantic Literal constraint raises a ValidationError.

As a result, the response to an invalid base_role is:
  - Structurally correct: goes through make_error → status="error", ok=False, "error" key
  - But the error message is a pydantic internal ValidationError dump;
    "explicit pre-validation" (check tool arg value → make_error) does not exist.

Test strategy:
  1. Characterization test (PASS): pin that the make_error structure
     (status="error", ok=False, "error" key) is returned even for invalid base_role.
  2. xfail(strict=True) test: record the expectation that the error message should
     contain concise wording from pre-validation ("must be" etc., explicit guard).
     Currently it is a multi-line pydantic ValidationError dump that does not meet this
     → remove the marker after the fix (add explicit if guard).
  3. Characterization test (PASS): pin that valid base_role ("worker"/"evaluator") succeeds
     (regression guard).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _get_write_fn(tmp_path: Path) -> Any:
    """Return the unwrapped write_agent_profile callable."""
    from yukar.agents.tools.agent_profile_tools import make_agent_profile_tools

    tools = make_agent_profile_tools(str(tmp_path), "proj")
    write_tool = tools[2]
    return write_tool.func if hasattr(write_tool, "func") else write_tool.__wrapped__


# ---------------------------------------------------------------------------
# 1. Characterization: structural correctness of error response (PASS)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_base_role_returns_make_error_structure(tmp_path: Path) -> None:
    """Invalid base_role is returned as make_error structure (characterization test).

    write_agent_profile wraps pydantic ValidationError via
    except Exception → make_error(str(exc), ok=False), so the response shape is correct.
    Pin this shape to ensure future changes do not break the structure.
    """
    write_fn = _get_write_fn(tmp_path)

    result = await write_fn(name="bad", description="", base_role="manager")

    # make_error structure is always returned
    assert result.get("status") == "error", "status must be 'error'"
    assert result.get("ok") is False, "ok must be False"
    assert "error" in result, "'error' key must exist"

    # content is list[dict] form (the shape make_error returns)
    content = result.get("content", [])
    assert isinstance(content, list) and len(content) > 0
    assert isinstance(content[0], dict) and "text" in content[0]


@pytest.mark.asyncio
async def test_empty_string_base_role_returns_make_error_structure(tmp_path: Path) -> None:
    """Empty-string base_role is also returned as make_error structure (characterization test)."""
    write_fn = _get_write_fn(tmp_path)

    result = await write_fn(name="bad-empty", description="", base_role="")

    assert result.get("status") == "error"
    assert result.get("ok") is False
    assert "error" in result


@pytest.mark.asyncio
async def test_case_mismatch_base_role_returns_make_error_structure(tmp_path: Path) -> None:
    """Case-mismatched 'Worker' is also invalid and returned as make_error structure
    (characterization test)."""
    write_fn = _get_write_fn(tmp_path)

    result = await write_fn(name="bad-case", description="", base_role="Worker")

    assert result.get("status") == "error"
    assert result.get("ok") is False
    assert "error" in result


# ---------------------------------------------------------------------------
# 2. Bug: error message is a pydantic internal dump, not from explicit pre-validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_base_role_returns_targeted_error_message(tmp_path: Path) -> None:
    """Invalid base_role error message should contain clear wording, not a pydantic dump.

    Expected: return make_error('Invalid base_role ...', ok=False) from the tool before
    reaching pydantic, or at minimum return a human-readable message that does not contain
    internal framework strings like "pydantic" / "validation error".

    Current: pydantic ValidationError.str() becomes the error text directly,
    exposing internal wording "1 validation error for AgentProfile".
    This xfail remains as a safety net that only passes after the fix.
    """
    write_fn = _get_write_fn(tmp_path)

    result = await write_fn(name="bad-msg", description="", base_role="manager")

    error_text: str = result.get("error", "")

    # Conditions expected after the fix:
    # - Must not contain pydantic internal framework wording
    assert "validation error for AgentProfile" not in error_text, (
        "pydantic internal class name is exposed: "
        "add explicit base_role check inside the tool and call make_error"
    )
    # - Error message should be concise (roughly 1 line)
    assert "\n" not in error_text.strip(), (
        "Error message spans multiple lines: make it a clear make_error('...', ok=False) call"
    )


# ---------------------------------------------------------------------------
# 3. Regression guard: valid base_role succeeds (PASS)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_base_role_worker_succeeds(tmp_path: Path) -> None:
    """base_role='worker' succeeds (regression guard)."""
    write_fn = _get_write_fn(tmp_path)

    result = await write_fn(
        name="valid-worker",
        description="A valid worker profile",
        base_role="worker",
    )

    assert result.get("status") == "success"
    assert result.get("ok") is True
    assert result.get("name") == "valid-worker"


@pytest.mark.asyncio
async def test_valid_base_role_evaluator_succeeds(tmp_path: Path) -> None:
    """base_role='evaluator' succeeds (regression guard)."""
    write_fn = _get_write_fn(tmp_path)

    result = await write_fn(
        name="valid-evaluator",
        description="A valid evaluator profile",
        base_role="evaluator",
    )

    assert result.get("status") == "success"
    assert result.get("ok") is True
    assert result.get("name") == "valid-evaluator"
