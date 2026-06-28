"""XDG-compliant settings loader.

Reads ~/.config/yukar/settings.yaml (or $YUKAR_CONFIG_DIR/settings.yaml).
If the file does not exist, a default is written and returned.
workspace_root is ~ expanded.
"""

from __future__ import annotations

import os
from pathlib import Path

from yukar.config.settings import Settings
from yukar.storage.yaml_io import read_yaml, write_yaml


def config_dir() -> Path:
    """Return the config directory, respecting YUKAR_CONFIG_DIR env override."""
    env = os.environ.get("YUKAR_CONFIG_DIR")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "yukar"
    return Path.home() / ".config" / "yukar"


def settings_path() -> Path:
    return config_dir() / "settings.yaml"


_REMOVED_SETTINGS_KEYS: frozenset[str] = frozenset(
    {
        # Removed in the OSS-readiness refactor; Settings now has no 'ui' field.
        # Pre-existing settings.yaml files may still contain this key, so we
        # strip it before validation to avoid a ValidationError on startup.
        # extra="forbid" is still active — only these explicitly-removed keys are
        # silently dropped; genuine typos in current keys continue to be rejected.
        "ui",
    }
)


def load_settings() -> Settings:
    """Load settings from disk, creating defaults if absent."""
    path = settings_path()
    if path.exists():
        raw = read_yaml(path)
        # Drop any top-level keys that were removed from the schema.  This
        # allows existing installs to survive a settings.yaml written by an
        # older version without crashing on startup.
        for key in _REMOVED_SETTINGS_KEYS:
            raw.pop(key, None)
        settings = Settings.model_validate(raw)
    else:
        settings = Settings()
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_settings_sync(path, settings)

    # Expand ~ in workspace_root
    settings.workspace_root = str(Path(settings.workspace_root).expanduser())
    return settings


def _write_settings_sync(path: Path, settings: Settings) -> None:
    """Write settings synchronously via temp→os.replace (atomic, no open('w')).

    Called only at startup before the event loop is running.  Uses ruamel.yaml
    directly but routes through a temp file so the write is crash-safe.
    """
    import io
    import tempfile

    from ruamel.yaml import YAML

    data = settings.model_dump()
    yaml = YAML()
    yaml.default_flow_style = False

    buf = io.BytesIO()
    yaml.dump(data, buf)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(buf.getvalue())
        os.replace(tmp, path)
    except Exception:
        import contextlib

        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


async def save_settings(settings: Settings) -> None:
    """Persist settings back to disk."""
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    await write_yaml(path, settings.model_dump())
