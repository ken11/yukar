"""Extension-to-language mapping for the indexer.

``language_for_path`` returns the tree-sitter-language-pack language name
for a given file path, or ``None`` if the file has no known language.

``LANG_MAP`` is the canonical mapping used throughout the indexer.  It covers
the most common languages in a typical polyglot codebase; unsupported
extensions fall back to ``None``, which triggers line-based splitting.

Grammar availability
--------------------
tree-sitter-language-pack (tslp) ships *no* grammar dylibs in its wheel.
On the first ``process()`` call for a language, tslp's ``DownloadManager``
fetches the "all" bundle (~21 MB tar.zst) into
``~/Library/Caches/tree-sitter-language-pack/<version>/`` (macOS) or the
platform equivalent.  ``app.lifespan`` pre-fetches this bundle at startup via
``asyncio.to_thread`` so that the download happens once and is thereafter a
no-op (idempotent cache check).  On offline or air-gapped machines the
download will fail, and the splitter degrades gracefully to line-based
splitting — see ``splitter._ts_split`` for details.

Language name registry
-----------------------
Values in ``LANG_MAP`` must match names in the tslp download manifest
(``tree_sitter_language_pack.manifest_languages()``).  In particular:

- C# is ``"csharp"`` (NOT ``"c_sharp"`` — that name is absent from the
  manifest and causes ``RuntimeError: Language 'c_sharp' not available for
  download`` on fresh machines).
"""

from __future__ import annotations

from pathlib import Path

# Canonical extension-to-language name mapping.
# Values must be names accepted by ``tree_sitter_language_pack.get_parser()``.
# Languages not listed here degrade to line-based splitting (no tree-sitter).
LANG_MAP: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".md": "markdown",
    ".markdown": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",  # "c_sharp" is NOT in the tslp manifest; use "csharp"
    ".rb": "ruby",
    ".php": "php",
    ".kt": "kotlin",
    ".swift": "swift",
    ".scala": "scala",
    ".toml": "toml",
    ".json": "json",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".lua": "lua",
    ".r": "r",
    ".hs": "haskell",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".ml": "ocaml",
    ".mli": "ocaml",
    ".clj": "clojure",
    ".sql": "sql",
    ".xml": "xml",
    ".dockerfile": "dockerfile",
}


def language_for_path(path: str | Path) -> str | None:
    """Return the tree-sitter language name for *path*, or ``None``.

    Args:
        path: The file path.  Only the extension is examined.

    Returns:
        A language name string (e.g. ``"python"``) or ``None`` if the
        extension is unknown or has no associated parser.
    """
    suffix = Path(path).suffix.lower()
    return LANG_MAP.get(suffix)
