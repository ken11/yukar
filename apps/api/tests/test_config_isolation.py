"""Characterization / bug-detection tests for isolate_config Tier C independence.

Finding: isolate-config-nesting (fixed in G1-G8 batch)
--------------------------------
In git/runner.py the ``isolate_config`` guard (Tier C) was nested *inside* the
``harden`` guard (Tier B), so ``harden=False, isolate_config=True`` dropped
``GIT_CONFIG_NOSYSTEM`` / ``GIT_CONFIG_GLOBAL``.

G2(a) review fix adds a separate concern: ``GIT_ATTR_NOSYSTEM`` was moved to
``isolate_config`` only, causing the host/UI path (``harden=True,
isolate_config=False``) to lose system-gitattributes isolation.  The fix sets
``GIT_ATTR_NOSYSTEM`` in BOTH the ``harden`` branch and the ``isolate_config``
branch so no code path loses coverage.

Tests
------
TC-1  (pass): harden=False, isolate_config=True  → GIT_CONFIG_NOSYSTEM set
TC-2  (pass): harden=True,  isolate_config=True  → GIT_CONFIG_NOSYSTEM set
TC-3  (pass): harden=False, isolate_config=True  → GIT_CONFIG_GLOBAL  set
TC-4  (pass): harden=False, isolate_config=True  → GIT_ATTR_NOSYSTEM  set
TC-5  (pass): harden=False, isolate_config=False → neither CONFIG var injected
TC-6  (pass): harden=True,  isolate_config=False → GIT_CONFIG_NOSYSTEM absent
TC-7  (pass): harden=True,  isolate_config=False → GIT_ATTR_NOSYSTEM set
              (host/UI path must still block system gitattributes — review fix #2)
TC-8  (pass): harden=True,  isolate_config=True  → GIT_ATTR_NOSYSTEM set (regression)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from yukar.git.runner import run_git

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_proc(returncode: int = 0) -> MagicMock:
    """Return a mock that quacks like asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", b""))
    return proc


async def _capture_env(
    *,
    harden: bool,
    isolate_config: bool,
    tmp_path: Path,
) -> dict[str, str]:
    """Call run_git with the given flags and return the env dict that would
    have been passed to asyncio.create_subprocess_exec.

    We monkeypatch both ``asyncio.create_subprocess_exec`` (called by
    run_git) and ``yukar.config.paths.empty_hooks_dir`` (called only when
    harden=True) so no real filesystem git operations are performed.
    """
    captured: dict[str, str] = {}

    async def fake_exec(
        *cmd: str,
        cwd: str,
        stdout: object,
        stderr: object,
        env: dict[str, str],
        **kwargs: object,
    ) -> MagicMock:
        captured.update(env)
        return _make_fake_proc()

    with (
        patch("yukar.git.runner.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("yukar.config.paths.empty_hooks_dir", return_value=Path("/fake/hooks")),
    ):
        await run_git(
            "status",
            cwd=tmp_path,
            check=False,
            harden=harden,
            isolate_config=isolate_config,
        )

    return captured


# ---------------------------------------------------------------------------
# TC-1: harden=False, isolate_config=True → GIT_CONFIG_NOSYSTEM should be set
# ---------------------------------------------------------------------------


async def test_tc1_isolate_config_independent_of_harden_nosystem(tmp_path: Path) -> None:
    """GIT_CONFIG_NOSYSTEM=1 must appear when isolate_config=True, harden=False."""
    env = await _capture_env(harden=False, isolate_config=True, tmp_path=tmp_path)
    assert env.get("GIT_CONFIG_NOSYSTEM") == "1", (
        f"Expected GIT_CONFIG_NOSYSTEM='1' but got: {env.get('GIT_CONFIG_NOSYSTEM')!r}. "
        "Full env keys: " + ", ".join(sorted(env))
    )


# ---------------------------------------------------------------------------
# TC-2 (characterization): harden=True, isolate_config=True → var IS present
# ---------------------------------------------------------------------------


async def test_tc2_isolate_config_present_when_harden_true(tmp_path: Path) -> None:
    """Baseline: GIT_CONFIG_NOSYSTEM=1 is injected under the normal (harden=True) path."""
    env = await _capture_env(harden=True, isolate_config=True, tmp_path=tmp_path)
    assert env.get("GIT_CONFIG_NOSYSTEM") == "1", (
        "Regression: GIT_CONFIG_NOSYSTEM was expected under harden=True,isolate_config=True"
    )


# ---------------------------------------------------------------------------
# TC-3: harden=False, isolate_config=True → GIT_CONFIG_GLOBAL should be set
# ---------------------------------------------------------------------------


async def test_tc3_isolate_config_global_independent_of_harden(tmp_path: Path) -> None:
    """GIT_CONFIG_GLOBAL=/dev/null must appear when isolate_config=True, harden=False."""
    env = await _capture_env(harden=False, isolate_config=True, tmp_path=tmp_path)
    assert env.get("GIT_CONFIG_GLOBAL") == "/dev/null", (
        f"Expected GIT_CONFIG_GLOBAL='/dev/null' but got: {env.get('GIT_CONFIG_GLOBAL')!r}"
    )


# ---------------------------------------------------------------------------
# TC-4: harden=False, isolate_config=True → GIT_ATTR_NOSYSTEM should be set
#
# The docstring (line 37) places GIT_ATTR_NOSYSTEM under Tier C / isolate_config,
# but the implementation puts it at the top of the `if harden:` block (line 272),
# making it a Tier B variable in practice.  This is a secondary nesting issue in
# the same finding family.
# ---------------------------------------------------------------------------


async def test_tc4_git_attr_nosystem_documented_as_tier_c(tmp_path: Path) -> None:
    """GIT_ATTR_NOSYSTEM=1 is documented under Tier C; verify it is set when
    isolate_config=True regardless of harden flag."""
    env = await _capture_env(harden=False, isolate_config=True, tmp_path=tmp_path)
    assert env.get("GIT_ATTR_NOSYSTEM") == "1", (
        f"Expected GIT_ATTR_NOSYSTEM='1' but got: {env.get('GIT_ATTR_NOSYSTEM')!r}"
    )


# ---------------------------------------------------------------------------
# TC-5 (characterization): harden=False, isolate_config=False → vars absent
# ---------------------------------------------------------------------------


async def test_tc5_no_isolation_vars_when_both_false(tmp_path: Path) -> None:
    """When both harden=False and isolate_config=False, no isolation vars injected."""
    env = await _capture_env(harden=False, isolate_config=False, tmp_path=tmp_path)
    assert "GIT_CONFIG_NOSYSTEM" not in env, "Unexpected GIT_CONFIG_NOSYSTEM"
    assert "GIT_CONFIG_GLOBAL" not in env, "Unexpected GIT_CONFIG_GLOBAL"


# ---------------------------------------------------------------------------
# TC-6 (characterization): harden=True, isolate_config=False → config vars absent
# ---------------------------------------------------------------------------


async def test_tc6_no_config_isolation_when_isolate_false_harden_true(tmp_path: Path) -> None:
    """isolate_config=False suppresses the config vars even when harden=True."""
    env = await _capture_env(harden=True, isolate_config=False, tmp_path=tmp_path)
    assert "GIT_CONFIG_NOSYSTEM" not in env, (
        "GIT_CONFIG_NOSYSTEM should be absent when isolate_config=False"
    )
    assert "GIT_CONFIG_GLOBAL" not in env, (
        "GIT_CONFIG_GLOBAL should be absent when isolate_config=False"
    )


# ---------------------------------------------------------------------------
# TC-7 (review fix #2): harden=True, isolate_config=False → GIT_ATTR_NOSYSTEM set
#
# Host/UI paths (diff/merge/commit/checkout/status) call run_git with
# harden=True, isolate_config=False.  Before the G2(a) review fix,
# GIT_ATTR_NOSYSTEM was placed only inside the isolate_config block, so this
# combination dropped the system-gitattributes guard and restored the external
# driver attack surface.  The fix moves GIT_ATTR_NOSYSTEM to also be set when
# harden=True.
# ---------------------------------------------------------------------------


async def test_tc7_git_attr_nosystem_set_on_harden_only_path(tmp_path: Path) -> None:
    """GIT_ATTR_NOSYSTEM=1 must appear when harden=True, isolate_config=False.

    This is the host/UI path (e.g. git diff, git commit via the merge UI).
    Losing GIT_ATTR_NOSYSTEM here restores external filter/diff/merge drivers
    at the system-gitattributes level — exactly the surface that Tier B hardens.
    """
    env = await _capture_env(harden=True, isolate_config=False, tmp_path=tmp_path)
    assert env.get("GIT_ATTR_NOSYSTEM") == "1", (
        f"Expected GIT_ATTR_NOSYSTEM='1' on harden=True,isolate_config=False path "
        f"but got: {env.get('GIT_ATTR_NOSYSTEM')!r}. "
        "System gitattributes is not blocked on the host/UI code path."
    )


# ---------------------------------------------------------------------------
# TC-8 (regression): harden=True, isolate_config=True → GIT_ATTR_NOSYSTEM set
# ---------------------------------------------------------------------------


async def test_tc8_git_attr_nosystem_still_set_when_both_true(tmp_path: Path) -> None:
    """GIT_ATTR_NOSYSTEM=1 must still appear with the normal (both True) flags."""
    env = await _capture_env(harden=True, isolate_config=True, tmp_path=tmp_path)
    assert env.get("GIT_ATTR_NOSYSTEM") == "1", (
        "Regression: GIT_ATTR_NOSYSTEM was expected under harden=True,isolate_config=True"
    )
