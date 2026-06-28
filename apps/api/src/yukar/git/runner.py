"""Low-level git CLI runner using asyncio.create_subprocess_exec.

All git subprocesses go through here to allow cancellation on pause/stop.

Security hardening layers
-------------------------
Every ``run_git`` call applies **Tier A** unconditionally:

* ``build_subprocess_env`` replaces the raw ``os.environ.copy()`` so host
  secrets (ANTHROPIC_API_KEY / AWS_* / GITHUB_TOKEN / SSH_AUTH_SOCK …) are
  never inherited by any git child process.

When ``harden=True`` (the default, fail-safe), additional **Tier B** flags are
prepended at the top level:

* ``--no-pager`` — belt-and-suspenders against ``core.pager`` pager exec.
* ``-c core.hooksPath=<empty-dir>`` — disables all hooks (.git/hooks and any
  ``core.hooksPath`` override) for every subcommand.
* ``-c core.fsmonitor=false`` — disables ``core.fsmonitor`` program execution
  which fires on status/diff/add/commit and is NOT stopped by ``hooksPath``.
* ``-c core.pager=cat`` — redundant with ``--no-pager`` for extra safety.
* ``-c core.sshCommand=false`` — kills any SSH-based external command.
* ``-c core.alternateRefsCommand=`` — empty → disables the alternate refs
  external command.
* ``-c gc.auto=0 -c maintenance.auto=false`` — prevents background GC/
  maintenance processes spawned during normal operations.

When ``isolate_config=True`` (the default), **Tier C** adds:

* ``GIT_CONFIG_NOSYSTEM=1`` and ``GIT_CONFIG_GLOBAL=/dev/null`` — prevents
  system and global config from being loaded, so agent-authored entries in
  local/shared config cannot be reinforced by operator global config, and no
  global config is read.  Set ``isolate_config=False`` for host-context paths
  (UI merge, UI commit, worktree checkout) where the operator's git-lfs or
  autocrlf global settings should be honoured.

* ``GIT_ATTR_NOSYSTEM=1`` — skips the system-level gitattributes file.
  In-tree ``.gitattributes`` still apply (unavoidable), but are mitigated by
  ``--no-ext-diff --no-textconv`` on diff subcommands.

``--no-ext-diff`` and ``--no-textconv`` are **subcommand-level** flags that
belong on the ``diff`` subcommand, NOT here.  Callers that invoke ``git diff``
(directly or via helpers in git/diff.py, git/status.py, git/resolve.py,
agents/tools/) must add them after the ``"diff"`` token.  See each call site.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass
from pathlib import Path

# Default timeout (seconds) for a single git invocation.  Most operations
# (status, diff, add, commit, ls-tree, show) finish in milliseconds; only
# pathological cases (NFS stall, credential helper prompt, giant repo) can
# hang.  120 s is generous for any local operation and matches the timeout
# used by ``agents/tools/command.py`` for shell commands.
_GIT_TIMEOUT: float = 120.0

# Cap on captured stdout/stderr per git invocation.  ``proc.communicate()``
# buffers the entire child output in memory, so a pathological diff (a huge
# binary blob, a multi-GB generated file, a fork bomb of output) could exhaust
# memory and take down the single uvicorn event loop.  We cap each stream at a
# few MiB and append a clear truncation marker.  8 MiB comfortably holds any
# realistic human-reviewable diff while bounding worst-case memory.
_MAX_CAPTURE_BYTES = 8 * 1024 * 1024
_TRUNCATION_MARKER = "\n…[output truncated: exceeded {limit} bytes]\n"


def _truncate(raw: bytes, limit: int | None = None) -> str:
    """Decode *raw* git output, truncating to *limit* bytes with a marker.

    Truncation happens on the **byte** stream (before decode) so the memory
    bound holds regardless of multibyte content; ``errors="replace"`` keeps a
    split multibyte sequence at the boundary from raising.

    *limit* defaults to the module-level ``_MAX_CAPTURE_BYTES`` resolved at call
    time (not bound as a default-arg value) so it stays overridable in tests.
    """
    if limit is None:
        limit = _MAX_CAPTURE_BYTES
    if len(raw) > limit:
        return (
            raw[:limit].decode("utf-8", errors="replace")
            + _TRUNCATION_MARKER.format(limit=limit)
        )
    return raw.decode("utf-8", errors="replace")


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class GitError(Exception):
    def __init__(self, result: GitResult, cmd: list[str]) -> None:
        self.result = result
        self.cmd = cmd
        super().__init__(
            f"git {' '.join(cmd[1:])} failed (rc={result.returncode}): {result.stderr}"
        )


class GitRefError(ValueError):
    """Raised when a git ref/branch token is unsafe to pass as a positional arg.

    The principal danger is a ref that begins with ``-``: git would parse it as
    an option (e.g. ``--output=…`` on diff, or a leaked switch inside
    ``worktree add``'s internal ``git branch`` call) rather than a revision.
    """


def validate_git_ref(ref: str, *, what: str = "ref") -> str:
    """Validate that *ref* is safe to pass to git as a positional argument.

    LLM- and config-derived refs (branch names, range specs like ``a...b``,
    default-branch names) reach git as positional tokens.  A leading ``-`` would
    let git treat the token as an option.  ``--end-of-options`` protects most
    subcommands, but commands like ``worktree add`` re-invoke ``git branch``
    internally where the separator does not propagate, so we additionally reject
    refs that begin with ``-``.

    Args:
        ref: The branch / ref / range-spec token.
        what: Human label for the error message (e.g. ``"branch"``).

    Returns:
        *ref* unchanged when valid (so call sites can inline the validation).

    Raises:
        GitRefError: If *ref* is empty or begins with ``-``.
    """
    if not ref:
        raise GitRefError(f"git {what} must not be empty")
    if ref.startswith("-"):
        raise GitRefError(
            f"git {what} {ref!r} starts with '-' and would be parsed as a git "
            "option; refusing to use it as a revision."
        )
    return ref


def git_author_env(name: str, email: str) -> dict[str, str]:
    """Return a dict of GIT_AUTHOR_* / GIT_COMMITTER_* env vars.

    Centralises the repeated construction of the author/committer identity
    environment passed to git commit and git merge operations.

    Args:
        name: Git author/committer name.
        email: Git author/committer email.

    Returns:
        Dict with GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL, GIT_COMMITTER_NAME,
        GIT_COMMITTER_EMAIL keys — ready to pass as ``env`` to ``run_git``.
    """
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }


def parse_numstat(output: str) -> list[tuple[int, int, str]]:
    """Parse ``git diff --numstat -z`` output into (added, deleted, path) tuples.

    Centralises the numstat parsing duplicated between ``git/diff.py`` and
    ``git/status.py``.  All callers MUST run numstat with ``-z`` so the path
    field is NUL-delimited and emitted **raw** (no double-quoting, no octal
    escaping).  This makes the keys produced here match the raw paths from
    ``git status --porcelain -z`` exactly, even for renames and non-ASCII
    filenames — neither of which the legacy newline format keyed correctly.

    NUL-delimited record grammar (``--numstat -z``)::

        <added>\\t<deleted>\\t<path>\\0                 # normal change
        <added>\\t<deleted>\\t\\0<oldpath>\\0<newpath>\\0  # rename / copy

    For a rename the path immediately following the second ``\\t`` is empty
    (an extra leading ``\\0``) and the *old* and *new* paths follow as two
    separate NUL-terminated fields; we key on the **new** path so it matches
    the porcelain rename target.

    Args:
        output: Raw stdout from ``git diff --numstat -z`` (already UTF-8
            decoded; NUL bytes are preserved as ``\\x00``).

    Returns:
        List of ``(added, deleted, filepath)`` tuples.  Binary files (shown
        with ``-`` counts) are represented with 0 counts.
    """
    # Split into NUL-delimited fields.  A trailing NUL produces an empty final
    # element which we ignore via the index-bounded walk below.
    fields = output.split("\x00")
    result: list[tuple[int, int, str]] = []
    i = 0
    n = len(fields)
    while i < n:
        field = fields[i]
        if not field:
            # Stray empty field (e.g. the trailing element after the final NUL).
            i += 1
            continue
        # Each record starts with "added\tdeleted\t<path-or-empty>".
        parts = field.split("\t", 2)
        if len(parts) != 3:
            # Not a well-formed numstat head — skip defensively.
            i += 1
            continue
        try:
            added = int(parts[0]) if parts[0] != "-" else 0
            deleted = int(parts[1]) if parts[1] != "-" else 0
        except ValueError:
            i += 1
            continue
        path_head = parts[2]
        if path_head == "":
            # Rename/copy: the old and new paths are the next two NUL fields.
            # Key on the new path (parts ahead: [i+1]=old, [i+2]=new).
            if i + 2 < n:
                filepath = fields[i + 2]
                result.append((added, deleted, filepath))
            i += 3
        else:
            filepath = path_head
            result.append((added, deleted, filepath))
            i += 1
    return result


async def run_git(
    *args: str,
    cwd: Path,
    check: bool = True,
    env: dict[str, str] | None = None,
    harden: bool = True,
    isolate_config: bool = True,
) -> GitResult:
    """Run a git command and return the result.

    Args:
        *args: git arguments (e.g. "status", "--porcelain").
            Note: ``--no-ext-diff`` and ``--no-textconv`` are diff-subcommand
            flags — they must be placed after ``"diff"`` in the *args*, not
            here.
        cwd: Working directory.
        check: If True, raise GitError on non-zero exit.
        env: Explicit, trusted environment additions (e.g. ``git_author_env``
            output).  Merged *after* the scrub via ``build_subprocess_env``
            so they bypass the secret filter intentionally.
        harden: When True (default, fail-safe), prepend hardening ``-c``
            flags to the git invocation and add ``GIT_ATTR_NOSYSTEM=1`` to the
            environment.  Set False only for pure-plumbing calls where the
            flags are known to break the operation.
        isolate_config: When True (default), add ``GIT_CONFIG_NOSYSTEM=1``
            and ``GIT_CONFIG_GLOBAL=/dev/null`` to isolate from system/global
            git configuration.  Set False for host-context paths (UI merge,
            UI commit, worktree checkout) where operator global config such as
            git-lfs must be preserved.

    Returns:
        GitResult with returncode, stdout, stderr.

    Raises:
        GitError: If check=True and the process exits with a non-zero code.
    """
    from yukar.sandbox.env import build_subprocess_env

    # Build hardening env additions before merging with caller-supplied env.
    harden_env: dict[str, str] = {}
    if harden:
        # Tier B env vars: block system gitattributes (filter drivers, diff
        # drivers, merge drivers defined at the system level) for ALL harden=True
        # calls including host/UI paths (diff/merge/commit/checkout/status) where
        # isolate_config=False.  If we only set this inside isolate_config, the
        # external-driver surface is restored on the host-context code path.
        harden_env["GIT_ATTR_NOSYSTEM"] = "1"
    if isolate_config:
        # Tier C: isolate from system/global git config and system gitattributes.
        # Independent of Tier B (harden) so callers can request config isolation
        # without the full suite of -c hardening flags.
        harden_env["GIT_CONFIG_NOSYSTEM"] = "1"
        harden_env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        # GIT_ATTR_NOSYSTEM is also set here so that callers with
        # harden=False, isolate_config=True get the attribute isolation too.
        harden_env["GIT_ATTR_NOSYSTEM"] = "1"

    # Merge: harden_env first (lower priority), then caller-supplied env
    # (higher priority — e.g. GIT_AUTHOR_* must survive even if harden_env
    # had a key with the same name, which it currently does not).
    merged_extra: dict[str, str] = {**harden_env, **(env or {})}

    # Tier A: always scrub host secrets; git_author_env values survive via extra.
    full_env = build_subprocess_env(cwd=cwd, extra=merged_extra if merged_extra else None)

    # Tier B: prepend hardening top-level flags.
    # IMPORTANT: asyncio.create_subprocess_exec does NOT go through a shell, so
    # there is NO word splitting on the argv list.  Each ``-c key=value`` pair
    # MUST be passed as two separate list elements ("-c" and "key=value"),
    # otherwise git receives the entire string as a single unknown option and
    # exits with rc=129.
    if harden:
        from yukar.config.paths import empty_hooks_dir

        hooks_dir = str(empty_hooks_dir())
        # safe.directory scoped to the cwd prevents "detected dubious ownership"
        # errors when the repo is owned by a different uid (NFS mounts, bind
        # mounts, sudo-created checkouts, container setups).  We scope it to
        # cwd specifically — not "*" — so we only trust this one repo, not the
        # entire filesystem.  This replaces the operator's global safe.directory
        # list when isolate_config=True is active, so we must inject it ourselves.
        cwd_str = str(cwd)
        harden_flags: list[str] = [
            "--no-pager",
            "-c",
            f"core.hooksPath={hooks_dir}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.pager=cat",
            "-c",
            "core.sshCommand=false",
            "-c",
            "core.alternateRefsCommand=",
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
            "-c",
            f"safe.directory={cwd_str}",
        ]
        cmd: list[str] = ["git", *harden_flags, *args]
    else:
        cmd = ["git", *args]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=full_env,
        start_new_session=True,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=_GIT_TIMEOUT
        )
    except (TimeoutError, asyncio.CancelledError):
        # Kill the entire process group so credential helpers, ssh agents, and
        # filter programs spawned by git do not linger as orphans.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        # Always reap the process to avoid zombie entries, then re-raise.
        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    result = GitResult(
        returncode=proc.returncode or 0,
        stdout=_truncate(stdout_bytes),
        stderr=_truncate(stderr_bytes),
    )
    if check and not result.ok:
        raise GitError(result, cmd)
    return result
