"""File-system walker for the indexer.

Contains the skip-extension set, file-size limit, and ``_collect_files``
walker that are shared between full-rebuild and incremental-update code paths.

``_collect_files`` is re-exported from :mod:`~yukar.indexer.service` so that
existing test imports (``from yukar.indexer.service import _collect_files``)
continue to work unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path

from yukar.sandbox.ignore import IgnoreRules

logger = logging.getLogger(__name__)

# Files to skip even if not gitignored (binary / large / noise)
_SKIP_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".ico",
        ".webp",
        ".bmp",
        ".tiff",
        ".pdf",
        ".zip",
        ".gz",
        ".tar",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".a",
        ".o",
        ".wasm",
        ".pyc",
        ".pyo",
        ".class",
        ".jar",
        ".mp3",
        ".mp4",
        ".wav",
        ".avi",
        ".mov",
        ".ttf",
        ".otf",
        ".woff",
        ".woff2",
        ".lock",  # lock files tend to be huge and uninteresting
    }
)

_MAX_FILE_BYTES = 500 * 1024  # 500 KiB per file
_NUL_SCAN_BYTES = 8192  # NUL check reads only the first 8 KiB (matches git heuristic)

# ---------------------------------------------------------------------------
# Secret-file name blocklist (defense-in-depth — A4-01)
#
# Files whose content should never be embedded and sent to Bedrock Titan,
# even if they somehow pass the gitignore check.  Checked by exact name,
# prefix, or suffix (case-sensitive — all secret file conventions use
# lower-case on Unix).
# ---------------------------------------------------------------------------

# Exact filenames that are always secret-bearing.
_SECRET_EXACT_NAMES: frozenset[str] = frozenset(
    {".env", ".netrc", "credentials", ".htpasswd", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
)

# Name prefixes — a file starting with ".env." (e.g. ".env.local", ".env.prod")
# is treated as a secret variant of a dotenv file.
_SECRET_NAME_PREFIXES: tuple[str, ...] = (".env.",)

# Explicit exceptions — committed non-secret dotenv templates that should NOT
# be treated as secret-bearing, even though they start with ".env.".
_SECRET_NAME_PREFIX_EXCEPTIONS: frozenset[str] = frozenset({".env.example", ".env.sample"})

# Suffixes for private-key and certificate formats.
_SECRET_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".p12", ".pfx", ".pkcs12")


def _is_secret_file(fpath: Path) -> bool:
    """Return True if *fpath* is a known secret-bearing file by name.

    Checks exact name, then name-prefix (with explicit exceptions for committed
    non-secret templates such as ``.env.example`` / ``.env.sample``), then
    suffix.  Returns False for all other files so legitimate code files are
    never incorrectly blocked.
    """
    name = fpath.name
    if name in _SECRET_EXACT_NAMES:
        return True
    if name in _SECRET_NAME_PREFIX_EXCEPTIONS:
        return False
    if any(name.startswith(p) for p in _SECRET_NAME_PREFIXES):
        return True
    return any(name.endswith(s) for s in _SECRET_SUFFIXES)


def _collect_files(repo_path: Path, ignore_rules: IgnoreRules) -> list[Path]:
    """Collect indexable files under *repo_path*.

    Files are skipped if they are gitignored, have a binary extension, exceed
    the size limit, or contain NUL bytes (binary heuristic).

    The NUL check reads the file once in binary mode (bounded to
    ``_MAX_FILE_BYTES`` by the preceding ``stat()`` size guard) and discards
    the bytes; the splitter later re-reads accepted files as text.  Two opens
    per accepted file is acceptable for local repos (the second read is served
    from the OS page cache).
    """
    import os

    result: list[Path] = []
    for root, dirnames, filenames in os.walk(repo_path):
        root_path = Path(root)
        dirnames[:] = sorted(
            d for d in dirnames if not ignore_rules.should_prune_dir(root_path / d)
        )
        for fname in sorted(filenames):
            fpath = root_path / fname
            # Skip symlinks whose resolved path lies outside the repo tree
            # (prevents reading arbitrary files via symlink traversal).
            if fpath.is_symlink():
                try:
                    real = Path(os.path.realpath(fpath))
                    repo_real = Path(os.path.realpath(repo_path))
                    if not real.is_relative_to(repo_real):
                        continue
                except OSError:
                    continue
            # Secret-file name blocklist — defense-in-depth (A4-01).
            # Checked AFTER the symlink guard and BEFORE gitignore so that a
            # .gitignore negation rule (``!.env``) cannot force a secret file
            # into the index by overriding a global gitignore exclusion.
            if _is_secret_file(fpath):
                logger.debug("indexer: skipping secret-bearing file %s", fpath)
                continue
            # Skip gitignored files
            if ignore_rules.is_ignored(fpath):
                continue
            # Skip binary / oversized files by extension
            ext = fpath.suffix.lower()
            if ext in _SKIP_EXTENSIONS:
                continue
            try:
                size = fpath.stat().st_size
            except OSError:
                continue
            if size > _MAX_FILE_BYTES:
                continue
            # NUL-byte binary heuristic: read only the first 8 KiB (git equivalent).
            # We read in binary mode to avoid decode errors on non-UTF-8 files;
            # the size guard above ensures the file is within _MAX_FILE_BYTES.
            try:
                with fpath.open("rb") as fh:
                    content = fh.read(_NUL_SCAN_BYTES)
                if b"\x00" in content:
                    continue
            except OSError:
                continue
            result.append(fpath)
    return result
