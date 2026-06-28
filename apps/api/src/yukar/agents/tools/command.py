"""Worker run_command tool — scoped subprocess execution.

The ``make_command_tools(ctx)`` factory returns a single ``run_command``
Strands tool whose closure captures an ``AgentContext``.  Every invocation:

1. Validates the proposed working directory via ``ctx.path_guard``.
2. Applies the always-on baseline denylist (``check_default_denylist``) before
   any per-repo allow/deny check.  The baseline deny is operator-immutable —
   no allowlist entry can override it.  It operates on parsed argv tokens
   (no-shell assumption) and covers catastrophic commands (rm -rf /, dd to
   block devices, mkfs, shutdown, reboot, etc.) via best-effort token-level
   matching.  ``git`` is unconditionally denied regardless of the operator
   allowlist: all git operations must go through the dedicated git tools
   (``run_git``), which apply the full Tier B/C hardening pipeline.  This
   unconditional deny also makes wrapper-path git invocations safe: ``env git
   …``, ``xargs git …``, and ``sh -c "git …"`` are caught by the recursive
   denylist walker because ``git`` appears in ``_DANGEROUS_BINARIES``.  Shell
   wrapper and sh-c patterns receive an additional layer of inspection but are
   still best-effort; the primary gate is the fail-closed empty-default
   allowlist.
3. Normalises the first token to its basename (e.g. ``/bin/rm`` → ``rm``)
   and checks both the raw token and the basename against the allow/deny
   lists in ``ctx.command_config``.
4. If the allow list is empty, rejects all commands (fail-safe default).
5. Rejects absolute, home-relative, or parent-traversing path arguments that
   point outside the assigned worktree (``check_absolute_args``).  argv[0] is
   exempt (governed by the allowlist).  Configurable via ``forbid_absolute_args``.
6. Runs the command with ``asyncio.create_subprocess_exec`` (no shell;
   string commands are split with ``shlex.split``) using a sanitized
   environment (``build_subprocess_env``) that strips secret variables
   (API keys, tokens, AWS credentials, SSH agent sockets) before they can
   be exfiltrated by arbitrary project code.
7. Enforces a configurable timeout and output size cap.

The worker can never execute in a directory outside its worktree, and can
never run commands that are not in the allow list (or are in the deny list).

Shell wrappers (``sh``, ``bash``, ``env``) receive no special treatment:
they must appear in the allow list like any other command.  This is sufficient
because (a) the allowlist is now an explicit whitelist, and (b) ``run_command``
never uses a shell — ``create_subprocess_exec`` is used directly.
Evaluator write-safety is enforced by tool-set design: the Evaluator is only
given ``read_diff`` and ``run_tests`` (which delegates to ``run_command``),
not the fs-write tools.  The same allowlist rules apply.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
import signal
from pathlib import Path
from typing import Any

from strands import tool

from yukar.agents.context import AgentContext
from yukar.sandbox.env import build_subprocess_env
from yukar.sandbox.path_guard import PathGuardError

# Hard limits to prevent runaway processes.
_DEFAULT_TIMEOUT_SECONDS: float = 120.0
_MAX_OUTPUT_BYTES: int = 256 * 1024  # 256 KiB per stream


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Terminate *proc* and its entire process group, then drain.

    The subprocess is launched with ``start_new_session=True`` so it becomes the
    leader of a new process group whose id equals its pid.  Killing only the
    direct child (``proc.kill()``) leaves any grandchildren it forked alive;
    ``os.killpg(pgid, SIGKILL)`` reaps the whole tree.  POSIX-only (darwin +
    linux); there is no process-group concept on Windows, which yukar does not
    target for run execution.

    Always best-effort: the process may have already exited (``ProcessLookup``)
    or the platform may lack ``killpg`` — in which case we fall back to a plain
    ``proc.kill()`` and never raise.  After signalling we drain ``communicate()``
    so the transport closes and no zombie is left behind.
    """
    pid = proc.pid
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        # Group kill unavailable or process already gone — fall back to the
        # direct child kill (also a no-op if it has already exited).
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    with contextlib.suppress(Exception):
        await proc.communicate()

# ---------------------------------------------------------------------------
# Baseline denylist — always-on, operator allowlist cannot override
#
# Design constraints:
# - no-shell: tokens are already shlex-split argv, no shell expansion occurs.
# - Token-level matching only (no substring scanning of the command string).
# - best-effort: wrappers and sh -c patterns receive additional inspection
#   but the primary gate is the fail-closed empty-default allowlist.
# - deny precedes allow in run_command — operator cannot lift these rules.
# ---------------------------------------------------------------------------

# Wrapper commands whose payload may contain a dangerous binary.
_WRAPPER_BINARIES: frozenset[str] = frozenset(
    {
        "sudo",
        "doas",
        "env",
        "nice",
        "nohup",
        "time",
        "timeout",
        "stdbuf",
        "setsid",
        "ionice",
        "xargs",
    }
)

# Dangerous binaries to scan for inside wrappers.
_DANGEROUS_BINARIES: frozenset[str] = frozenset(
    {
        "rm",
        "dd",
        "mkfs",
        "chmod",
        "chown",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "init",
        "git",
    }
)

# Shell interpreters whose -c argument receives a second parse pass.
_SHELL_BINARIES: frozenset[str] = frozenset({"sh", "bash", "zsh", "dash", "ksh"})


def _has_recursive_flag(tokens: list[str]) -> bool:
    """Return True if *tokens* contain a recursive flag.

    Recognises ``-r``, ``-R``, ``--recursive``, and combined short-flag bundles
    that include ``r`` or ``R`` (e.g. ``-rf``, ``-fr``, ``-Rf``, ``-fR``).
    Only checks positional flags, not values (tokens after ``=``).
    ``--``-prefixed long options are not bundled, so only ``--recursive`` matches.
    """
    for tok in tokens:
        if tok == "--recursive":
            return True
        # Short flag bundle: starts with '-' but not '--', may contain r/R.
        if tok.startswith("-") and not tok.startswith("--") and ("r" in tok[1:] or "R" in tok[1:]):
            return True
    return False


def _is_root_or_home_target(arg: str) -> bool:
    """Return True if *arg* looks like a root-level or home directory target.

    Home patterns (always dangerous):
        ~, ~/, $HOME, ${HOME}, ~<username> (any arg starting with '~')

    Absolute path patterns (dangerous when depth <= 1 after normalisation):
        /           depth 0
        /etc        depth 1  (one meaningful component under /)
        //          normalises to /
        /.          normalises to /
        /*          depth 1 (wildcard at root level)
        /usr/       depth 1 after stripping trailing slash

    Relative paths (``build/``, ``./node_modules``, ``dist``) are NOT dangerous.
    Absolute paths with depth >= 2 (``/home/user/project``) are NOT dangerous.
    """
    # Home directory patterns.
    if arg.startswith("~"):
        return True
    if arg in ("$HOME", "${HOME}"):
        return True

    # Must be absolute to be dangerous via path depth.
    if not arg.startswith("/"):
        return False

    # Normalise and compute depth.
    norm = os.path.normpath(arg)
    # normpath('//') → '/', normpath('/.') → '/', etc.
    if norm == "/":
        return True

    # Split into parts and count components below root.
    # depth 1: e.g. /etc → ['etc'] — only one component under root.
    parts = norm.lstrip("/").split("/")
    return len(parts) <= 1


def _check_single_command(argv: list[str]) -> str | None:
    """Check *argv* (argv[0] = binary) against the baseline denylist.

    Returns a human-readable denial reason if the command is dangerous,
    or ``None`` if it passes.

    Rules (basename-based, no-shell):
    - shutdown / reboot / halt / poweroff → always deny.
    - init with argument 0 or 6 → deny.
    - rm: ``--no-preserve-root`` → deny; recursive + root/home target → deny.
    - dd: any argument starting with ``of=/dev/`` → deny.
    - mkfs or mkfs.* → deny.
    - chmod / chown: recursive + root/home target → deny.
    - git: unconditionally denied regardless of the operator allowlist.  All
      git operations must use the dedicated git tools (``run_git``), which
      apply the full Tier B/C hardening pipeline.  ``git`` also appears in
      ``_DANGEROUS_BINARIES`` so the wrapper scanner catches ``env git …``,
      ``xargs git …``, and ``sh -c "git …"`` via the recursive walker.
    """
    if not argv:
        return None

    cmd = Path(argv[0]).name  # basename

    # --- Shutdown family ---
    if cmd in ("shutdown", "reboot", "halt", "poweroff"):
        return f"baseline deny: {cmd!r} is not permitted"

    # --- init with runlevel 0 or 6 ---
    if cmd == "init" and any(a in ("0", "6") for a in argv[1:]):
        return "baseline deny: 'init 0/6' (system halt/reboot) is not permitted"

    # --- rm ---
    if cmd == "rm":
        non_flag_args = [a for a in argv[1:] if not a.startswith("-")]
        if "--no-preserve-root" in argv:
            return "baseline deny: 'rm --no-preserve-root' is not permitted"
        if _has_recursive_flag(argv[1:]) and any(_is_root_or_home_target(a) for a in non_flag_args):
            return "baseline deny: recursive rm targeting root or home is not permitted"

    # --- dd ---
    if cmd == "dd":
        for arg in argv[1:]:
            if arg.startswith("of=/dev/"):
                return f"baseline deny: 'dd {arg}' writes to a block device"

    # --- mkfs (and mkfs.*) ---
    if cmd == "mkfs" or cmd.startswith("mkfs."):
        return f"baseline deny: {cmd!r} (filesystem creation) is not permitted"

    # --- chmod / chown ---
    if cmd in ("chmod", "chown"):
        non_flag_args = [a for a in argv[1:] if not a.startswith("-")]
        if _has_recursive_flag(argv[1:]) and any(_is_root_or_home_target(a) for a in non_flag_args):
            return f"baseline deny: recursive {cmd!r} targeting root or home is not permitted"

    # --- git: unconditionally denied via run_command.
    #     All git operations must go through the dedicated run_git tools,
    #     which apply Tier B/C hardening.  The operator allowlist cannot
    #     override this denial.
    if cmd == "git":
        return (
            "baseline deny: git is not permitted via run_command; "
            "use the dedicated git tools (run_git)"
        )

    return None


def check_default_denylist(tokens: list[str], _depth: int = 0) -> str | None:
    """Check parsed argv *tokens* against the always-on baseline denylist.

    This function is the first line of defence in ``run_command``.  It is
    called **before** the per-repo allow/deny check and cannot be overridden
    by an operator allowlist.

    Three-layer inspection (no-shell, argv-token-level, best-effort):

    (1) Direct command — ``_check_single_command(tokens)``.
    (2) Wrapper commands (``sudo``, ``env``, ``xargs``, etc.) — scan tokens for
        the first occurrence of a known dangerous binary *or* shell binary and
        recursively call ``check_default_denylist`` on the remaining slice so
        that nested wrappers and shell ``-c`` patterns are also evaluated.
    (3) Shell with ``-c`` — ``sh``, ``bash``, etc.: find the ``-c`` flag
        (including bundles like ``-lc``), parse the next token with
        ``shlex.split``, then recursively call ``check_default_denylist`` so
        that inner wrappers and further nested shells are also evaluated.

    Layers (2) and (3) invoke ``check_default_denylist`` recursively with
    ``_depth + 1``.  When ``_depth`` exceeds 4 the recursion bottoms out and
    only ``_check_single_command`` is applied, preventing unbounded recursion.

    Returns a non-None denial reason string if the command is blocked;
    returns ``None`` if no baseline rule matches.

    Note: best-effort — ``&&``, ``;``, and pipe operators inside ``-c``
    strings are not interpreted (only the first argv is evaluated after
    shlex-split).  The primary gate is the fail-closed empty-default
    allowlist; baseline deny is a defence-in-depth layer for common
    catastrophic patterns.
    """
    # Depth guard: avoid unbounded recursion from adversarially nested input.
    if _depth > 4:
        return _check_single_command(tokens)

    if not tokens:
        return None

    # Layer (1): direct command check.
    result = _check_single_command(tokens)
    if result is not None:
        return result

    first = Path(tokens[0]).name

    # Layer (2): wrapper — scan for the first dangerous or shell binary in the
    # payload and recurse so that nested wrappers / shell -c patterns are also
    # evaluated at full depth.
    if first in _WRAPPER_BINARIES:
        for j, tok in enumerate(tokens):
            if j == 0:
                continue
            tok_base = Path(tok).name
            is_dangerous = tok_base in _DANGEROUS_BINARIES or (
                tok_base.startswith("mkfs.") and "mkfs" in _DANGEROUS_BINARIES
            )
            is_shell = tok_base in _SHELL_BINARIES
            if is_dangerous or is_shell:
                payload_result = check_default_denylist(tokens[j:], _depth + 1)
                if payload_result is not None:
                    return f"baseline deny (via {first!r} wrapper): {payload_result}"
                break

    # Layer (3): shell with -c — parse the inline script token and recurse so
    # that inner wrappers and nested shell invocations are also evaluated.
    if first in _SHELL_BINARIES:
        for k, tok in enumerate(tokens[1:], start=1):
            # Match -c alone, or bundles like -lc, -ce, -lce (tok starts with '-'
            # but not '--', and 'c' appears somewhere after the initial dash).
            if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
                # The next token (if present) is the inline script.
                if k + 1 < len(tokens):
                    try:
                        inner = shlex.split(tokens[k + 1])
                    except ValueError:
                        inner = []
                    if inner:
                        shell_result = check_default_denylist(inner, _depth + 1)
                        if shell_result is not None:
                            return f"baseline deny (via {first!r} -c): {shell_result}"
                break

    return None


# key=value option / assignment forms whose value must also be inspected
# (covers --out=/x, -o=/x, and bare positional forms like if=/x, PREFIX=/x).
# The key must look like an identifier so URLs / arbitrary text are not split.
_KEY_VALUE_RE = re.compile(r"^-{0,2}[A-Za-z_][A-Za-z0-9_.-]*=(.*)$")


def _resolve_real(path_str: str) -> str:
    """Best-effort realpath that never raises.

    Resolves symlinks in the existing leading components and normalises the
    rest.  Using realpath (not just normpath) defeats in-tree symlinks that
    point outside the worktree.
    """
    try:
        return os.path.realpath(path_str)
    except OSError:
        return os.path.normpath(path_str)


def _under_allowed_prefix(real_path: str, allowed_real_prefixes: tuple[str, ...]) -> bool:
    """Return True if *real_path* (already realpath'd) is one of, or nested
    under, *allowed_real_prefixes* (also realpath'd)."""
    for base in allowed_real_prefixes:
        # A '/' prefix means "allow all absolute paths" -- a deliberate opt-out.
        if base == "/":
            return True
        if real_path == base or real_path.startswith(base + "/"):
            return True
    return False


def _has_parent_traversal(path_str: str) -> bool:
    """True if *path_str* contains a ``..`` path component (e.g. ../x, a/../../b).

    Splits on '/' so revision ranges like ``main..feature`` or ``HEAD~2..HEAD``
    (no '/'-delimited '..' segment) are NOT treated as traversal.
    """
    return ".." in path_str.split("/")


# Pattern for an absolute/home path ATTACHED to a short option without '='.
# Captures the leading path character ('/' or '~') at the start of the
# embedded path, which begins right after the option letters (and optional '@').
# Forms covered (A2-01):
#   -I/etc/passwd       → short-opt attached absolute
#   -I~/src             → short-opt attached home-relative
#   -d@/etc/passwd      → curl --data-binary @-form attached absolute
#   -d@~/file           → curl --data-binary @-form attached home-relative
# The capture group is the embedded path including its leading '/' or '~'.
_ATTACHED_SHORT_OPT_RE = re.compile(r"^-[A-Za-z]+@?([/~].*)$")


def _arg_escapes_worktree(arg: str, allowed_real_prefixes: tuple[str, ...], cwd_real: str) -> bool:
    """Return True if *arg* is an absolute, home-relative, or parent-traversing
    path outside the allowed prefixes.

    Inspects the raw token and -- for identifier-keyed ``key=value`` forms
    (``--out=/x``, ``-o=/x``, ``if=/x``, ``PREFIX=/x``) -- the value too.
    Also catches paths attached directly to a short option without '=' (A2-01):
    e.g. ``-I/etc/passwd``, ``-d@/abs/path``, ``-d@~/x``.
    Home forms (``~``, ``$HOME``, ``${HOME}``) are blocked defensively: they
    are literal under no-shell, but some tools self-expand them.  Absolute
    paths are blocked unless their realpath resolves under an allowed prefix
    (realpath defeats both ``<root>/../../etc`` and in-tree-symlink escapes).
    Relative paths containing a ``..`` path component (e.g. ``../x``,
    ``a/../../b``) are resolved against *cwd_real* and rejected if they escape;
    plain relative paths without ``..`` stay under cwd which is under the
    worktree and are always accepted.
    """
    candidates = [arg]
    match = _KEY_VALUE_RE.match(arg)
    if match is not None:
        candidates.append(match.group(1))
    # Extract path embedded in an attached short option without '=': -I/path, -d@/path (A2-01).
    attached = _ATTACHED_SHORT_OPT_RE.match(arg)
    if attached is not None:
        candidates.append(attached.group(1))
    for cand in candidates:
        if not cand:
            continue
        if cand.startswith("~"):
            return True
        if cand in ("$HOME", "${HOME}") or cand.startswith(("$HOME/", "${HOME}/")):
            return True
        if cand.startswith("/"):
            real = _resolve_real(cand)
            if not _under_allowed_prefix(real, allowed_real_prefixes):
                return True
        elif _has_parent_traversal(cand):
            real = _resolve_real(os.path.join(cwd_real, cand))
            if not _under_allowed_prefix(real, allowed_real_prefixes):
                return True
    return False


def check_absolute_args(
    tokens: list[str], allowed_prefixes: tuple[str, ...], cwd: str
) -> str | None:
    """Return a denial reason if any argument (after argv[0]) is an absolute,
    home-relative, or parent-traversing path outside *allowed_prefixes*.

    argv[0] (the executable) is exempt -- it is governed by the allow/deny list
    and absolute executable paths like ``/usr/bin/python`` are legitimate.

    Conservative by design: an argument that merely *looks* like an
    absolute/home path -- e.g. a commit message or regex starting with ``/``
    -- is rejected even though no file is opened.  This is fail-safe; operators
    loosen it per repo via ``forbid_absolute_args=False`` or by extending
    ``allowed_absolute_arg_prefixes``.  Containment is textual + realpath-based,
    not a syscall sandbox.

    Residual best-effort gaps (textual guard; the real boundary is a disposable
    execution environment): an absolute path embedded in an attached short option
    without '=' (e.g. -I/usr/include); the value of a non-identifier key=value
    token; an absolute path after a second '=' in a multi-'=' token (the value is
    captured whole after the FIRST '=', so key=junk=/etc/x is not flagged); and
    path arguments buried inside a shell -c payload (e.g. when 'sh'/'bash' is
    allowlisted) -- this guard only inspects the top-level argv, not the inner
    script.  Operators harden further via forbid_absolute_args /
    allowed_absolute_arg_prefixes, by not allowlisting shell wrappers, or by
    running autonomous loops in a disposable sandbox.
    """
    cwd_real = _resolve_real(cwd)
    allowed_real_prefixes = tuple(_resolve_real(p) for p in allowed_prefixes)
    for arg in tokens[1:]:
        if _arg_escapes_worktree(arg, allowed_real_prefixes, cwd_real):
            return (
                f"argument {arg!r} points outside the worktree "
                "(absolute, home, or parent-escaping relative paths are not permitted by default)"
            )
    return None


def make_command_tools(
    ctx: AgentContext,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    *,
    forbid_absolute_args: bool = True,
    allowed_absolute_arg_prefixes: tuple[str, ...] | None = None,
) -> list[Any]:
    """Return a [run_command] tool list bound to *ctx*'s worktree.

    Args:
        ctx: Agent context (determines cwd root and allow/deny lists).
        timeout: Maximum wall-clock seconds for any single subprocess.
        forbid_absolute_args: If ``True`` (default), reject arguments after
            argv[0] that are absolute or home-relative paths outside the
            worktree.  Set to ``False`` to disable (e.g. for trusted callers
            that need to pass a controlled absolute path).
        allowed_absolute_arg_prefixes: Explicit tuple of absolute path prefixes
            that are permitted as arguments even when ``forbid_absolute_args``
            is ``True``.  Defaults to ``(str(ctx.path_guard.root),)``.

    Returns:
        A one-element list containing the ``run_command`` Strands tool.
    """

    @tool
    async def run_command(command: str, cwd: str = ".") -> dict[str, Any]:
        """Execute a shell command inside the worktree.

        The command is run with ``asyncio.create_subprocess_exec`` (no shell
        expansion).  The working directory is validated against the worktree
        sandbox; the command's first token is checked against the repo's
        allow/deny list (empty allow list = deny all).

        Args:
            command: The command line to execute (will be split with
                ``shlex.split``).  Must not use shell features — the command
                is run directly without a shell.
            cwd: Working directory relative to (or inside) the worktree.
                Defaults to the worktree root (``"."``).

        Returns:
            A dict with ``"stdout"``, ``"stderr"``, ``"returncode"``, and
            ``"status"`` (``"success"`` / ``"error"``).
        """
        # Validate cwd.
        try:
            resolved_cwd = ctx.path_guard.check_cwd(cwd)
        except PathGuardError as exc:
            return {"status": "error", "content": [{"text": f"cwd error: {exc}"}]}

        # Split the command.
        try:
            tokens = shlex.split(command)
        except ValueError as exc:
            return {
                "status": "error",
                "content": [{"text": f"Invalid command syntax: {exc}"}],
            }

        if not tokens:
            return {"status": "error", "content": [{"text": "Empty command"}]}

        # Baseline denylist — always-on, evaluated before the per-repo allow/deny
        # check.  Operator allowlists cannot override these rules.  No-shell
        # assumption: tokens are already split by shlex above.
        # See check_default_denylist docstring for full description and caveats.
        baseline_err = check_default_denylist(tokens)
        if baseline_err is not None:
            return {"status": "error", "content": [{"text": baseline_err}]}

        first_token = tokens[0]

        # Check allow/deny (RepoCommandConfig checks both raw token and basename).
        if not ctx.command_config.is_allowed(first_token):
            return {
                "status": "error",
                "content": [
                    {
                        "text": (
                            f"Command {first_token!r} is not permitted in this "
                            f"worktree (allow={list(ctx.command_config.allow)!r}, "
                            f"deny={list(ctx.command_config.deny)!r})"
                        )
                    }
                ],
            }

        # Reject absolute / home-path arguments that escape the worktree.
        # Placed after allow/deny so their messages take precedence;
        # argv[0] is exempt (governed by the allow list).
        if forbid_absolute_args:
            # Compute allowed_prefixes lazily here so the factory does not
            # access ctx.path_guard until the tool is actually invoked.
            effective_prefixes: tuple[str, ...] = (
                (str(ctx.path_guard.root),)
                if allowed_absolute_arg_prefixes is None
                else allowed_absolute_arg_prefixes
            )
            abs_err = check_absolute_args(tokens, effective_prefixes, str(resolved_cwd))
            if abs_err is not None:
                return {"status": "error", "content": [{"text": abs_err}]}

        # Run the subprocess.
        #
        # ``start_new_session=True`` runs the child in its own session/process
        # group (it calls ``setsid()`` in the child).  This lets the timeout and
        # cancellation paths terminate the *whole* tree via ``os.killpg`` so that
        # grandchildren the command spawned (e.g. a build that forks workers) do
        # not survive a single ``proc.kill()`` on the direct child.  setsid /
        # killpg are POSIX and work on darwin and linux alike.
        try:
            proc = await asyncio.create_subprocess_exec(
                *tokens,
                cwd=str(resolved_cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=build_subprocess_env(cwd=resolved_cwd),
                start_new_session=True,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except TimeoutError:
                # Kill the entire process group so any grandchildren the command
                # spawned are reaped too, then drain to avoid a zombie.
                await _kill_process_group(proc)
                return {
                    "status": "error",
                    "content": [{"text": (f"Command timed out after {timeout}s: {command!r}")}],
                    "returncode": -1,
                }
            except asyncio.CancelledError:
                # Cancellation (pause/stop): kill the whole group, drain, and
                # re-raise so the supervisor's stop/cancel path still propagates.
                await _kill_process_group(proc)
                raise
        except FileNotFoundError:
            return {
                "status": "error",
                "content": [{"text": f"Executable not found: {first_token!r}"}],
            }
        except OSError as exc:
            return {
                "status": "error",
                "content": [{"text": f"OS error running command: {exc}"}],
            }

        # Truncate large outputs.
        def _decode(b: bytes) -> str:
            truncated = b[:_MAX_OUTPUT_BYTES]
            text = truncated.decode("utf-8", errors="replace")
            if len(b) > _MAX_OUTPUT_BYTES:
                text += f"\n... [truncated {len(b) - _MAX_OUTPUT_BYTES} bytes]"
            return text

        stdout = _decode(stdout_bytes)
        stderr = _decode(stderr_bytes)
        rc = proc.returncode or 0
        status = "success" if rc == 0 else "error"

        result_text = f"Exit code: {rc}\n"
        if stdout:
            result_text += f"stdout:\n{stdout}\n"
        if stderr:
            result_text += f"stderr:\n{stderr}\n"

        return {
            "status": status,
            "content": [{"text": result_text}],
            "stdout": stdout,
            "stderr": stderr,
            "returncode": rc,
        }

    return [run_command]
