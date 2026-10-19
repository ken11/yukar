"""Every role's system prompt carries the PEP 758 hard rule.

Models trained before Python 3.14 read `except A, B:` as Python 2 syntax and
keep flagging it.  The rule must reach every role — including any role added
later — so this scans all ``*_SYSTEM_PROMPT`` constants instead of listing them.
"""

from __future__ import annotations

from yukar.agents import prompts


def _system_prompts() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(prompts).items()
        if name.endswith("_SYSTEM_PROMPT") and isinstance(value, str)
    }


def test_all_roles_are_scanned() -> None:
    assert set(_system_prompts()) >= {
        "_MANAGER_SYSTEM_PROMPT",
        "_WORKER_SYSTEM_PROMPT",
        "_EVALUATOR_SYSTEM_PROMPT",
        "_REVIEWER_SYSTEM_PROMPT",
        "_ARBITER_SYSTEM_PROMPT",
        "_RESOLVE_SYSTEM_PROMPT",
    }


def test_every_system_prompt_carries_pep758_rule() -> None:
    rule = prompts._PEP758_RULE
    missing = [name for name, value in _system_prompts().items() if rule not in value]
    assert missing == []
