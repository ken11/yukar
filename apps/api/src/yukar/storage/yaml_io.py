"""YAML round-trip read/write using ruamel.yaml.

All writes are routed through atomic.py.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, cast, overload

from pydantic import BaseModel
from ruamel.yaml import YAML

from yukar.storage.atomic import atomic_write_with

logger = logging.getLogger(__name__)


def _make_yaml() -> YAML:
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.preserve_quotes = True
    # Never wrap long lines.  ruamel's emitter folds long double-quoted
    # scalars at ~80 columns, and a fold landing right after an escaped
    # backslash (e.g. a contract containing `rg '\bfoo\b'`) is emitted
    # WITHOUT a continuation backslash — the physical line break then reads
    # back as a phantom space (`settings\.yaml` → `settings\ .yaml`), so the
    # stored string no longer equals the written one (plan-hash divergence,
    # corrupted shell commands).  Content fidelity beats pretty wrapping.
    yaml.width = 2**31
    return yaml


def read_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file and return a plain dict (synchronous).

    Returns an empty dict if the file does not exist.
    """
    if not path.exists():
        return {}
    yaml = _make_yaml()
    with path.open("r", encoding="utf-8") as f:
        result = yaml.load(f)
    if result is None:
        return {}
    if not isinstance(result, dict):
        return {}
    return dict(result)


async def write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write data as YAML atomically."""
    yaml = _make_yaml()

    def _write(buf: object) -> None:
        yaml.dump(data, buf)

    await atomic_write_with(path, _write)


@overload
def load_model[T: BaseModel](path: Path, model_cls: type[T], *, default: T) -> T: ...


@overload
def load_model[T: BaseModel](
    path: Path, model_cls: type[T], *, default: None = None
) -> T | None: ...


def load_model[T: BaseModel](
    path: Path,
    model_cls: type[T],
    *,
    default: T | None = None,
) -> T | None:
    """Load a single YAML file and validate it into *model_cls* (synchronous).

    Returns *default* if the file does not exist.
    """
    if not path.exists():
        return default
    return model_cls.model_validate(read_yaml(path))


async def save_model(path: Path, obj: BaseModel) -> None:
    """Persist *obj* to *path* as YAML atomically."""
    await write_yaml(path, obj.model_dump(mode="json"))


@overload
async def load_model_async[T: BaseModel](path: Path, model_cls: type[T], *, default: T) -> T: ...


@overload
async def load_model_async[T: BaseModel](
    path: Path, model_cls: type[T], *, default: None = None
) -> T | None: ...


async def load_model_async[T: BaseModel](
    path: Path,
    model_cls: type[T],
    *,
    default: T | None = None,
) -> T | None:
    """Load a single YAML file and validate it into *model_cls* (async).

    Delegates to ``load_model`` inside ``asyncio.to_thread`` so the event loop
    is not blocked by synchronous YAML parsing.

    Returns *default* if the file does not exist.
    """
    result = await asyncio.to_thread(load_model, path, model_cls, default=default)
    return cast("T | None", result)


async def load_validated_dir_async[T](
    entries: Iterable[Path],
    loader: Callable[[Path], T],
    label: str,
) -> list[T]:
    """Iterate *entries*, call *loader* on each, log-and-skip on failure (async).

    The entire directory parse loop is run inside a single ``asyncio.to_thread``
    call to avoid per-file thread-hop overhead while still releasing the event
    loop for the duration of the I/O.

    *loader* may itself call synchronous ``read_yaml`` — that is safe because
    it executes inside the worker thread, not on the event loop.
    """
    result = await asyncio.to_thread(load_validated_dir, list(entries), loader, label)
    return cast("list[T]", result)


def load_validated_dir[T](
    entries: Iterable[Path],
    loader: Callable[[Path], T],
    label: str,
) -> list[T]:
    """Iterate *entries*, call *loader* on each, log-and-skip on failure.

    A single corrupt entry must never abort the entire listing (the
    "EP-6 disappeared" bug class).  Failed paths are logged at WARNING
    level with the full traceback and silently omitted from the result.
    """
    result: list[T] = []
    for path in entries:
        try:
            result.append(loader(path))
        except Exception:
            logger.warning("Failed to read %s %s", label, path.name, exc_info=True)
    return result
