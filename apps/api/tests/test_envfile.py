"""Env-source declarations (§11) — dotenv parser, path resolution, model rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from yukar.models.project import DevService
from yukar.preview.envfile import EnvFileError, parse_env_file, resolve_env_file_path


class TestParseEnvFile:
    def test_basic_and_dialect(self, tmp_path: Path) -> None:
        f = tmp_path / "dev.env"
        f.write_text(
            "# comment\n"
            "\n"
            "PLAIN=value\n"
            "export EXPORTED=yes\n"
            'DQ="quoted value"\n'
            "SQ='single'\n"
            "HASH=se#cret\n"
            "SPACED =  padded  \n"
            "EMPTY=\n"
        )
        values = parse_env_file(f)
        assert values == {
            "PLAIN": "value",
            "EXPORTED": "yes",
            "DQ": "quoted value",
            "SQ": "single",
            # Verbatim: trailing "#" is part of the value, never a comment.
            "HASH": "se#cret",
            "SPACED": "padded",
            "EMPTY": "",
        }

    def test_malformed_line_reports_lineno_but_never_content(self, tmp_path: Path) -> None:
        # The line itself may be a secret fragment (e.g. a multi-line PEM key)
        # and the error reaches the agent via tool results — content must stay out.
        f = tmp_path / "bad.env"
        f.write_text("OK=1\nSUPER-SECRET-FRAGMENT\n")
        with pytest.raises(EnvFileError, match="line 2") as excinfo:
            parse_env_file(f)
        assert "SUPER-SECRET-FRAGMENT" not in str(excinfo.value)

    def test_bad_key_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.env"
        f.write_text("1BAD=x\n")
        with pytest.raises(EnvFileError, match="malformed"):
            parse_env_file(f)

    def test_missing_file_errors(self, tmp_path: Path) -> None:
        with pytest.raises(EnvFileError, match="cannot be read"):
            parse_env_file(tmp_path / "nope.env")

    def test_utf8_bom_is_accepted(self, tmp_path: Path) -> None:
        # A BOM-prefixed dotenv is valid; str.strip() does NOT remove U+FEFF,
        # so the first key must not be mangled into "﻿KEY".
        f = tmp_path / "bom.env"
        f.write_bytes(b"\xef\xbb\xbfKEY=value\n")
        assert parse_env_file(f) == {"KEY": "value"}

    def test_non_utf8_wrapped_as_envfile_error(self, tmp_path: Path) -> None:
        # Latin-1 bytes must surface as EnvFileError (not a raw UnicodeDecodeError
        # that escapes the DevServerError contract into a 500 / tool exception).
        f = tmp_path / "latin1.env"
        f.write_bytes(b"KEY=caf\xe9\n")
        with pytest.raises(EnvFileError, match="not valid UTF-8"):
            parse_env_file(f)


class TestResolveEnvFilePath:
    def test_absolute_kept(self, tmp_path: Path) -> None:
        assert resolve_env_file_path(str(tmp_path / "a.env"), None) == tmp_path / "a.env"

    def test_tilde_expanded(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        assert resolve_env_file_path("~/s.env", None) == tmp_path / "s.env"

    def test_relative_anchored_to_repo_root(self, tmp_path: Path) -> None:
        assert resolve_env_file_path(".env.dev", tmp_path) == tmp_path / ".env.dev"

    def test_relative_without_repo_root_errors(self) -> None:
        with pytest.raises(EnvFileError, match="repo-relative"):
            resolve_env_file_path(".env.dev", None)


class TestDevServiceEnvDeclarations:
    def _svc(self, **overrides: object) -> DevService:
        base: dict[str, object] = {"name": "web", "command": ["true"], "base_port": 3000}
        base.update(overrides)
        return DevService.model_validate(base)

    def test_valid_declarations_accepted(self) -> None:
        svc = self._svc(env_file=["~/x.env", ".env"], env_passthrough=["DATABASE_URL", "_X1"])
        assert svc.env_file == ["~/x.env", ".env"]

    def test_blank_env_file_entry_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            self._svc(env_file=["  "])

    def test_invalid_passthrough_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="env_passthrough"):
            self._svc(env_passthrough=["1BAD"])
        with pytest.raises(ValueError, match="env_passthrough"):
            self._svc(env_passthrough=["A B"])
