"""AgentContext — typed context bundle passed to Worker and Evaluator tool factories.

Each Worker is created with an ``AgentContext`` that is frozen at construction
time and captures everything the worker needs to know about its scope:

- which project / epic it belongs to
- which repo it has been assigned (exactly one)
- the absolute path to *that* repo's worktree (the only FS scope it may touch)
- the per-repo command allow/deny lists
- the active ``PathGuard`` instance derived from the worktree path

The context is a plain frozen dataclass — it holds no mutable state and can be
passed safely into closures.

Usage
-----
Tool factories in ``agents/tools/`` receive a context and return bound tool
functions whose closures capture the context.  This makes it structurally
impossible for a Worker to access any path outside its assigned worktree
without explicitly bypassing Python.

Gitignore wiring
----------------
``AgentContext.create`` is an **async** factory.  It builds ``IgnoreRules``
from the worktree's gitignore files (via ``asyncio.to_thread`` so the
synchronous filesystem walk does not block the event loop) and passes the
resulting ``ignore_fn`` to ``PathGuard``.  This ensures that the gitignore
sandbox (spec §6.6) is active on every production-path context; no manual
``object.__setattr__`` patching in tests is needed or expected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from yukar.sandbox.path_guard import PathGuard


@dataclass(frozen=True, slots=True)
class RepoCommandConfig:
    """Allow/deny lists for ``run_command`` inside a worktree.

    Evaluation order:
      1. Deny is checked first (by raw token and by basename).  Any match
         → rejected regardless of the allow list.
      2. If the allow list is **non-empty**, the basename must appear in it
         for the command to be permitted.
      3. If the allow list is **empty**, the command is **denied** (explicit
         allowlist required — fail-safe default).

    Both lists contain command names (first token / basename) to check against.
    The distinction between an empty allow list meaning "allow all" vs.
    "deny all" was a security bug: the new behaviour is deny-by-default.

    To allow all commands (not recommended), use ``allow=("*",)`` is NOT
    supported — instead explicitly list every command the repo needs.

    Shell wrappers (sh/bash/env) are intentionally given no special treatment.
    They are subject to the same allowlist rules.  If the operator does not
    explicitly add them to the allow list they will be rejected — which is the
    correct posture (shell wrappers bypass argument-level restrictions).
    """

    allow: tuple[str, ...] = field(default_factory=tuple)
    deny: tuple[str, ...] = field(default_factory=tuple)

    def is_allowed(self, first_token: str) -> bool:
        """Return ``True`` if ``first_token`` may be executed.

        Deny is checked first (deny takes priority over allow).  Both the raw
        token and its ``Path.name`` basename are checked against deny and allow
        lists so that absolute paths like ``/bin/rm`` are treated identically
        to ``rm``.

        Args:
            first_token: The first whitespace-delimited token of the command
                as passed by the caller (may be a bare name or an absolute
                path, e.g. ``"pytest"``, ``"/usr/bin/python"``).

        Returns:
            ``True`` if the command is permitted.
        """
        basename = Path(first_token).name

        # Deny check — either the raw token or its basename triggers a block.
        if first_token in self.deny or basename in self.deny:
            return False

        # Empty allow list → all commands denied (fail-safe / explicit allowlist).
        if not self.allow:
            return False

        # Non-empty allow list: basename or raw token must appear.
        return first_token in self.allow or basename in self.allow


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Immutable context bundle for a single Worker or Evaluator agent.

    Attributes:
        project_id: Identifier of the parent Project.
        epic_id: Identifier of the Epic this agent belongs to.
        repo_name: Name of the repository assigned to this agent.
        worktree_path: Absolute path to the git worktree for this
            (epic, repo) pair.  All fs/command/git operations are confined
            here by the embedded ``path_guard``.
        command_config: Allow/deny configuration for ``run_command``.
        path_guard: ``PathGuard`` instance rooted at ``worktree_path``.
            Constructed automatically from ``worktree_path`` if not provided.
        workspace_root: Workspace root path (``yukar-projects/``).  Needed for
            service calls that write back to YAML state files.
    """

    project_id: str
    epic_id: str
    repo_name: str
    worktree_path: Path
    workspace_root: str
    command_config: RepoCommandConfig = field(default_factory=RepoCommandConfig)
    path_guard: PathGuard = field(init=False)

    def __post_init__(self) -> None:
        # Bypass frozen=True to set the computed field.
        # NOTE: PathGuard is constructed here *without* an ignore_fn as a
        # synchronous fallback.  The async ``create`` factory overwrites this
        # with a PathGuard that carries the gitignore-aware ignore_fn built from
        # ``IgnoreRules.from_repo``.  Direct dataclass construction (not via
        # ``create``) will therefore lack gitignore filtering — use ``create``
        # for all production paths.
        object.__setattr__(self, "path_guard", PathGuard(self.worktree_path))

    @classmethod
    async def create(
        cls,
        *,
        project_id: str,
        epic_id: str,
        repo_name: str,
        worktree_path: Path,
        workspace_root: str,
        allow: list[str] | None = None,
        deny: list[str] | None = None,
    ) -> AgentContext:
        """Async convenience constructor: builds ``RepoCommandConfig`` and wires
        gitignore rules into ``PathGuard``.

        ``IgnoreRules.from_repo`` performs a synchronous filesystem walk; this
        factory runs it in a thread pool so the event loop stays responsive.

        Args:
            project_id: Project identifier.
            epic_id: Epic identifier.
            repo_name: Assigned repository name.
            worktree_path: Absolute worktree path.
            workspace_root: Workspace root string from settings.
            allow: Command allow list (first tokens).  ``None`` → empty list
                (deny-by-default; see ``RepoCommandConfig``).
            deny: Command deny list (first tokens).  ``None`` → no extra denies.

        Returns:
            A fully initialised ``AgentContext`` with gitignore-aware
            ``PathGuard`` (spec §6.6).
        """
        from yukar.sandbox.ignore import IgnoreRules

        cfg = RepoCommandConfig(
            allow=tuple(allow or []),
            deny=tuple(deny or []),
        )
        ctx = cls(
            project_id=project_id,
            epic_id=epic_id,
            repo_name=repo_name,
            worktree_path=worktree_path,
            workspace_root=workspace_root,
            command_config=cfg,
        )
        # Build IgnoreRules in a thread (synchronous filesystem walk).
        ignore_rules = await IgnoreRules.from_repo_async(worktree_path)
        ignore_fn = ignore_rules.make_ignore_fn()
        # Replace the PathGuard with one that carries the ignore_fn.
        object.__setattr__(ctx, "path_guard", PathGuard(worktree_path, ignore_fn=ignore_fn))
        return ctx
