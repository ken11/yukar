"""Worker/Evaluator grep tool — literal full-text search inside the worktree.

``make_grep_tools(ctx)`` returns a single ``repo_grep`` Strands tool whose
closure captures an ``AgentContext``.

repo_grep searches the live worktree using ripgrep (rg) so results always
reflect the most recent file state.  The pattern is matched as a FIXED STRING
by default (``rg -F``); ``regex=True`` switches to ripgrep's Rust regex
syntax.  Use it when you need:
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


def _validate_pattern(pattern: str) -> str | None:
    """Return an actionable error message for patterns that can never match.

    Control characters (other than tab) never appear in source text, so a
    pattern containing one is almost always the visible symptom of an escaping
    accident in the JSON tool-call layer (e.g. a regex word boundary ``\\b``
    arriving as the JSON backspace escape).  Failing loudly with the fix beats
    silently returning 0 matches.
    """
    if "\x08" in pattern:
        return (
            "pattern contains a literal backspace (U+0008) — a regex word boundary "
            '`\\b` probably lost its backslash in JSON encoding ("\\b" is the JSON '
            'backspace escape). Send the pattern with the backslash doubled ("\\\\b") '
            "and retry."
        )
    if "\n" in pattern or "\r" in pattern:
        return (
            "pattern contains a newline — repo_grep matches within a single line "
            "only. Search for a one-line fragment instead."
        )
    bad = {c for c in pattern if (ord(c) < 0x20 and c != "\t") or ord(c) == 0x7F}
    if bad:
        codes = ", ".join(f"U+{ord(c):04X}" for c in sorted(bad))
        return (
            f"pattern contains control character(s) {codes} that never appear in "
            "source text — it was likely mangled by string escaping. Double any "
            "backslashes and retry."
        )
    return None


def _zero_match_hint(pattern: str, regex: bool) -> str | None:
    """Return a self-correction hint for a 0-match result, or ``None``.

    A backslash in the pattern is the most common cause of a false miss:
    in literal mode it is searched as a real backslash character, and in
    regex mode a doubled backslash matches a real backslash character —
    both usually mean the caller escaped a pattern that needed no escaping.
    """
    if not regex and "\\" in pattern:
        return (
            "note: the pattern was matched LITERALLY — `\\` is a real backslash "
            "character, so e.g. `foo\\(` searched for the text `foo\\(`, not `foo(`. "
            "If the backslash was meant as regex escaping, drop it (or pass "
            "regex=true)."
        )
    if regex and "\\\\" in pattern:
        return (
            "note: `\\\\` in a regex matches a literal backslash character. If you "
            "double-escaped (e.g. `foo\\\\(bar\\\\)` to find `foo(bar)`), use a single "
            "backslash per metacharacter: `foo\\(bar\\)`."
        )
    return None


async def grep_worktree(
    ctx: AgentContext,
    pattern: str,
    path: str = ".",
    max_results: int = 200,
    context: int = 0,
    regex: bool = False,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run ripgrep over *ctx*'s worktree (read-only core).

    *pattern* is matched as a fixed string (``rg -F``) unless *regex* is
    ``True``, in which case it is a ripgrep (Rust regex) pattern.

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

    pattern_err = _validate_pattern(pattern)
    if pattern_err is not None:
        return make_error(pattern_err, results=[])

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
        # rg omits the filename field when given a single explicit file target,
        # which would leave 2-field match lines the parser below must discard.
        # Force the 3-field "path<SEP>lineno<SEP>text" shape unconditionally.
        "--with-filename",
        f"--field-match-separator={_MATCH_SEP}",
        *([] if regex else ["-F"]),
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
    if rc == 0 and n == 0 and not truncated:
        # rg's exit code says at least one line matched, yet none survived
        # parsing — a parser/output-shape desync.  Surface it loudly instead
        # of reporting a false "0 match(es)" (the exact lie this tool once
        # told for single-file path targets).
        return make_error(
            "internal error: rg found matches but none could be parsed from its "
            "output — report this as a repo_grep bug.",
            results=[],
        )
    summary = f"{n} match(es)" + (" (truncated)" if truncated else "")
    if n == 0:
        hint = _zero_match_hint(pattern, regex)
        if hint is not None:
            summary += "\n" + hint
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
        regex: bool = False,
    ) -> dict[str, Any]:
        r"""Search the worktree for an exact text fragment (default) or a regex.

        By default the pattern is matched as a LITERAL string (ripgrep ``-F``):
        paste the text exactly as it appears in the file.  Every character —
        including ``( ) [ ] { } | . * + ? \`` — is matched as-is, so do NOT
        add any escaping (``foo\(`` would search for a real backslash).

        Set ``regex=True`` to interpret the pattern as a regular expression.
        The engine is ripgrep's Rust regex — NOT plain grep (BRE) and not PCRE:

        - ``\d`` ``\s`` ``\w`` ``\b``, ``(a|b)``, ``+`` ``?`` ``{n,m}`` work as
          in Python/JS.
        - ``(`` ``)`` ``|`` are metacharacters WITHOUT a backslash; ``\(`` and
          ``\|`` match the literal characters (the opposite of grep's BRE
          dialect, where ``\(`` means a group).
        - Escape a metacharacter with ONE backslash (``hoge\(`` finds
          ``hoge(``).  A doubled backslash (``hoge\\(``) matches a real
          backslash character in the file — a common accidental miss.
        - Look-around, back-references and multi-line patterns are not
          supported; they return an explicit error (never a silent 0).

        Patterns match within a single line, case-sensitively.  Returns the
        matching lines themselves as ``path:lineno:text`` (not just a count),
        optionally with surrounding lines of context.

        Searches the live worktree files directly — results always reflect the
        most recent edits (repo_search / repo_summarize use a FAISS index that
        may not have caught up yet).  Like ripgrep, the search skips gitignored
        files, hidden files (dotfiles), and binary files.

        Args:
            pattern: Text to search for.  A literal string by default; a Rust
                regex when ``regex=True``.  Passed to rg via ``-e`` so it
                cannot be confused with a flag.
            path: Sub-path inside the worktree to restrict the search to.
                Defaults to ``"."`` (the entire worktree).  Paths that escape
                the worktree boundary are rejected with an error.
            max_results: Maximum number of matching lines to return.
                Defaults to 200.  Excess lines are discarded with
                ``truncated=True`` in the response.
            context: Number of surrounding lines to show before and after each
                match (like ``rg -C``).  Defaults to 0 (match lines only);
                capped at 10.  Use 2-3 to see the code around each match.
            regex: When ``True``, treat the pattern as a Rust regex (see
                above).  Defaults to ``False`` (literal match).

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
        return await grep_worktree(
            ctx, pattern, path, max_results, context, regex, timeout=timeout
        )

    return [repo_grep]
