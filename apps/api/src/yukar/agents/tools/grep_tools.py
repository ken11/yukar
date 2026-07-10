"""Worker/Evaluator grep tool — literal full-text search inside the worktree.

``make_grep_tools(ctx)`` returns a single ``repo_grep`` Strands tool whose
closure captures an ``AgentContext``.

repo_grep searches the live worktree using ripgrep (rg) so results always
reflect the most recent file state.  Use it when you need:
- Exact / literal text matching of code you just wrote.
- Verifying a string, symbol, or pattern actually appears in the worktree.

Use repo_search / repo_summarize for semantic / structural exploration;
their FAISS index may lag behind the latest edits.

Tool
----
- ``repo_grep`` — ripgrep search scoped to the assigned worktree.
"""

from __future__ import annotations

import asyncio
from typing import Any

from strands import tool

from yukar.agents.context import AgentContext
from yukar.agents.tools.command import (
    _DEFAULT_TIMEOUT_SECONDS,
    _MAX_OUTPUT_BYTES,
    _kill_process_group,
)
from yukar.agents.tools.response_builder import make_error, make_success
from yukar.sandbox.env import build_subprocess_env
from yukar.sandbox.path_guard import PathGuardError

# Unit separator — cannot appear in file paths and effectively never in source
# text, so match lines split unambiguously even when context lines are present
# (context lines keep rg's default "path-lineno-text" form, which CAN look like
# "str:int:rest" and must not be mistaken for a match).
_MATCH_SEP = "\x1f"

# Upper bound for the context-lines option (rg -C) — keeps output bounded.
_MAX_CONTEXT_LINES = 10


async def grep_worktree(
    ctx: AgentContext,
    pattern: str,
    path: str = ".",
    max_results: int = 200,
    context: int = 0,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run ripgrep over *ctx*'s worktree (read-only core).

    Shared by the single-repo ``repo_grep`` tool and the multi-repo overview
    ``repo_grep`` (which resolves a per-repo ctx first), so there is exactly one
    implementation of the search + containment logic.  All paths are validated
    through ``ctx.path_guard`` so the search root can never escape the worktree.

    The matched lines themselves (``path:lineno:text``, plus surrounding lines
    when *context* > 0) are rendered into the ``content`` text — that is the
    only part of the response the LLM can see, so a bare match count would be
    useless to it.

    Returns a ``make_success``/``make_error`` dict (see ``repo_grep`` docstring).
    """
    worktree = ctx.worktree_path
    context = max(0, min(context, _MAX_CONTEXT_LINES))

    # Validate search root through path_guard (same containment as fs_read).
    try:
        resolved = ctx.path_guard.resolve(path)
    except PathGuardError as exc:
        return make_error(f"path error: {exc}", results=[])

    # Convert resolved absolute path to worktree-relative so rg (run with
    # cwd=worktree) never receives an absolute argument that the sandbox
    # hasn't validated.
    try:
        rel = str(resolved.relative_to(worktree))
    except ValueError:
        rel = "."

    # Build argv — pattern is always after -e, search path after -- so
    # neither can inject rg options.
    argv = [
        "rg",
        "--no-config",
        "--color=never",
        "--line-number",
        "--no-heading",
        f"--field-match-separator={_MATCH_SEP}",
        *(["-C", str(context)] if context > 0 else []),
        "-e",
        pattern,
        "--",
        rel if rel else ".",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(worktree),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=build_subprocess_env(cwd=worktree),
            start_new_session=True,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except TimeoutError:
            await _kill_process_group(proc)
            return make_error(f"repo_grep timed out after {timeout}s", results=[])
        except asyncio.CancelledError:
            await _kill_process_group(proc)
            raise

    except FileNotFoundError:
        return make_error(
            "ripgrep (rg) is not installed on this host. Install ripgrep to use repo_grep.",
            results=[],
        )

    rc = proc.returncode
    # rg exit codes: 0 = match found, 1 = no match (not an error), 2+ = error.
    if rc is not None and rc >= 2:
        stderr_text = stderr_bytes[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        return make_error(f"rg error (rc={rc}): {stderr_text.strip()}", results=[])

    # rc == 0 or rc == 1 (no match) — decode stdout.
    raw = stdout_bytes[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    lines = [ln for ln in raw.splitlines() if ln]

    results: list[dict[str, Any]] = []
    display_lines: list[str] = []
    truncated = False

    for raw_line in lines:
        if _MATCH_SEP not in raw_line:
            # Context line ("path-lineno-text") or group separator ("--") —
            # only emitted when context > 0.  Pass through for display.
            display_lines.append(raw_line)
            continue
        if len(results) >= max_results:
            truncated = True
            break
        # Match line: "path<SEP>lineno<SEP>text" (see _MATCH_SEP).
        parts = raw_line.split(_MATCH_SEP, 2)
        if len(parts) < 3:
            continue
        try:
            line_no = int(parts[1])
        except ValueError:
            continue
        results.append({"path": parts[0], "line": line_no, "text": parts[2]})
        display_lines.append(f"{parts[0]}:{line_no}:{parts[2]}")

    n = len(results)
    summary = f"{n} match(es)" + (" (truncated)" if truncated else "")
    text = summary if n == 0 else summary + "\n" + "\n".join(display_lines)
    return make_success(text, results=results, truncated=truncated)


def make_grep_tools(
    ctx: AgentContext,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> list[Any]:
    """Return [repo_grep] tool bound to *ctx*'s worktree.

    The returned tool searches the live worktree with ripgrep.  All paths are
    validated through ``ctx.path_guard`` so the search root can never escape
    the assigned worktree — the same containment model as ``fs_read``.

    Args:
        ctx: Agent context (worktree_path, path_guard).
        timeout: Maximum seconds to wait for rg to complete.

    Returns:
        A one-element list containing the ``repo_grep`` Strands tool.
    """

    @tool
    async def repo_grep(
        pattern: str,
        path: str = ".",
        max_results: int = 200,
        context: int = 0,
    ) -> dict[str, Any]:
        """Search the worktree for a literal or regex pattern using ripgrep.

        Returns the matching lines themselves as ``path:lineno:text`` (not just
        a count), optionally with surrounding lines of context.

        Searches the live worktree files directly — results always reflect the
        most recent edits (repo_search / repo_summarize use a FAISS index that
        may not have caught up yet).  Use repo_grep to confirm that code you
        just wrote is present with the exact text expected.

        The search respects ``.gitignore`` rules by default (ripgrep's standard
        behaviour), so node_modules, .venv, and other ignored directories are
        automatically excluded — consistent with fs_read / fs_list.

        Args:
            pattern: Regex or literal pattern to search for.  Passed to rg via
                ``-e`` so the pattern cannot be confused with a flag.
            path: Sub-path inside the worktree to restrict the search to.
                Defaults to ``"."`` (the entire worktree).  Paths that escape
                the worktree boundary are rejected with an error.
            max_results: Maximum number of matching lines to return.
                Defaults to 200.  Excess lines are discarded with
                ``truncated=True`` in the response.
            context: Number of surrounding lines to show before and after each
                match (like ``rg -C``).  Defaults to 0 (match lines only);
                capped at 10.  Use 2-3 to see the code around each match.

        Returns:
            A dict with:
            - ``"status"``: ``"success"`` or ``"error"``.
            - ``"content"``: list of ``{"text": ...}`` — match count followed
              by the matching lines (``path:lineno:text``), interleaved with
              context lines when *context* > 0.
            - ``"results"``: list of ``{"path": str, "line": int, "text": str}``
              (empty on error or no match).
            - ``"truncated"``: ``True`` when more matches existed than
              *max_results* (only present on success).
        """
        return await grep_worktree(ctx, pattern, path, max_results, context, timeout=timeout)

    return [repo_grep]
