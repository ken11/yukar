"""Tests for yukar/cli.py — standalone path discovery and static asset preparation."""

from __future__ import annotations

import unittest.mock as mock
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_monorepo(base: Path) -> Path:
    """Create a minimal monorepo structure and return the root."""
    root = base / "monorepo"
    root.mkdir()
    (root / "pnpm-workspace.yaml").write_text("packages:\n  - apps/*\n")
    return root


def _make_next_dirs(root: Path) -> Path:
    """Return apps/web/.next path, creating parent dirs."""
    next_dir = root / "apps" / "web" / ".next"
    next_dir.mkdir(parents=True)
    return next_dir


def _fake_cli_dir(root: Path) -> Path:
    """Create and return a fake cli.py location inside the monorepo."""
    d = root / "apps" / "api" / "src" / "yukar"
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# _find_standalone
# ---------------------------------------------------------------------------


class TestFindStandalone:
    def test_monorepo_nested_layout(self, tmp_path: Path) -> None:
        """Monorepo layout: standalone/apps/web/server.js → return that dir."""
        root = _make_monorepo(tmp_path)
        next_dir = _make_next_dirs(root)
        nested_server_dir = next_dir / "standalone" / "apps" / "web"
        nested_server_dir.mkdir(parents=True)
        (nested_server_dir / "server.js").write_text("// server")

        from yukar import cli

        with mock.patch.object(cli, "__file__", str(_fake_cli_dir(root) / "cli.py")):
            result = cli._find_standalone()

        assert result == nested_server_dir

    def test_flat_layout_fallback(self, tmp_path: Path) -> None:
        """Flat layout: standalone/server.js → return standalone dir."""
        root = _make_monorepo(tmp_path)
        next_dir = _make_next_dirs(root)
        flat_server_dir = next_dir / "standalone"
        flat_server_dir.mkdir(parents=True)
        (flat_server_dir / "server.js").write_text("// server")

        from yukar import cli

        with mock.patch.object(cli, "__file__", str(_fake_cli_dir(root) / "cli.py")):
            result = cli._find_standalone()

        assert result == flat_server_dir

    def test_not_built_yet_returns_nested_path(self, tmp_path: Path) -> None:
        """When build hasn't run, return the nested path (server.js absent)."""
        root = _make_monorepo(tmp_path)
        _make_next_dirs(root)

        from yukar import cli

        with mock.patch.object(cli, "__file__", str(_fake_cli_dir(root) / "cli.py")):
            result = cli._find_standalone()

        expected = root / "apps" / "web" / ".next" / "standalone" / "apps" / "web"
        assert result == expected

    def test_no_monorepo_root_returns_none(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When pnpm-workspace.yaml is not found, return None and warn."""
        # Deeply nested path with no marker anywhere
        fake_dir = tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "h" / "i"
        fake_dir.mkdir(parents=True)

        from yukar import cli

        with mock.patch.object(cli, "__file__", str(fake_dir / "cli.py")):
            result = cli._find_standalone()

        assert result is None
        captured = capsys.readouterr()
        assert "pnpm-workspace.yaml not found" in captured.err


# ---------------------------------------------------------------------------
# _prepare_static_assets
# ---------------------------------------------------------------------------


class TestPrepareStaticAssets:
    def _setup(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create a monorepo with .next/static and public, plus a server_dir."""
        root = _make_monorepo(tmp_path)
        web = root / "apps" / "web"
        web.mkdir(parents=True)

        # Simulate .next/static with a CSS file
        static_src = web / ".next" / "static"
        static_src.mkdir(parents=True)
        (static_src / "app.css").write_text("body{}")

        # Simulate public/ with an image
        public_src = web / "public"
        public_src.mkdir()
        (public_src / "favicon.ico").write_bytes(b"\x00")

        server_dir = tmp_path / "server"
        server_dir.mkdir()
        (server_dir / "server.js").write_text("// server")

        return root, server_dir

    def test_copies_static_and_public(self, tmp_path: Path) -> None:
        from yukar.cli import _prepare_static_assets

        root, server_dir = self._setup(tmp_path)
        _prepare_static_assets(root, server_dir)

        assert (server_dir / ".next" / "static" / "app.css").exists()
        assert (server_dir / "public" / "favicon.ico").exists()

    def test_overwrites_existing_static(self, tmp_path: Path) -> None:
        """Stale static files are removed before copying fresh ones."""
        from yukar.cli import _prepare_static_assets

        root, server_dir = self._setup(tmp_path)

        # Pre-populate with stale file
        stale_static = server_dir / ".next" / "static"
        stale_static.mkdir(parents=True)
        (stale_static / "stale.css").write_text("old{}")

        _prepare_static_assets(root, server_dir)

        assert not (server_dir / ".next" / "static" / "stale.css").exists()
        assert (server_dir / ".next" / "static" / "app.css").exists()

    def test_no_public_dir_is_fine(self, tmp_path: Path) -> None:
        """Missing apps/web/public does not raise."""
        from yukar.cli import _prepare_static_assets

        root = _make_monorepo(tmp_path)
        web = root / "apps" / "web"
        web.mkdir(parents=True)
        static_src = web / ".next" / "static"
        static_src.mkdir(parents=True)
        (static_src / "app.css").write_text("body{}")

        server_dir = tmp_path / "server"
        server_dir.mkdir()

        # Should not raise even when public/ is absent
        _prepare_static_assets(root, server_dir)

        assert (server_dir / ".next" / "static" / "app.css").exists()
        assert not (server_dir / "public").exists()

    def test_no_static_dir_is_fine(self, tmp_path: Path) -> None:
        """Missing .next/static does not raise."""
        from yukar.cli import _prepare_static_assets

        root = _make_monorepo(tmp_path)
        web = root / "apps" / "web"
        web.mkdir(parents=True)

        server_dir = tmp_path / "server"
        server_dir.mkdir()

        _prepare_static_assets(root, server_dir)

        assert not (server_dir / ".next" / "static").exists()
