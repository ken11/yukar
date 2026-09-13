"""UI language of the user (the Web UI's locale toggle).

The frontend sends its current locale on every request as the
``X-Yukar-Locale`` header.  Run-starting endpoints forward it to the
orchestrator so the user-facing agents (Manager / Reviewer) are told to write
in that language.  Pure leaf module (imports only from ``typing``).
"""

from __future__ import annotations

from typing import Literal, get_args

UiLanguage = Literal["ja", "en"]

UI_LANGUAGE_HEADER = "X-Yukar-Locale"

_VALID: frozenset[str] = frozenset(get_args(UiLanguage))


def parse_ui_language(raw: str | None) -> UiLanguage | None:
    """Return the language when *raw* is a known locale, else ``None``.

    Accepts case/whitespace variations and a region suffix (``ja-JP``).
    Unknown values are ignored rather than rejected: the header is advisory.
    """
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    base = value.split("-", 1)[0].split("_", 1)[0]
    if base in _VALID:
        return base  # type: ignore[return-value]  # ty: ignore[invalid-return-type]
    return None
