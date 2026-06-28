"""Canonical role Literal definitions — single source of truth for all role sets.

This module is a pure leaf: it imports nothing from yukar itself (only from
``typing``).  That invariant prevents import cycles when other modules (including
``config/paths.py``) depend on it.

Four distinct role sets exist in the system; they must NOT be collapsed:

1. ``AgentRole`` (4 roles: manager / worker / evaluator / arbiter)
   Roles that drive LLM invocations, sandbox execution, and usage attribution.
   ``arbiter`` is a system-generated role used only during multi-Epic merges.

2. ``ThreadRole`` (5 roles: above + user)
   Valid values for ``ThreadEntry.role``.  A ``user`` thread can appear in the
   threads.yaml index when a human message seeds a conversation.

3. ``ConfigurableAgentRole`` (3 roles: manager / worker / evaluator)
   Roles for which users can set custom instructions (agent-configs L1 API).
   ``arbiter`` and ``user`` are excluded: arbiter has no user-facing instruction
   surface; user is not an agent role.

4. ``UserCreatableThreadRole`` (4 roles: manager / worker / evaluator / user)
   Roles that can appear in a POST /threads request body.  ``arbiter`` is
   excluded because arbiter threads are created internally by the merge system
   and must never be created directly by clients.
"""

from __future__ import annotations

from typing import Literal

# ------------------------------------------------------------------
# 1. AgentRole — LLM / sandbox / usage attribution
# ------------------------------------------------------------------

AgentRole = Literal["manager", "worker", "evaluator", "arbiter"]
"""Roles that correspond to LLM-driven agents and usage attribution buckets."""

# ------------------------------------------------------------------
# 2. ThreadRole — threads.yaml index entry roles
# ------------------------------------------------------------------

ThreadRole = Literal["manager", "worker", "evaluator", "arbiter", "user"]
"""All valid roles for a ThreadEntry (agent roles + human user)."""

# ------------------------------------------------------------------
# 3. ConfigurableAgentRole — user-facing agent instruction surface
# ------------------------------------------------------------------

ConfigurableAgentRole = Literal["manager", "worker", "evaluator"]
"""Roles for which users can configure custom instructions.

arbiter is excluded (system-internal, no instruction surface).
user is excluded (not an agent role).
"""

# ------------------------------------------------------------------
# 4. UserCreatableThreadRole — roles allowed in POST /threads
# ------------------------------------------------------------------

UserCreatableThreadRole = Literal["manager", "worker", "evaluator", "user"]
"""Roles that a client may specify when creating a thread via the API.

arbiter is excluded: arbiter threads are generated internally by the merge
system (Arbiter role) and must not be created directly by API clients.
"""
