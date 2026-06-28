"""Git status — list changed files with +/- line counts."""

from __future__ import annotations

from pathlib import Path

from yukar.git.runner import parse_numstat, run_git
from yukar.models.diff import FileStat


def _unquote_git_path(raw: str) -> str:
    """Remove surrounding double-quotes and unescape git's octal/backslash encoding.

    git status --porcelain (without ``-z``) quotes paths that contain non-ASCII
    or special chars as ``"path"`` and encodes the bytes of a non-ASCII filename
    as a run of octal escapes (``\\343\\203\\225`` …), one escape per UTF-8 byte.

    Decoding must collect the **whole consecutive byte run** and decode it as
    UTF-8 in one step.  Decoding each ``\\ooo`` escape independently to a
    codepoint (``chr(byte)``) produces per-byte mojibake (e.g. ``Ã£Â``) for any
    multibyte character.  We therefore accumulate into a ``bytearray`` and decode
    once, with ``errors="replace"`` so a malformed sequence never raises.

    Used as a defensive fallback only — ``get_status`` reads porcelain with
    ``-z`` (raw, unquoted paths), so this path is exercised for legacy / direct
    callers and matches numstat keys round-trip.
    """
    stripped = raw.strip()
    if not (stripped.startswith('"') and stripped.endswith('"')):
        return stripped

    inner = stripped[1:-1]
    out = bytearray()
    i = 0
    n = len(inner)
    while i < n:
        ch = inner[i]
        if ch == "\\" and i + 1 < n:
            nxt = inner[i + 1]
            if nxt in ('"', "\\", "/"):
                out.extend(nxt.encode("utf-8"))
                i += 2
            elif nxt in "abfnrtv":
                # Standard C escapes git may emit.
                out.append(
                    {
                        "a": 0x07,
                        "b": 0x08,
                        "f": 0x0C,
                        "n": 0x0A,
                        "r": 0x0D,
                        "t": 0x09,
                        "v": 0x0B,
                    }[nxt]
                )
                i += 2
            elif nxt in "01234567" and i + 3 < n:
                # Octal: \ooo — append the raw byte so a multi-byte UTF-8
                # sequence (consecutive escapes) decodes correctly below.
                try:
                    out.append(int(inner[i + 1 : i + 4], 8))
                    i += 4
                except ValueError:
                    out.extend(ch.encode("utf-8"))
                    i += 1
            else:
                out.extend(ch.encode("utf-8"))
                i += 1
        else:
            out.extend(ch.encode("utf-8"))
            i += 1
    return out.decode("utf-8", errors="replace")


async def get_status(repo_path: Path, *, isolate_config: bool = True) -> list[FileStat]:
    """Return a list of changed files in the working tree.

    Uses ``git status --porcelain -z`` for the file list and
    ``git diff --numstat -z HEAD`` for line counts.  Both run with ``-z`` so
    their NUL-delimited, raw (unquoted) paths key against each other exactly —
    including renames and non-ASCII filenames.

    Args:
        repo_path: Absolute path to the git repository or worktree.
        isolate_config: Passed through to ``run_git``.  Set ``False`` for
            host/UI paths (human merge gate, UI commit preview) so that the
            status reflects operator global config (autocrlf/eol) — matching
            what the actual commit or merge will produce.  Defaults to ``True``
            for agent worktree paths.
    """
    # Porcelain status with -z: NUL-delimited records, paths emitted RAW (no
    # double-quoting, no octal escaping).  This keys identically to numstat -z
    # below — even for renames and non-ASCII filenames, which the legacy
    # newline+quoted format mis-keyed (renames produced "old -> new"; non-ASCII
    # produced per-byte mojibake), so both reported +0/-0 churn.
    result = await run_git(
        "status", "--porcelain", "-z", cwd=repo_path, isolate_config=isolate_config
    )

    # Numstat for tracked changes; -z makes paths raw/NUL-delimited and renames
    # parse to the new path, matching the porcelain keys exactly.
    # --no-ext-diff/--no-textconv stop external exec.
    numstat = await run_git(
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--numstat",
        "-z",
        "HEAD",
        cwd=repo_path,
        check=False,
        isolate_config=isolate_config,
    )
    numstat_map: dict[str, tuple[int, int]] = {
        fp: (a, d) for a, d, fp in parse_numstat(numstat.stdout)
    }

    files: list[FileStat] = []
    for xy, filepath in _parse_porcelain_z(result.stdout):
        added, deleted = numstat_map.get(filepath, (0, 0))
        files.append(
            FileStat(
                path=filepath,
                added=added,
                deleted=deleted,
                status=xy.strip(),
            )
        )
    return files


def _parse_porcelain_z(output: str) -> list[tuple[str, str]]:
    """Parse ``git status --porcelain -z`` into ``(xy, path)`` records.

    NUL-delimited record grammar::

        XY <path>\\0                  # normal change
        XY <newpath>\\0<oldpath>\\0   # rename / copy (R/C status)

    Paths are raw (unquoted, unescaped) in ``-z`` mode.  For a rename/copy the
    **new** path comes first and the old path follows as a separate NUL field;
    we key on the new path so it matches the numstat-``-z`` target.

    Args:
        output: Raw stdout from ``git status --porcelain -z``.

    Returns:
        List of ``(xy, path)`` tuples where ``xy`` is the two-char status code.
    """
    fields = output.split("\x00")
    records: list[tuple[str, str]] = []
    i = 0
    n = len(fields)
    while i < n:
        field = fields[i]
        if not field or len(field) < 4:
            # Empty trailing element, or the old-path field already consumed by
            # a preceding rename (handled below via i += 2).
            i += 1
            continue
        xy = field[:2]
        # ``-z`` emits raw, unquoted paths (verified independent of
        # core.quotePath).  ``_unquote_git_path`` is a no-op on a raw path but
        # is applied defensively so a quoted path from any legacy/edge source
        # still decodes correctly (and never produces per-byte mojibake).
        path = _unquote_git_path(field[3:])
        records.append((xy, path))
        # A rename/copy carries the old path in the very next NUL field.
        if xy and xy[0] in ("R", "C"):
            i += 2
        else:
            i += 1
    return records
