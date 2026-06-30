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


def _entry_matches(entry: str, tokens: list[str]) -> bool:
    """Return ``True`` if an allow/deny *entry* matches the command *tokens*.

    An entry is one or more whitespace-separated tokens (a single line of the
    operator's allow/deny textarea).  Matching is **command-prefix** based:

    - argv[0] (``tokens[0]``) matches the entry's first token either by its raw
      value *or* by its ``Path.name`` basename, so ``/usr/bin/pytest`` is treated
      the same as ``pytest``.
    - For a **single-token** entry (e.g. ``"pytest"``) that is the whole test —
      it matches *any* invocation of that command regardless of arguments.  This
      preserves the historical command-name semantics.
    - For a **multi-token** entry (e.g. ``"make generate"``, ``"pnpm test"``)
      every token after the first must equal the command token at the same
      position (literal positional match).  Trailing command arguments beyond the
      entry are allowed, so ``"pnpm test"`` matches ``pnpm test --filter x`` but
      ``"make generate"`` does *not* match ``make build``.

    A multi-token entry is therefore strictly **more restrictive** than its first
    token alone — it can only narrow what a bare command name would permit, never
    widen it.  Empty/blank entries never match.
    """
    entry_tokens = entry.split()
    if not entry_tokens or len(entry_tokens) > len(tokens):
        return False

    # argv[0]: match by raw value or basename (mirrors legacy single-token rule).
    cmd0, e0 = tokens[0], entry_tokens[0]
    if e0 != cmd0 and e0 != Path(cmd0).name:
        return False

    # Remaining entry tokens must match the command positionally (literal).
    # entry_tokens[1:] is no longer than tokens[1:] (length guard above), so
    # zip stops at the entry length — trailing command args are allowed.
    return all(e == c for e, c in zip(entry_tokens[1:], tokens[1:], strict=False))


@dataclass(frozen=True, slots=True)
class RepoCommandConfig:
    """Allow/deny lists for ``run_command`` inside a worktree.

    Evaluation order:
      1. Deny is checked first.  Any entry that command-prefix matches → rejected
         regardless of the allow list.
      2. If the allow list is **non-empty**, some allow entry must command-prefix
         match for the command to be permitted.
      3. If the allow list is **empty**, the command is **denied** (explicit
         allowlist required — fail-safe default).

    Each entry is a command line of one or more whitespace-separated tokens, as
    typed one-per-line in the operator UI (e.g. ``pytest``, ``pnpm test``,
    ``make generate``).  A single-token entry matches any invocation of that
    command (by raw token or basename, so ``/bin/rm`` == ``rm``); a multi-token
    entry additionally requires the following tokens to match positionally, which
    lets operators allow a specific subcommand (``make generate``) without
    allowing the whole command (``make``).  See ``_entry_matches`` for the exact
    rule.  The distinction between an empty allow list meaning "allow all" vs.
    "deny all" was a security bug: the behaviour is deny-by-default.

    To allow all commands (not recommended), ``allow=("*",)`` is NOT supported —
    instead explicitly list every command the repo needs.

    Shell wrappers (sh/bash/env) are intentionally given no special treatment.
    They are subject to the same allowlist rules.  If the operator does not
    explicitly add them to the allow list they will be rejected — which is the
    correct posture (shell wrappers bypass argument-level restrictions).
    """

    allow: tuple[str, ...] = field(default_factory=tuple)
    deny: tuple[str, ...] = field(default_factory=tuple)

    def is_allowed(self, tokens: list[str]) -> bool:
        """Return ``True`` if the command *tokens* may be executed.

        Deny is checked first (deny takes priority over allow).  Both allow and
        deny entries are matched against the full argv with ``_entry_matches`` so
        that multi-token entries (``make generate``, ``pnpm test``) match a
        specific subcommand while single-token entries (``pytest``) match any
        invocation of that command.

        Args:
            tokens: The shlex-split command argv (``tokens[0]`` is the binary,
                which may be a bare name or an absolute path).

        Returns:
            ``True`` if the command is permitted.
        """
        if not tokens:
            return False

        # Deny check — any matching deny entry blocks the command.
        if any(_entry_matches(entry, tokens) for entry in self.deny):
            return False

        # Empty allow list → all commands denied (fail-safe / explicit allowlist).
        if not self.allow:
            return False

        # Non-empty allow list: some allow entry must command-prefix match.
        return any(_entry_matches(entry, tokens) for entry in self.allow)


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
