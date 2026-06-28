"""Code splitter — tree-sitter-based semantic chunker with line-based fallback.

``split_file`` is the primary entry point.  It splits a source file into a
list of ``Chunk`` dicts.  Each chunk represents a semantically coherent unit
(function, class, top-level block, or a line-range slice for unsupported
languages).

Splitting strategy
------------------
1. Detect language from file extension (``languages.language_for_path``).
2. If the language is supported by tree-sitter-language-pack, use
   ``tree_sitter_language_pack.process`` to obtain structure-aware chunks via
   ``ProcessConfig(chunk_max_size=MAX_CHUNK_CHARS)``.
3. If the language is unsupported, unavailable, or parsing fails, fall back
   to line-based splitting (``_line_split``).  A **warning** is emitted once
   per language when structure splitting is disabled so the operator can see
   that degradation occurred (e.g. because the grammar bundle has not been
   downloaded yet).
4. Any chunk that still exceeds ``MAX_CHUNK_CHARS`` is further sliced by
   ``_resplit_oversized``.

Concurrency
-----------
tree-sitter / tslp are synchronous C extensions.  Callers that live in the
asyncio event loop must wrap calls with a bounded ``asyncio.to_thread``:

    sem = asyncio.Semaphore(4)
    async def split_async(path, text, ...):
        async with sem:
            return await asyncio.to_thread(split_file, path, text, ...)

The module-level functions are intentionally synchronous so they can be tested
directly without an event loop.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TypedDict

from yukar.indexer.languages import language_for_path

logger = logging.getLogger(__name__)

# Maximum characters per chunk.  Larger chunks are re-split.
MAX_CHUNK_CHARS: int = 3000
# Fallback line window for line-based splitting.
LINE_SPLIT_LINES: int = 80
# Overlap characters for forced splits (approximated via line-boundary backtrack).
CHUNK_OVERLAP_CHARS: int = 200

# Track which languages have already had their "structure split degraded" warning
# emitted so that each language only produces a single log line per process.
_warned_fallback_languages: set[str] = set()


class Chunk(TypedDict):
    """A single code chunk produced by the splitter."""

    repo: str
    path: str  # repo-relative POSIX path
    start_line: int  # 0-indexed, inclusive
    end_line: int  # 0-indexed, inclusive
    text: str
    language: str | None  # None for line-based fallback
    mtime: float  # file mtime (seconds since epoch); 0.0 if unknown


def split_file(
    text: str,
    *,
    repo: str,
    path: str | Path,
    max_chars: int = MAX_CHUNK_CHARS,
    mtime: float = 0.0,
) -> list[Chunk]:
    """Split *text* into semantic chunks.

    Args:
        text: Full source text of the file.
        repo: Repository name (stored verbatim in each chunk).
        path: Repo-relative file path (used for extension-based language detection).
        max_chars: Maximum character count per chunk.  Oversized chunks are
            further split by line boundaries.
        mtime: File modification time (seconds since epoch).  Stored in each
            chunk for incremental reindex mtime comparison.  Defaults to 0.0
            when unknown.

    Returns:
        A list of ``Chunk`` dicts.  The list is never empty — even an empty
        file produces one chunk (with empty text).
    """
    str_path = Path(path).as_posix()
    language = language_for_path(path)

    if language is not None:
        chunks = _ts_split(
            text, repo=repo, path=str_path, language=language, max_chars=max_chars, mtime=mtime
        )
        if chunks is not None:
            return _enforce_max(chunks, max_chars, mtime=mtime)

    # Fallback: line-based splitting (language=None)
    return _line_split(text, repo=repo, path=str_path, max_chars=max_chars, mtime=mtime)


# ---------------------------------------------------------------------------
# tree-sitter-language-pack based splitting
# ---------------------------------------------------------------------------


def _ts_split(
    text: str,
    *,
    repo: str,
    path: str,
    language: str,
    max_chars: int,
    mtime: float = 0.0,
) -> list[Chunk] | None:
    """Attempt to split *text* using tree-sitter-language-pack.

    Returns a list of chunks on success, or ``None`` if parsing failed or the
    language is unavailable (caller should fall back to line splitting).

    When splitting fails, a **warning** is logged once per language (subsequent
    failures for the same language are silenced to avoid log spam).  This makes
    the degradation observable without drowning the log in repeated messages.
    The most common cause is that the grammar bundle has not been downloaded
    yet (e.g. first startup without network access).
    """
    try:
        import tree_sitter_language_pack as tslp  # type: ignore[import-untyped]

        config = tslp.ProcessConfig(
            language=language,
            structure=True,
            chunk_max_size=max_chars,
            imports=False,
            exports=False,
        )
        result = tslp.process(text, config)
    except Exception as exc:  # noqa: BLE001 — any error degrades gracefully
        _warn_fallback(language, path, exc)
        return None

    raw_chunks = list(result.chunks or [])
    if not raw_chunks:
        # tslp returned no chunks — fall back so the caller handles it
        return None

    out: list[Chunk] = []
    for c in raw_chunks:
        chunk_text = c.content
        if not chunk_text.strip():
            continue
        out.append(
            Chunk(
                repo=repo,
                path=path,
                start_line=c.start_line,
                end_line=c.end_line,
                text=chunk_text,
                language=language,
                mtime=mtime,
            )
        )

    return out if out else None


def _warn_fallback(language: str, path: str, exc: BaseException) -> None:
    """Emit a per-language-once warning that structure splitting has degraded.

    Uses the module-level ``_warned_fallback_languages`` set so that each
    language only produces a single WARNING entry, regardless of how many
    files trigger the fallback.
    """
    if language in _warned_fallback_languages:
        logger.debug("tree-sitter split failed for %s (%s): %s", path, language, exc)
        return
    _warned_fallback_languages.add(language)
    logger.warning(
        "tree-sitter structure splitting is unavailable for language '%s' "
        "(degraded to line-based splitting). "
        "This usually means the grammar bundle has not been downloaded yet — "
        "check startup logs for grammar pre-fetch results. "
        "First affected file: %s. Cause: %s",
        language,
        path,
        exc,
    )


# ---------------------------------------------------------------------------
# Line-based fallback
# ---------------------------------------------------------------------------


def _line_split(
    text: str,
    *,
    repo: str,
    path: str,
    max_chars: int,
    mtime: float = 0.0,
    overlap_chars: int = CHUNK_OVERLAP_CHARS,
) -> list[Chunk]:
    """Split *text* into fixed-line-window chunks with overlap.

    Each window is at most ``LINE_SPLIT_LINES`` lines *and* at most *max_chars*
    characters.  Consecutive chunks share approximately *overlap_chars* of
    text by backtracking line boundaries before advancing the start pointer.

    The overlap backtrack is capped at half the window size so that even for
    files whose lines are all shorter than *overlap_chars* (e.g. 2-char lines),
    the overlap never exceeds 50 % of the window — guaranteeing forward
    progress of at least half a window per step and bounding chunk count to at
    most 2× the no-overlap theoretical minimum.

    Args:
        text: Source text.
        repo: Repository name.
        path: Repo-relative file path.
        max_chars: Maximum characters per chunk.
        mtime: File modification time (seconds since epoch).
        overlap_chars: Approximate character count of overlap between
            consecutive chunks (line-boundary aligned).

    Returns:
        A non-empty list of chunks (language=``None``).
    """
    lines = text.splitlines(keepends=True)
    if not lines:
        return [
            Chunk(
                repo=repo, path=path, start_line=0, end_line=0, text="", language=None, mtime=mtime
            )
        ]

    chunks: list[Chunk] = []
    i = 0
    while i < len(lines):
        # Collect lines until window size or char limit is reached
        window: list[str] = []
        chars = 0
        while i < len(lines) and len(window) < LINE_SPLIT_LINES and chars < max_chars:
            window.append(lines[i])
            chars += len(lines[i])
            i += 1
        start = i - len(window)
        chunk_text = "".join(window)
        chunks.append(
            Chunk(
                repo=repo,
                path=path,
                start_line=start,
                end_line=i - 1,
                text=chunk_text,
                language=None,
                mtime=mtime,
            )
        )
        # Apply overlap: backtrack i so the next chunk re-covers ~overlap_chars
        # of the current chunk's tail (line-boundary aligned).
        #
        # The backtrack is capped at half the window length to guarantee that
        # overlap never exceeds 50 % of the window — even when all lines are
        # shorter than overlap_chars.  Without this cap, near-total overlap
        # causes O(N²) chunk explosion on files with many very short lines.
        if i < len(lines) and overlap_chars > 0:
            overlap_acc = 0
            back = 0
            max_back = max(1, len(window) // 2)
            while back < max_back and overlap_acc < overlap_chars:
                back += 1
                overlap_acc += len(window[-back])
            # Guarantee at least one line of progress to prevent infinite loops.
            i = max(i - back, start + 1)

    return chunks


# ---------------------------------------------------------------------------
# Post-process: enforce max_chars on any chunk
# ---------------------------------------------------------------------------


def _enforce_max(chunks: list[Chunk], max_chars: int, *, mtime: float = 0.0) -> list[Chunk]:
    """Re-split any chunk that exceeds *max_chars*.

    Args:
        chunks: Input chunk list (may be empty).
        max_chars: Hard character limit per chunk.
        mtime: File mtime to propagate into newly created sub-chunks.

    Returns:
        A new list where every chunk's ``text`` is at most *max_chars* chars.
    """
    out: list[Chunk] = []
    for chunk in chunks:
        if len(chunk["text"]) <= max_chars:
            out.append(chunk)
        else:
            # Re-split oversized chunk by line boundaries (with overlap).
            sub = _line_split(
                chunk["text"],
                repo=chunk["repo"],
                path=chunk["path"],
                max_chars=max_chars,
                mtime=chunk.get("mtime", mtime),
            )
            # Fix up start_line offset (chunk's start_line is absolute)
            offset = chunk["start_line"]
            for s in sub:
                out.append(
                    Chunk(
                        repo=s["repo"],
                        path=s["path"],
                        start_line=s["start_line"] + offset,
                        end_line=s["end_line"] + offset,
                        text=s["text"],
                        language=chunk["language"],
                        mtime=s.get("mtime", mtime),
                    )
                )
    return out
