"""Minimal dotenv-style parser for user-declared dev-server env files (§11).

Deliberately tiny and literal — the goal is "read the file the user already
maintains", not a full dotenv dialect:

- ``KEY=VALUE`` per line; blank lines and ``#``-comment lines are skipped.
- An optional ``export `` prefix is accepted (shell-sourceable files).
- A value wrapped in matching single or double quotes is unquoted; no escape
  expansion is performed inside.
- Everything else is VERBATIM: no interpolation, no trailing-comment
  stripping (a secret may legitimately contain ``#``), no multi-line values.

The parser never touches the process environment; the caller decides where
the values go (the dev-server child, and nowhere else).
"""

from __future__ import annotations

import re
from pathlib import Path

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvFileError(ValueError):
    """An env file was missing, unreadable, or malformed."""


def resolve_env_file_path(decl: str, repo_root: Path | None) -> Path:
    """Resolve a declared env-file path (absolute / ``~`` / repo-relative).

    Repo-relative declarations resolve against *repo_root* — the repo's BASE
    checkout, never the worktree (gitignored env files do not exist there).

    Raises:
        EnvFileError: relative declaration without a known repo root.
    """
    path = Path(decl.strip()).expanduser()
    if path.is_absolute():
        return path
    if repo_root is None:
        raise EnvFileError(
            f"env_file {decl!r} is repo-relative but no repo root is known in this context"
        )
    return repo_root / path


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse *path* and return its variables (see module docstring for dialect).

    Raises:
        EnvFileError: the file is missing, unreadable, or has a malformed line.
    """
    try:
        # utf-8-sig transparently strips a leading BOM (a BOM-prefixed file is
        # valid dotenv, and str.strip() does NOT remove U+FEFF).
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise EnvFileError(f"env_file {str(path)!r} cannot be read: {exc}") from exc
    except UnicodeDecodeError as exc:
        # Not an OSError — must be wrapped, or it escapes the EnvFileError
        # contract and surfaces downstream as a raw 500 / tool exception.
        raise EnvFileError(
            f"env_file {str(path)!r} is not valid UTF-8: {exc}"
        ) from exc

    values: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _ENV_NAME_RE.match(key):
            # Never echo the line itself: this error reaches the AGENT through
            # the browser_open tool result, and a malformed line in a secrets
            # file (e.g. the continuation of a multi-line PEM key) IS a secret.
            raise EnvFileError(
                f"env_file {str(path)!r}: malformed line {lineno} "
                "(content withheld — expected KEY=VALUE)"
            )
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values
