"""Atomic write helpers.

All YAML / text writes must go through here.
Strategy: write to a temp file in the same directory, then os.replace() (atomic
rename on POSIX). An asyncio.Lock per path prevents concurrent writes to the
same file within the single event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from asyncio import Lock
from collections.abc import Callable
from pathlib import Path

_locks: dict[str, Lock] = {}


def _lock_for(path: Path) -> Lock:
    key = str(path.resolve())
    if key not in _locks:
        _locks[key] = Lock()
    return _locks[key]


def _write_bytes_sync(path: Path, data: bytes) -> None:
    """Synchronous inner implementation of the atomic write.

    mkdir / mkstemp / write / fsync×2 / os.replace are all performed here
    so they can be offloaded to a thread via ``asyncio.to_thread``.
    Durability (fsync×2) is fully preserved.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            # Flush user-space buffers and fsync the temp file so its
            # contents hit the disk *before* the rename. Without this a
            # power loss between replace and writeback can leave a
            # zero-length or partial file under *path*.
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # fsync the parent directory so the rename itself is durable.
        # Best-effort: some platforms/filesystems reject opening a dir.
        with contextlib.suppress(OSError):
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


async def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes to path atomically.

    Acquires a per-path ``asyncio.Lock`` to serialise concurrent callers, then
    offloads the filesystem work (mkstemp / write / fsync×2 / os.replace) to a
    thread via ``asyncio.to_thread`` so that fsync does not block the event loop.

    Guarantees that *path*'s parent directory exists before writing, creating
    it (and any missing ancestors) with ``parents=True, exist_ok=True``.
    """
    lock = _lock_for(path)
    async with lock:
        await asyncio.to_thread(_write_bytes_sync, path, data)


async def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write text to path atomically.

    Guarantees that *path*'s parent directory exists (delegates to
    ``atomic_write_bytes``).
    """
    await atomic_write_bytes(path, text.encode(encoding))


async def atomic_write_with(path: Path, writer: Callable[[object], None]) -> None:
    """Write using a callable that accepts a file-like object.

    The callable receives a binary file handle. Useful for ruamel.yaml.dump().
    Guarantees that *path*'s parent directory exists (delegates to
    ``atomic_write_bytes``).
    """
    import io

    buf = io.BytesIO()
    writer(buf)
    await atomic_write_bytes(path, buf.getvalue())
