"""Subprocess environment construction for sandboxed command execution.

run_command (and any tool that spawns project-controlled subprocesses) must
NOT inherit the host process environment wholesale.  The highest-risk failure
mode is not that the agent runs ``pnpm test`` -- it is that ``pnpm test``
runs while ANTHROPIC_API_KEY / AWS_* / GITHUB_TOKEN / SSH_AUTH_SOCK are present
in the environment and can be exfiltrated by arbitrary project code.

``build_subprocess_env`` constructs a controlled environment:

1. Start empty and copy only an explicit allowlist of non-sensitive variable
   names (and a few safe prefixes such as ``LC_`` / ``XDG_``) from the parent.
2. Inject safe defaults (``CI=1``, ``NO_COLOR=1``, ``GIT_TERMINAL_PROMPT=0``)
   and ``PWD``.
3. Scrub any variable whose name matches a known secret name or a sensitive
   substring — this is non-overridable for the allowlist/prefix passthrough
   sources and protects them from ever leaking a credential.
4. Merge caller-supplied ``extra`` *after* the scrub: these are explicit,
   trusted, code-level additions (e.g. GIT_AUTHOR_* for a commit).  ``extra``
   is the one sanctioned way to (re)introduce a named credential a specific
   tool needs — it bypasses the scrub intentionally.

HOME and PATH are preserved from the parent so language toolchains and package
managers (npm/pnpm/uv/cargo caches under ~) keep working -- containment here is
about secrets, not about breaking the build.  Because passthrough is an
allowlist, arbitrary project-specific env vars are dropped by default;
operators who need one must extend _SAFE_PASSTHROUGH_NAMES deliberately.
Stronger isolation (disposable worktree / container, empty HOME) is the
disposable-environment model and is out of scope for this in-process guard.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

# Variable names always passed through from the parent environment if present.
_SAFE_PASSTHROUGH_NAMES: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TERMINFO",
        "COLORTERM",
        "TZ",
        "TMPDIR",
        "LANG",
        "LANGUAGE",
        "PAGER",
        # TLS trust stores -- needed for HTTPS in toolchains, not secret.
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "CURL_CA_BUNDLE",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    }
)

# Variable name prefixes passed through (locale, XDG base dirs).
_SAFE_PASSTHROUGH_PREFIXES: tuple[str, ...] = ("LC_", "XDG_")

# Safe defaults injected into every sandboxed subprocess environment.
_INJECTED_DEFAULTS: dict[str, str] = {
    "CI": "1",
    "NO_COLOR": "1",
    # Never let git block on an interactive credential prompt.
    "GIT_TERMINAL_PROMPT": "0",
}

# Explicit secret variable names -- always removed from the passthrough set.
_SECRET_NAMES: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "SSH_AUTH_SOCK",
    }
)

# Sensitive substrings matched (case-insensitively) against the variable NAME.
# NOTE: bare "KEY" is intentionally absent — it would false-positive on
# MONKEY, KEYBOARD, KEYRING, etc.  Use specific compound forms instead.
_SECRET_SUBSTRINGS: tuple[str, ...] = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "PASSPHRASE",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "API_KEY",
    "APIKEY",
    "ACCESS_KEY",
    "SESSION_TOKEN",
    "SIGNING_KEY",
    "AUTH",
)


def _is_secret_name(name: str) -> bool:
    """Return True if *name* looks like a credential-bearing variable."""
    if name in _SECRET_NAMES:
        return True
    upper = name.upper()
    return any(sub in upper for sub in _SECRET_SUBSTRINGS)


def build_subprocess_env(
    *,
    cwd: Path,
    parent_env: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a sanitized environment for a sandboxed subprocess.

    Args:
        cwd: Resolved working directory; exported as ``PWD``.
        parent_env: Source environment to draw passthrough vars from.
            Defaults to ``os.environ``.
        extra: Explicit, trusted additions merged *after* the secret scrub
            (the only sanctioned way to pass a credential a tool needs).

    Returns:
        A new dict suitable for ``asyncio.create_subprocess_exec(env=...)``.
    """
    src: Mapping[str, str] = os.environ if parent_env is None else parent_env

    env: dict[str, str] = {}
    for name, value in src.items():
        if name in _SAFE_PASSTHROUGH_NAMES or name.startswith(_SAFE_PASSTHROUGH_PREFIXES):
            env[name] = value

    # Guarantee a usable PATH even if the parent lacked one.
    if not env.get("PATH"):
        env["PATH"] = os.defpath or "/usr/bin:/bin"

    env.update(_INJECTED_DEFAULTS)
    env["PWD"] = str(cwd)

    # Non-overridable secret scrub over the allowlist/prefix passthrough +
    # injected set.  caller-supplied ``extra`` is applied after this scrub
    # and is the one sanctioned way to (re)introduce a named credential.
    env = {key: val for key, val in env.items() if not _is_secret_name(key)}

    # Trusted, explicit additions bypass the scrub.
    if extra:
        env.update(extra)

    return env
