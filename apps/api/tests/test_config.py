"""Tests for config/paths.py layout and loader.py YUKAR_CONFIG_DIR override."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


class TestPaths:
    def test_project_yaml_location(self, tmp_workspace: Path) -> None:
        from yukar.config import paths

        root = str(tmp_workspace)
        p = paths.project_yaml(root, "my-project")
        assert p == tmp_workspace / "my-project" / ".yukar" / "project.yaml"

    def test_epic_yaml_location(self, tmp_workspace: Path) -> None:
        from yukar.config import paths

        root = str(tmp_workspace)
        p = paths.epic_yaml(root, "proj", "EP-5")
        assert p == tmp_workspace / "proj" / "epics" / "EP-5" / ".yukar" / "epic.yaml"

    def test_tasks_yaml_location(self, tmp_workspace: Path) -> None:
        from yukar.config import paths

        root = str(tmp_workspace)
        p = paths.tasks_yaml(root, "proj", "EP-5")
        assert p == tmp_workspace / "proj" / "epics" / "EP-5" / ".yukar" / "tasks.yaml"

    def test_threads_yaml_location(self, tmp_workspace: Path) -> None:
        from yukar.config import paths

        root = str(tmp_workspace)
        p = paths.threads_yaml(root, "proj", "EP-5")
        assert p == tmp_workspace / "proj" / "epics" / "EP-5" / "threads.yaml"

    def test_session_dir_location(self, tmp_workspace: Path) -> None:
        from yukar.config import paths

        root = str(tmp_workspace)
        p = paths.session_dir(root, "proj", "EP-5")
        assert p == tmp_workspace / "proj" / "epics" / "EP-5" / "sessions" / "session_EP-5"

    def test_message_json_location(self, tmp_workspace: Path) -> None:
        from yukar.config import paths

        root = str(tmp_workspace)
        p = paths.message_json(root, "proj", "EP-5", "th-abc", 3)
        expected = (
            tmp_workspace
            / "proj"
            / "epics"
            / "EP-5"
            / "sessions"
            / "session_EP-5"
            / "agents"
            / "agent_th-abc"
            / "messages"
            / "message_3.json"
        )
        assert p == expected

    def test_worktree_dir_location(self, tmp_workspace: Path) -> None:
        from yukar.config import paths

        root = str(tmp_workspace)
        p = paths.worktree_dir(root, "proj", "EP-5", "manager", "my-repo")
        assert (
            p == tmp_workspace / "proj" / "epics" / "EP-5" / "worktrees" / "manager" / "my-repo"
        )


class TestLoader:
    def test_load_creates_defaults_in_custom_dir(
        self, yukar_config_dir: Path
    ) -> None:
        from yukar.config.loader import load_settings, settings_path

        settings = load_settings()
        assert settings.git.author_name == "yukar"
        # Config file should have been created
        assert settings_path().exists()

    def test_yukar_config_dir_override(self, tmp_path: Path) -> None:
        custom = tmp_path / "custom_config"
        custom.mkdir()
        os.environ["YUKAR_CONFIG_DIR"] = str(custom)
        try:
            from yukar.config.loader import config_dir

            assert config_dir() == custom
        finally:
            del os.environ["YUKAR_CONFIG_DIR"]

    def test_workspace_root_tilde_expanded(self, yukar_config_dir: Path) -> None:
        from yukar.config.loader import load_settings

        settings = load_settings()
        assert "~" not in settings.workspace_root

    def test_legacy_ui_key_is_silently_dropped(self, tmp_path: Path) -> None:
        """A settings.yaml written by an older yukar version (which had a 'ui'
        section) must load without a ValidationError after the 'ui' field was
        removed.  The resulting Settings must have no 'ui' attribute."""
        from ruamel.yaml import YAML

        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        settings_file = cfg_dir / "settings.yaml"

        yaml = YAML()
        yaml.dump({"workspace_root": "~/yukar-projects", "ui": {"theme": "yukar"}}, settings_file)

        os.environ["YUKAR_CONFIG_DIR"] = str(cfg_dir)
        try:
            from yukar.config.loader import load_settings

            settings = load_settings()
            assert not hasattr(settings, "ui"), "'ui' attribute must not exist on Settings"
            assert "yukar-projects" in settings.workspace_root
        finally:
            del os.environ["YUKAR_CONFIG_DIR"]

    def test_unknown_current_key_still_raises(self, tmp_path: Path) -> None:
        """extra='forbid' must still reject genuine typos in current keys;
        only the explicitly-removed legacy keys are silently dropped."""
        from pydantic import ValidationError
        from ruamel.yaml import YAML

        cfg_dir = tmp_path / "cfg2"
        cfg_dir.mkdir()
        settings_file = cfg_dir / "settings.yaml"

        yaml = YAML()
        data = {"workspace_root": "~/yukar-projects", "bogus_top_level_key": True}
        yaml.dump(data, settings_file)

        os.environ["YUKAR_CONFIG_DIR"] = str(cfg_dir)
        try:
            from yukar.config.loader import load_settings

            with pytest.raises(ValidationError):
                load_settings()
        finally:
            del os.environ["YUKAR_CONFIG_DIR"]
