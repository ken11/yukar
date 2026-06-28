"""stats.json and error.json read/write helpers for the indexer.

``stats.json`` lives in each repo's index directory and records
``last_indexed_at``, ``ts_files``, ``fallback_files``, ``files_indexed``,
``chunks_indexed``, and ``embedding_dim``.

``error.json`` lives alongside ``stats.json`` and records the most recent
indexing failure:  ``{ "message": str, "error_type": str, "failed_at": ISO8601 }``.
It is written on failure and deleted on success so that ``get_status()`` can
surface the reason a repo is stuck at "unindexed".

All helpers are synchronous (called from async code via ``asyncio.to_thread``
where required, or directly in sync context for reads).
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import tempfile
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Internal helpers — shared by stats.json and error.json
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any] | None:
    """Load a JSON file, returning ``None`` on missing/corrupt file."""
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json_atomic(index_dir: Path, filename: str, data: dict[str, Any]) -> None:
    """Atomically write *data* as JSON to ``index_dir/filename`` (temp + os.replace)."""
    target = index_dir / filename
    fd, tmp = tempfile.mkstemp(dir=index_dir, prefix=f".tmp_{filename.split('.')[0]}_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
        os.replace(tmp, target)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_stats(index_dir: Path) -> dict[str, Any]:
    """Load ``stats.json`` from *index_dir*, returning ``{}`` on missing/corrupt file."""
    return _read_json(index_dir / "stats.json") or {}


# ---------------------------------------------------------------------------
# error.json helpers
# ---------------------------------------------------------------------------


def read_error(index_dir: Path) -> dict[str, Any] | None:
    """Load ``error.json`` from *index_dir*.

    Returns the parsed dict on success, or ``None`` when the file is absent
    or corrupt.
    """
    return _read_json(index_dir / "error.json")


def write_error(index_dir: Path, exc: BaseException) -> None:
    """Atomically write failure metadata to ``error.json`` in *index_dir*.

    Creates *index_dir* if it does not yet exist (so callers do not need to
    pre-create it just to record the error).

    The file format is::

        {
          "message": "<str(exc)>",
          "error_type": "<type(exc).__name__>",
          "failed_at": "<ISO8601 UTC>"
        }
    """
    index_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "message": str(exc),
        "error_type": type(exc).__name__,
        "failed_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    _write_json_atomic(index_dir, "error.json", data)


def clear_error(index_dir: Path) -> None:
    """Remove ``error.json`` from *index_dir* if it exists (no-op otherwise)."""
    with contextlib.suppress(OSError):
        (index_dir / "error.json").unlink()
