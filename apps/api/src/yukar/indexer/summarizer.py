"""Repo structure summarizer.

``summarize_repo`` generates a Markdown summary of a repository's structure
(ignoring gitignored files) and writes it to the index cache directory along
with a ``stats.json`` file that records indexing statistics.

The summary includes:
- File tree (ignore-filtered)
- Language breakdown (file counts)
- Top-level symbols per file (extracted by tree-sitter when available)

Cache layout (spec §4.1):
    ``{project}/.yukar/cache/index/{repo}/summary.md``
    ``{project}/.yukar/cache/index/{repo}/stats.json``

Concurrency
-----------
All file I/O is synchronous.  Callers in the asyncio event loop should wrap
``summarize_repo`` in ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import defaultdict
from pathlib import Path

from yukar.indexer.languages import language_for_path
from yukar.indexer.walker import _is_secret_file
from yukar.sandbox.ignore import IgnoreRules

logger = logging.getLogger(__name__)


def summarize_repo(
    repo_path: Path,
    index_dir: Path,
    *,
    ignore_rules: IgnoreRules | None = None,
    files_indexed: int = 0,
    chunks_indexed: int = 0,
    embedding_dim: int = 0,
    ts_files: int = 0,
    fallback_files: int = 0,
    last_indexed_at: str | None = None,
) -> None:
    """Generate a Markdown summary and stats for *repo_path*.

    Args:
        repo_path: Absolute path to the repository root.
        index_dir: Directory to write ``summary.md`` and ``stats.json``.
        ignore_rules: Active ``IgnoreRules`` (if ``None``, a fresh set is built).
        files_indexed: Number of files included in the FAISS index.
        chunks_indexed: Number of chunks included in the FAISS index.
        embedding_dim: Dimensionality of the embedding vectors.  Stored in
            ``stats.json`` and used at search time to detect model mismatches.
            0 means unknown / not stored.
        ts_files: Number of files split with tree-sitter structure splitting.
        fallback_files: Number of files split with line-based fallback splitting.
        last_indexed_at: ISO-format timestamp of the index completion.  When
            not ``None``, written to ``stats.json`` so callers can see when the
            index was last updated without a separate read-modify-write pass.
    """
    repo_path = repo_path.resolve()
    index_dir.mkdir(parents=True, exist_ok=True)

    if ignore_rules is None:
        ignore_rules = IgnoreRules.from_repo(repo_path)

    # Collect all non-ignored files
    all_files: list[Path] = _walk_files(repo_path, ignore_rules)

    # Language counts
    lang_counts: dict[str, int] = defaultdict(int)
    for f in all_files:
        lang = language_for_path(f) or "other"
        lang_counts[lang] += 1

    # Build file tree string
    tree_lines = _build_tree(repo_path, all_files)

    # Extract top-level symbols per file (best-effort)
    symbols: dict[str, list[str]] = {}
    for f in all_files[:200]:  # cap to avoid slow startup on huge repos
        syms = _extract_symbols(f)
        if syms:
            symbols[f.relative_to(repo_path).as_posix()] = syms

    # Render Markdown
    md = _render_summary(
        repo_name=repo_path.name,
        total_files=len(all_files),
        lang_counts=dict(lang_counts),
        tree_lines=tree_lines,
        symbols=symbols,
        files_indexed=files_indexed,
        chunks_indexed=chunks_indexed,
    )

    # Write summary.md atomically
    _atomic_write_text(index_dir / "summary.md", md)

    # Write stats.json atomically.
    # ``embedding_dim`` is stored so that search can detect dimension mismatches
    # when the embedding model is changed after indexing (Minor review fix #5).
    # ``ts_files``/``fallback_files`` and ``last_indexed_at`` are written in the
    # same pass to avoid a second read-modify-write by the caller.
    stats: dict[str, object] = {
        "repo": repo_path.name,
        "repo_path": str(repo_path),
        "total_files": len(all_files),
        "files_indexed": files_indexed,
        "chunks_indexed": chunks_indexed,
        "lang_counts": dict(lang_counts),
        "ts_files": ts_files,
        "fallback_files": fallback_files,
    }
    if embedding_dim > 0:
        stats["embedding_dim"] = embedding_dim
    if last_indexed_at is not None:
        stats["last_indexed_at"] = last_indexed_at
    _atomic_write_text(index_dir / "stats.json", json.dumps(stats, indent=2))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _walk_files(repo_path: Path, ignore_rules: IgnoreRules) -> list[Path]:
    """Return all non-ignored files under *repo_path*, sorted.

    Symlinks whose resolved path lies outside *repo_path* are excluded
    (A4-02 — mirrors the guard in indexer/walker._collect_files).
    """
    import os as _os

    repo_real = Path(_os.path.realpath(repo_path))
    result: list[Path] = []
    for root, dirnames, filenames in _os.walk(repo_path):
        root_path = Path(root)
        # Prune ignored directories in-place
        dirnames[:] = sorted(
            d for d in dirnames if not ignore_rules.should_prune_dir(root_path / d)
        )
        for fname in sorted(filenames):
            fpath = root_path / fname
            # Skip symlinks that escape the repo tree (A4-02).
            if fpath.is_symlink():
                try:
                    real = Path(_os.path.realpath(fpath))
                    if not real.is_relative_to(repo_real):
                        continue
                except OSError:
                    continue
            # Secret-file name blocklist — keep file tree consistent with what
            # _collect_files indexes (A4-01).  Without this guard, secret file
            # *names* (but not contents) would appear in summary.md's file tree.
            if _is_secret_file(fpath):
                continue
            if not ignore_rules.is_ignored(fpath):
                result.append(fpath)
    return result


def _build_tree(repo_path: Path, files: list[Path], max_lines: int = 300) -> list[str]:
    """Return a hierarchical directory+file tree representation.

    Directories are listed as ``dir/`` entries with indentation proportional
    to their depth, followed by their contents.  A trailing summary line is
    appended when the output is truncated.

    Example output::

        src/
          api/
            main.py
          utils.py
        README.md

    Args:
        repo_path: Repository root.
        files: All included files (absolute paths).
        max_lines: Truncate after this many lines.

    Returns:
        List of formatted lines.
    """
    from pathlib import PurePosixPath

    # Sort by relative POSIX path for deterministic output.
    sorted_files = sorted(files, key=lambda f: f.relative_to(repo_path).as_posix())

    lines: list[str] = []
    seen_dirs: set[str] = set()
    total_entries = 0  # tracks how many logical entries we have for the truncation msg

    def _emit(text: str) -> bool:
        """Append *text* to *lines*. Returns False if the limit was just reached."""
        nonlocal total_entries
        lines.append(text)
        total_entries += 1
        return len(lines) < max_lines

    remaining = len(sorted_files)
    for fpath in sorted_files:
        remaining -= 1
        rel = fpath.relative_to(repo_path)
        parts = PurePosixPath(rel).parts  # e.g. ('src', 'api', 'main.py')

        # Emit any ancestor directory lines not yet emitted.
        for depth in range(len(parts) - 1):
            dir_key = "/".join(parts[: depth + 1])
            if dir_key not in seen_dirs:
                seen_dirs.add(dir_key)
                indent = "  " * depth
                if not _emit(f"{indent}{parts[depth]}/"):
                    # Hit the limit while emitting a dir line.
                    lines.append(f"  ... ({remaining + 1} more entries)")
                    return lines

        # Emit the file line.
        file_indent = "  " * (len(parts) - 1)
        if not _emit(f"{file_indent}{parts[-1]}"):
            lines.append(f"  ... ({remaining} more entries)")
            return lines

    return lines


def _extract_symbols(file_path: Path) -> list[str]:
    """Extract top-level symbol names from *file_path* using tree-sitter.

    Returns an empty list on any failure (graceful degrade).
    """
    lang = language_for_path(file_path)
    if lang is None:
        return []
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if not text.strip():
        return []

    try:
        import tree_sitter_language_pack as tslp  # type: ignore[import-untyped]

        config = tslp.ProcessConfig(language=lang, structure=True, imports=False, exports=False)
        result = tslp.process(text, config)
        return [item.name for item in (result.structure or []) if item.name]
    except Exception as exc:  # noqa: BLE001
        logger.debug("symbol extraction failed for %s: %s", file_path, exc)
        return []


def _render_summary(
    *,
    repo_name: str,
    total_files: int,
    lang_counts: dict[str, int],
    tree_lines: list[str],
    symbols: dict[str, list[str]],
    files_indexed: int,
    chunks_indexed: int,
) -> str:
    """Render the Markdown summary."""
    lines: list[str] = []
    lines.append(f"# Repository: {repo_name}")
    lines.append("")
    lines.append(
        f"**{total_files} files** · **{files_indexed} indexed** · **{chunks_indexed} chunks**"
    )
    lines.append("")

    # Language breakdown
    if lang_counts:
        lines.append("## Language breakdown")
        lines.append("")
        for lang, count in sorted(lang_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {lang}: {count}")
        lines.append("")

    # File tree
    lines.append("## File tree")
    lines.append("")
    lines.append("```")
    lines.extend(tree_lines)
    lines.append("```")
    lines.append("")

    # Symbols
    if symbols:
        lines.append("## Top-level symbols")
        lines.append("")
        for path_str, syms in sorted(symbols.items()):
            lines.append(f"**{path_str}**: {', '.join(syms)}")
        lines.append("")

    return "\n".join(lines)


def _atomic_write_text(dest: Path, text: str) -> None:
    """Write *text* to *dest* atomically (temp + os.replace)."""
    import contextlib

    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, dest)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
