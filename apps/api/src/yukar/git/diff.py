"""Git diff operations.

Modes:
  working — uncommitted changes in the worktree (`git diff HEAD`)
  epic    — epic branch vs default branch three-dot diff

Also provides commit, merge, and multi-repo diff summary operations.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Literal

from yukar.git.runner import (
    GitError,
    git_author_env,
    parse_numstat,
    run_git,
    validate_git_ref,
)
from yukar.git.status import get_status
from yukar.models.diff import DiffResult, DiffSummary, FileStat, RepoDiffSummary

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dangerous driver configuration keys detected during host-context vetting.
# These indicate that git might exec an external program during the operation.
#
# The check covers LOCAL *and* WORKTREE scoped config (both are agent-writable).
# Operator global/system config is intentionally excluded — it is not
# agent-controlled and legitimate tools like git-lfs live there.
#
# Enforced precondition (layered defence): run_command's git guard in
# agents/tools/command.py blocks ``git config --worktree`` and
# ``extensions.worktreeConfig`` at the worker input level.  That guard makes
# it structurally hard for a worker to create worktree-scoped entries, but
# vetting here treats that guard as advisory, not sufficient: we check *both*
# local and worktree scopes in every _vet_host_git_context call regardless.
# ---------------------------------------------------------------------------

# .gitattributes driver assignment keywords.
_DANGEROUS_ATTR_KEYWORDS: tuple[str, ...] = ("filter=", "merge=", "diff=")


class GitVettingError(Exception):
    """Raised when host-context git operation is refused due to dangerous driver config.

    The message explains what was found and how to proceed manually.
    """


# Sentinel string returned by git show when a path does not exist in a tree.
# git 2.x uses "does not exist in" or "exists on disk, but not in" in the
# error message; we match on these PRECISE substrings to distinguish true
# absence from genuine git failures (broken object store, etc.).  We
# deliberately do NOT match a bare "path '" — that loose form risks
# reclassifying a future corruption message as benign absence (re-opening the
# fail-open hole).  "<path> does not exist in <tree>" already contains
# "does not exist in", so coverage is unchanged.
_GIT_SHOW_ABSENT_MARKERS: tuple[str, ...] = (
    "does not exist in",
    "exists on disk, but not in",
)

# Sentinel strings indicating a tree-ish itself is absent (empty repo, missing
# branch) — as opposed to a corrupt object store.  Used by the ls-tree scan to
# distinguish benign absence (skip) from genuine failure (fail-closed).
_GIT_TREE_ABSENT_MARKERS: tuple[str, ...] = (
    "unknown revision",
    "not a valid object name",
    "not a tree object",
    "ambiguous argument",
)


def _is_absent_from_tree(stderr: str) -> bool:
    """Return True if git show stderr indicates the path is simply not in the tree."""
    lower = stderr.lower()
    return any(marker.lower() in lower for marker in _GIT_SHOW_ABSENT_MARKERS)


def _is_tree_absent(stderr: str) -> bool:
    """Return True if git ls-tree stderr indicates the tree-ish is simply absent.

    Distinguishes a benign missing tree (empty repo / non-existent branch) from
    a genuine failure such as a corrupt object store, so the caller can skip the
    former but fail closed on the latter.
    """
    lower = stderr.lower()
    return any(marker in lower for marker in _GIT_TREE_ABSENT_MARKERS)


def _is_dangerous_config_entry(key: str, value: str) -> str | None:
    """Return a description if (key, value) represents a dangerous git config entry.

    Checks for external program execution vectors:
    - core.fsmonitor: dangerous unless set to boolean false
    - core.hookspath: dangerous if non-empty (hooks dir override)
    - diff.external: dangerous if non-empty
    - filter.<drv>.clean / .smudge / .process: dangerous if non-empty
    - merge.<drv>.driver: dangerous if non-empty
    - diff.<drv>.textconv: dangerous if non-empty
    - core.attributesFile: dangerous if non-empty (redirects attribute lookup)

    Returns None if the entry is safe.
    """
    k = key.strip().lower()
    v = value.strip()

    if k == "core.fsmonitor" and v.lower() not in ("false", "0", ""):
        return f"core.fsmonitor={v!r}"

    if k == "core.hookspath" and v:
        return f"core.hooksPath={v!r}"

    if k == "diff.external" and v:
        return f"diff.external={v!r}"

    # core.attributesFile redirects .gitattributes lookup to an agent-controlled path
    if k == "core.attributesfile" and v:
        return f"core.attributesFile={v!r}"

    if k.startswith("filter.") and k.endswith((".clean", ".smudge", ".process")) and v:
        return f"{k}={v!r}"

    if k.startswith("merge.") and k.endswith(".driver") and v:
        return f"{k}={v!r}"

    if k.startswith("diff.") and k.endswith(".textconv") and v:
        return f"{k}={v!r}"

    return None


async def _vet_host_git_context(repo_path: Path, merge_branch: str | None = None) -> None:
    """Vet a repo for dangerous driver configuration before a host-context operation.

    Checks LOCAL **and WORKTREE** scoped git config (both are agent-writable) and
    all ``.gitattributes`` files in the working tree, git object store, and
    ``.git/info/attributes``.

    This vetting applies only to agent-reachable configuration sources.  Operator
    global / system config is NOT checked — legitimate tools like git-lfs configured
    globally will not trigger false positives here.

    **Enforced precondition**: ``run_command``'s git guard blocks
    ``git config --worktree`` and ``extensions.worktreeConfig`` at the worker
    input level.  Even so, vetting here checks both local and worktree scopes
    independently and treats that guard as advisory — fail-closed.

    **Note on git-lfs in tracked .gitattributes**: repos that track ``.gitattributes``
    containing ``filter=lfs`` will be refused by this vetter.  This is an accepted
    trade-off — operators with such repos must perform the merge manually via their
    git client.  The error message below provides the manual-merge guidance.

    Args:
        repo_path: Absolute path to the git repository or worktree.
        merge_branch: Optional branch name to also check for ``.gitattributes``.

    Raises:
        GitVettingError: If dangerous driver config is found, or if vetting
            itself cannot complete reliably (fail-closed).
    """
    issues: list[str] = []

    # ------------------------------------------------------------------
    # 1. LOCAL + WORKTREE config via ``git config --list --show-scope -z``
    #
    #    --show-scope reveals both ``local`` (shared .git/config) and
    #    ``worktree`` (per-worktree config.worktree) entries.  We reject any
    #    dangerous entry in either scope.  global/system/command scopes are
    #    deliberately ignored — they are operator-controlled and not
    #    agent-writable.
    #
    #    Output format (NUL-delimited pairs):
    #      scope\0key\nvalue\0scope\0key\nvalue\0…
    #    Each entry consists of two consecutive NUL-delimited tokens:
    #      token[i]   = scope string  (e.g. "local", "worktree", "global")
    #      token[i+1] = "key\nvalue"  (the newline separates key from value)
    # ------------------------------------------------------------------
    try:
        config_result = await run_git(
            "config",
            "--list",
            "--show-scope",
            "-z",
            cwd=repo_path,
            check=False,
            harden=True,
            isolate_config=False,
        )
        if not config_result.ok:
            # git config --list can fail with rc=128 for non-git dirs, but a
            # valid repo under the vetter should never return non-zero here.
            raise GitVettingError(
                f"Cannot vet repository config (git config --list failed, "
                f"rc={config_result.returncode}): {config_result.stderr.strip()}"
            )
        # Parse NUL-delimited scope/kv pairs.
        # GitResult.stdout is decoded UTF-8 (with NUL chars preserved as \x00).
        # Format: scope\x00key\nvalue\x00scope\x00key\nvalue\x00…
        tokens = [t for t in config_result.stdout.split("\x00") if t]
        i = 0
        while i + 1 < len(tokens):
            scope = tokens[i].strip()
            kv = tokens[i + 1]
            i += 2
            # Only inspect agent-writable scopes.
            if scope not in ("local", "worktree"):
                continue
            # kv is "key\nvalue" (git separates with newline in -z mode).
            if "\n" in kv:
                key, _, value = kv.partition("\n")
            else:
                key, value = kv, ""
            desc = _is_dangerous_config_entry(key, value)
            if desc:
                issues.append(f"{scope} config {desc}")
    except GitVettingError:
        raise
    except Exception as exc:
        # Any unexpected failure here means we cannot guarantee safety.
        raise GitVettingError(
            f"Vetting aborted: unexpected error reading git config ({exc!r}). "
            "Refusing operation to remain fail-closed."
        ) from exc

    # ------------------------------------------------------------------
    # 2. Check for executable hooks in .git/hooks or core.hooksPath dir.
    #    We already disable hooks via -c core.hooksPath=<empty> in run_git,
    #    but we log suspicious hooks for transparency.
    # ------------------------------------------------------------------
    # _scan_executable_hooks resolves the real git dir (handles linked
    # worktrees where .git is a file) and logs any findings itself.
    await asyncio.to_thread(_scan_executable_hooks, repo_path)

    # ------------------------------------------------------------------
    # 3. All .gitattributes in tracked trees (HEAD + merge branch)
    #    Uses ls-tree to enumerate ALL gitattributes paths, not just root.
    # ------------------------------------------------------------------
    for tree_ish in ["HEAD"] + ([merge_branch] if merge_branch else []):
        await _vet_all_gitattributes_in_tree(repo_path, tree_ish, issues)

    # ------------------------------------------------------------------
    # 4. Working-tree .gitattributes (covers uncommitted add -A path in commit())
    #    Enumerate all .gitattributes on disk, excluding .git/ itself.
    # ------------------------------------------------------------------
    await asyncio.to_thread(_vet_working_tree_gitattributes, repo_path, issues)

    # ------------------------------------------------------------------
    # 5. .git/info/attributes (per-repo, non-tracked, agent may not write
    #    it via run_command but a poisoned repo could contain it)
    # ------------------------------------------------------------------
    try:
        from yukar.git.resolve import _resolve_git_dir as _rgd

        git_dir_for_attrs = _rgd(repo_path)
        info_attrs = git_dir_for_attrs / "info" / "attributes"
        if info_attrs.exists():
            _vet_gitattributes_content(
                info_attrs.read_text(encoding="utf-8", errors="replace"),
                ".git/info/attributes",
                issues,
            )
    except Exception:
        logger.debug("Could not inspect .git/info/attributes for vetting", exc_info=True)

    if issues:
        bullet_list = "\n".join(f"  - {i}" for i in issues)
        raise GitVettingError(
            "Refusing host-context git operation: the repository has git driver/filter "
            "configuration that could execute external programs on the host.\n"
            f"Detected:\n{bullet_list}\n\n"
            "This may be caused by agent-authored local config, committed .gitattributes, "
            "or pre-existing repository configuration.\n\n"
            "To proceed, please perform the operation manually using your git client "
            "(which can apply its own judgment about these settings).\n"
            "If this is a false positive (e.g. you use git-lfs with tracked .gitattributes), "
            "merge manually via: git merge --no-ff <branch>"
        )


def _vet_gitattributes_content(content: str, source_label: str, issues: list[str]) -> None:
    """Scan *content* (text of a .gitattributes file) for dangerous driver keywords.

    Args:
        content: Text content of the .gitattributes file.
        source_label: Human-readable label used in issue messages.
        issues: Mutable list; dangerous findings are appended in-place.
    """
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Skip unset directives like "-filter" which remove the attribute.
        # Unset tokens appear as individual whitespace-separated tokens that
        # start with "-{keyword}" (e.g. "-filter" or "-filter=lfs").  We must
        # NOT match on a substring of the whole line (e.g. "something-filter=")
        # because that would hide a genuine "filter=lfs" token on the same line
        # (fail-open, false-negative — violates fail-closed principle).
        # The gitattributes pattern is the first whitespace-delimited token; the
        # remaining tokens are the attributes.  Only attribute tokens (index 1+)
        # can be unset directives.
        attr_tokens = stripped.split()[1:]
        for keyword in _DANGEROUS_ATTR_KEYWORDS:
            # An unset directive is an attribute token that begins with
            # "-{keyword}" (e.g. keyword="filter=" → token "-filter" or
            # "-filter=lfs").  We check only the attribute tokens (index 1+),
            # never the pattern (index 0), so a pattern like
            # "something-filter=lfs" cannot falsely trigger the skip.
            if any(tok.startswith(f"-{keyword}") for tok in attr_tokens):
                continue
            if keyword in stripped:
                issues.append(f".gitattributes ({source_label}): {stripped!r}")
                break


async def _vet_all_gitattributes_in_tree(repo_path: Path, tree_ish: str, issues: list[str]) -> None:
    """Enumerate and vet ALL ``.gitattributes`` files in *tree_ish*.

    Uses ``git ls-tree -r --name-only`` to discover all tracked ``.gitattributes``
    paths (any directory depth), then ``git show <tree>:<path>`` for each.

    Args:
        repo_path: Absolute path to the git repository.
        tree_ish: A git tree-ish (branch name, "HEAD", commit SHA, etc.).
        issues: Mutable list; dangerous findings are appended in-place.

    Raises:
        GitVettingError: If ls-tree fails for a reason other than the tree not
            existing (fail-closed: an unreadable tree cannot be trusted).
    """
    # Enumerate all .gitattributes paths in the tree.
    try:
        ls_result = await run_git(
            "ls-tree",
            "-r",
            "--name-only",
            tree_ish,
            cwd=repo_path,
            check=False,
            harden=True,
            isolate_config=False,
        )
    except Exception as exc:
        raise GitVettingError(
            f"Vetting aborted: cannot enumerate tracked files in {tree_ish!r} ({exc!r}). "
            "Refusing operation to remain fail-closed."
        ) from exc

    if not ls_result.ok:
        if _is_tree_absent(ls_result.stderr):
            # Tree-ish genuinely does not exist (empty repo, non-existent
            # branch).  Treat as absent — not a failure.
            logger.debug(
                "ls-tree %r returned rc=%d for %s — tree absent, skipping",
                tree_ish,
                ls_result.returncode,
                repo_path,
            )
            return
        # ls-tree failed for some OTHER reason (corrupt object store, etc.).
        # We cannot enumerate attribute files, so we cannot vouch for the tree.
        # Fail closed — symmetric with _vet_single_tracked_gitattributes.
        raise GitVettingError(
            f"Vetting aborted: cannot enumerate tracked files in {tree_ish!r} "
            f"(rc={ls_result.returncode}): {ls_result.stderr.strip()}. "
            "Refusing operation to remain fail-closed."
        )

    # Match git's basename-honoring rule exactly: only a file literally named
    # ``.gitattributes`` (at any depth) is an attributes file.  A bare
    # ``endswith('.gitattributes')`` would over-match benign tracked files like
    # ``config.gitattributes`` (git does not honour those), causing fail-safe
    # over-refusals and redundant ``git show`` calls.
    attrs_paths = [
        p.strip()
        for p in ls_result.stdout.splitlines()
        if p.strip() == ".gitattributes" or p.strip().endswith("/.gitattributes")
    ]

    for attrs_path in attrs_paths:
        await _vet_single_tracked_gitattributes(repo_path, tree_ish, attrs_path, issues)


async def _vet_single_tracked_gitattributes(
    repo_path: Path, tree_ish: str, attrs_path: str, issues: list[str]
) -> None:
    """Read and vet a single tracked ``.gitattributes`` at *attrs_path* in *tree_ish*.

    Args:
        repo_path: Absolute path to the git repository.
        tree_ish: Tree-ish reference.
        attrs_path: Repo-relative path to the .gitattributes file.
        issues: Mutable list; dangerous findings are appended in-place.

    Raises:
        GitVettingError: If git show fails for a path that ls-tree reported as
            present (broken object store / unexpected error → fail-closed).
    """
    try:
        show_result = await run_git(
            "show",
            f"{tree_ish}:{attrs_path}",
            cwd=repo_path,
            check=False,
            harden=True,
            isolate_config=False,
        )
    except Exception as exc:
        raise GitVettingError(
            f"Vetting aborted: cannot read {attrs_path!r} from {tree_ish!r} ({exc!r}). "
            "Refusing operation to remain fail-closed."
        ) from exc

    if not show_result.ok:
        if _is_absent_from_tree(show_result.stderr):
            # Path is absent in this tree — not an error (ls-tree and show can
            # race in edge cases, or the path may be a submodule gitlink).
            return
        # ls-tree reported the file but git show failed — broken object store
        # or unexpected error.  Fail closed.
        raise GitVettingError(
            f"Vetting aborted: git show {tree_ish}:{attrs_path!r} failed "
            f"(rc={show_result.returncode}): {show_result.stderr.strip()}. "
            "Refusing operation to remain fail-closed."
        )

    _vet_gitattributes_content(show_result.stdout, f"{tree_ish}:{attrs_path}", issues)


def _scan_executable_hooks(repo_path: Path) -> None:
    """Log names of executable hook files under the repo's ``hooks`` dir.

    Synchronous — intended to be called via ``asyncio.to_thread``.  Resolves the
    real git dir (so linked worktrees, where ``.git`` is a file, are handled) and
    logs any executable hooks found, using the *resolved* hooks path so the debug
    message is accurate.  These hooks are already suppressed at run time via
    ``-c core.hooksPath=<empty>``; the log is for transparency only.

    Returns nothing; ``OSError`` is the only expected exception class.

    Args:
        repo_path: Absolute path to the git repository root.
    """
    try:
        from yukar.git.resolve import _resolve_git_dir

        git_dir = _resolve_git_dir(repo_path)
        hooks_path = git_dir / "hooks"
        if not hooks_path.is_dir():
            return
        exec_hooks = [
            f.name for f in hooks_path.iterdir() if f.is_file() and f.stat().st_mode & 0o111
        ]
        if exec_hooks:
            logger.debug("Suppressed executable hooks in %s: %s", hooks_path, exec_hooks)
    except OSError:
        logger.debug("Could not inspect git hooks dir for vetting", exc_info=True)


def _vet_working_tree_gitattributes(repo_path: Path, issues: list[str]) -> None:
    """Scan all working-tree ``.gitattributes`` files (uncommitted changes included).

    This covers the ``git add -A`` path inside ``commit()``: an agent can write
    a ``.gitattributes`` to the working tree without committing it; ``add -A``
    will stage it and the subsequent ``commit`` would activate the filters.

    Uses ``os.walk`` (off the event loop via ``asyncio.to_thread`` at the call
    site) instead of ``rglob`` so that ``.git/`` can be pruned mid-traversal.

    Only ``.git/`` is pruned: its contents are not user ``.gitattributes`` and
    ``.git/info/attributes`` is vetted separately.  We deliberately do **not**
    prune ``node_modules/`` (or any other heavyweight dir) here — this is the
    fail-closed scan that catches an *uncommitted* ``.gitattributes`` which
    ``git add -A`` would stage, and such a file can live anywhere that is not
    gitignored.  Skipping ``node_modules/`` would leave a hole for a poisoned
    ``node_modules/.gitattributes`` in a repo that does not ignore it.  The
    walk no longer blocks the event loop, so traversal cost is acceptable.

    Args:
        repo_path: Absolute path to the git repository root.
        issues: Mutable list; dangerous findings are appended in-place.
    """
    # Only .git is pruned — node_modules is deliberately NOT pruned because
    # a poisoned node_modules/.gitattributes staged via git add -A would be missed.
    try:
        for dirpath, dirnames, filenames in os.walk(repo_path):
            # Prune .git in-place so os.walk does not descend into it.
            dirnames[:] = [d for d in dirnames if d != ".git"]

            if ".gitattributes" not in filenames:
                continue

            attrs_file = Path(dirpath) / ".gitattributes"
            try:
                content = attrs_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                logger.debug("Could not read working-tree %s for vetting", attrs_file)
                continue

            rel = str(attrs_file.relative_to(repo_path))
            _vet_gitattributes_content(content, f"working-tree:{rel}", issues)
    except Exception:
        logger.debug("Could not scan working-tree .gitattributes for vetting", exc_info=True)


async def get_diff(
    repo_path: Path,
    mode: Literal["working", "epic"],
    repo_name: str | None = None,
    branch: str | None = None,
    default_branch: str = "main",
) -> DiffResult:
    """Get a diff in the requested mode.

    Args:
        repo_path: Absolute path to the git repo / worktree
        mode: 'working' = uncommitted changes; 'epic' = branch vs default
        repo_name: Registered repo name (used in DiffResult.repo).  Falls back
                   to the directory basename only when not provided (e.g. in
                   direct unit tests).
        branch: Required for epic mode — the epic branch name
        default_branch: The default branch for epic diff base
    """
    # Host/UI read path: use isolate_config=False so operator global config
    # (autocrlf, eol, etc.) is honoured — matching what commit() and merge()
    # will actually apply, so the displayed diff is faithful to the real change.
    if mode == "working":
        result = await run_git(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            cwd=repo_path,
            check=False,
            isolate_config=False,
        )
        unified = result.stdout
        files = await get_status(repo_path, isolate_config=False)
    else:
        if not branch:
            raise ValueError("branch is required for epic mode diff")
        # branch + default_branch are config/LLM-derived; reject leading-dash
        # refs and place them after --end-of-options so a crafted range-spec
        # cannot be parsed as a git option (e.g. --output=… to write a file).
        validate_git_ref(branch, what="branch")
        validate_git_ref(default_branch, what="default_branch")
        range_spec = f"{default_branch}...{branch}"
        result = await run_git(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--end-of-options",
            range_spec,
            cwd=repo_path,
            check=False,
            isolate_config=False,
        )
        unified = result.stdout
        # Get numstat for file list; -z gives raw NUL-delimited paths so renames
        # and non-ASCII names parse correctly.  --no-textconv is harmless here.
        numstat = await run_git(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--numstat",
            "-z",
            "--end-of-options",
            range_spec,
            cwd=repo_path,
            check=False,
            isolate_config=False,
        )
        files = [
            FileStat(path=fp, added=a, deleted=d) for a, d, fp in parse_numstat(numstat.stdout)
        ]

    total_added = sum(f.added for f in files)
    total_deleted = sum(f.deleted for f in files)

    # Use the explicitly supplied registered name; fall back to directory
    # basename only as a last resort (avoids silently returning wrong names).
    effective_repo_name = repo_name if repo_name is not None else repo_path.name

    return DiffResult(
        mode=mode,
        repo=effective_repo_name,
        branch=branch,
        files=files,
        unified_diff=unified,
        total_added=total_added,
        total_deleted=total_deleted,
    )


async def commit(
    repo_path: Path,
    message: str,
    author_name: str = "yukar",
    author_email: str = "yukar@localhost",
) -> str:
    """Stage all changes and commit. Returns the new commit SHA.

    This is the UI commit path (human-triggered), so we use
    ``isolate_config=False`` to preserve operator global config (git-lfs,
    autocrlf, etc.) while still applying hook/fsmonitor/env-scrub hardening.
    The worktree is vetted by ``_vet_host_git_context`` before this runs.
    """
    await _vet_host_git_context(repo_path)
    await run_git("add", "-A", cwd=repo_path, isolate_config=False)
    await run_git(
        "commit",
        "-m",
        message,
        cwd=repo_path,
        env=git_author_env(author_name, author_email),
        isolate_config=False,
    )
    sha_result = await run_git("rev-parse", "HEAD", cwd=repo_path, isolate_config=False)
    return sha_result.stdout.strip()


class MergeConflictError(Exception):
    """Raised when a merge results in conflicts."""

    def __init__(self, conflicts: list[str]) -> None:
        self.conflicts = conflicts
        super().__init__(f"Merge conflict in: {', '.join(conflicts)}")


async def merge(
    repo_path: Path,
    branch: str,
    message: str | None = None,
    author_name: str = "yukar",
    author_email: str = "yukar@localhost",
) -> str:
    """Merge branch into current HEAD using --no-ff.

    Raises MergeConflictError if there are conflicts (409).
    Returns the merge commit SHA on success.

    This is the human merge gate (UI-triggered), so we use
    ``isolate_config=False`` to preserve operator global config while still
    applying hook/fsmonitor/env-scrub hardening.  The target repo is vetted
    by ``_vet_host_git_context`` for dangerous driver configuration before the
    merge is attempted.

    Important: if the target branch or worktree has tracked ``.gitattributes``
    containing ``filter=``, ``merge=``, or ``diff=`` driver assignments, this
    function raises ``GitVettingError`` and refuses to merge.  Operators who
    have legitimate git-lfs or similar filters in tracked files must merge
    manually.  See the error message for details.
    """
    await _vet_host_git_context(repo_path, merge_branch=branch)
    commit_msg = message or f"Merge branch '{branch}'"
    try:
        await run_git(
            "merge",
            "--no-ff",
            "-m",
            commit_msg,
            branch,
            cwd=repo_path,
            env=git_author_env(author_name, author_email),
            isolate_config=False,
        )
    except GitError as e:
        if "CONFLICT" in e.result.stdout or "CONFLICT" in e.result.stderr:
            # Use --diff-filter=U to get the exact conflicting file paths.
            # This is more reliable than parsing "CONFLICT (content): Merge conflict in <path>"
            # lines which vary by conflict type and language settings.
            # IMPORTANT: isolate_config=False here is correct and intentional.
            # The diff/abort calls after a conflict-merge run in the host context
            # (same as the merge itself). isolate_config=True would drop operator
            # global config and could cause these cleanup calls to behave
            # differently from the merge.  --no-ext-diff + --no-textconv ensure
            # no external driver is invoked for the conflict-listing diff.
            # Do NOT change to isolate_config=True without updating this comment.
            unmerged = await run_git(
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--name-only",
                "--diff-filter=U",
                cwd=repo_path,
                check=False,
                isolate_config=False,
            )
            conflicts: list[str] = [p.strip() for p in unmerged.stdout.splitlines() if p.strip()]
            # Abort the merge to leave repo clean
            await run_git("merge", "--abort", cwd=repo_path, check=False, isolate_config=False)
            raise MergeConflictError(conflicts) from e
        raise

    merge_sha_result = await run_git("rev-parse", "HEAD", cwd=repo_path, isolate_config=False)
    return merge_sha_result.stdout.strip()


async def is_branch_merged(
    repo_path: Path,
    branch: str,
    default_branch: str = "main",
) -> bool:
    """Return True if *branch* has already been merged into *default_branch*.

    Uses ``git merge-base --is-ancestor <branch> <default_branch>``:
    - exit 0  → branch is an ancestor of default_branch (already merged)
    - exit 1  → not an ancestor (not yet merged)
    - Other   → git error (branch / default_branch may not exist; treated as
                not-merged so callers keep waiting rather than prematurely
                declaring the epic merged).

    The check also returns True when *branch* does not exist in the repo at
    all (the branch was already pruned after merging, which is semantically
    equivalent to "merged").

    This is the UI-triggered path (same as ``merge()``), so we use
    ``isolate_config=False`` to honour operator global config and apply the
    standard hardening env-scrub via ``run_git``.

    Args:
        repo_path: Absolute path to the git repository (the main checkout,
            not the epic worktree).
        branch: Epic branch name to check.
        default_branch: The repository's default branch name.

    Returns:
        True when the branch is an ancestor of (or identical to) the default
        branch, or when the branch simply does not exist; False otherwise.
    """
    validate_git_ref(branch, what="branch")
    validate_git_ref(default_branch, what="default_branch")

    # First check whether the branch exists at all.  A missing branch is
    # treated as "merged" (it was pruned after a successful merge).
    ref_check = await run_git(
        "rev-parse",
        "--verify",
        "--quiet",
        branch,
        cwd=repo_path,
        check=False,
        isolate_config=False,
    )
    if not ref_check.ok:
        # Branch does not exist in this repo → treat as merged.
        return True

    # Branch exists: check if it is an ancestor of the default branch.
    result = await run_git(
        "merge-base",
        "--is-ancestor",
        branch,
        default_branch,
        cwd=repo_path,
        check=False,
        isolate_config=False,
    )
    # rc=0 → ancestor (merged); rc=1 → not ancestor; other → error (not merged)
    return result.returncode == 0


async def get_repo_diff_summary(
    repo_path: Path,
    mode: Literal["working", "epic"],
    repo_name: str,
    branch: str | None = None,
    default_branch: str = "main",
) -> RepoDiffSummary:
    """Return lightweight numstat-only summary for a single repo.

    Does NOT fetch the full unified diff.  Intended for the multi-repo
    aggregation endpoint (spec §5.3) where bandwidth matters.

    Args:
        repo_path: Absolute path to the git repository or worktree.
        mode: ``"working"`` or ``"epic"`` (same semantics as ``get_diff``).
        repo_name: Registered repo name for the result.
        branch: Required for epic mode.
        default_branch: Base branch for epic mode three-dot range.

    Returns:
        A :class:`RepoDiffSummary` with ``files``, ``added``, ``deleted``.
        Returns zeroed summary on any git error (skips rather than 500).
    """
    # Host/UI read path: use isolate_config=False so this summary reflects
    # the same diff that commit() / merge() would produce (operator global
    # config such as autocrlf/eol is applied consistently).
    try:
        if mode == "working":
            numstat = await run_git(
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--numstat",
                "-z",
                "HEAD",
                cwd=repo_path,
                check=False,
                isolate_config=False,
            )
        else:
            if not branch:
                return RepoDiffSummary(repo=repo_name)
            # Reject leading-dash refs and fence the range-spec behind
            # --end-of-options (config/LLM-derived; see get_diff).
            validate_git_ref(branch, what="branch")
            validate_git_ref(default_branch, what="default_branch")
            range_spec = f"{default_branch}...{branch}"
            numstat = await run_git(
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--numstat",
                "-z",
                "--end-of-options",
                range_spec,
                cwd=repo_path,
                check=False,
                isolate_config=False,
            )

        file_stats = parse_numstat(numstat.stdout)
        return RepoDiffSummary(
            repo=repo_name,
            files=len(file_stats),
            added=sum(a for a, _d, _fp in file_stats),
            deleted=sum(d for _a, d, _fp in file_stats),
        )
    except GitError:
        # Epic branch may not exist yet, or worktree is in a transient state —
        # return zeroed summary so the multi-repo aggregation can continue.
        return RepoDiffSummary(repo=repo_name)
    except Exception:
        # Unexpected error — log and return zeroed summary so callers don't 500.
        logger.exception(
            "Unexpected error computing diff summary for repo %r (mode=%s, branch=%s)",
            repo_name,
            mode,
            branch,
        )
        return RepoDiffSummary(repo=repo_name)


async def get_diff_summary(
    repos: list[tuple[Path, str, str | None, str]],
    mode: Literal["working", "epic"],
) -> DiffSummary:
    """Aggregate diff statistics across multiple repos.

    Args:
        repos: List of ``(repo_path, repo_name, branch, default_branch)``
            tuples.  Pass ``branch=None`` for repos that have no epic branch
            yet — they are included with zero counts.
        mode: ``"working"`` or ``"epic"``.

    Returns:
        A :class:`DiffSummary` with per-repo breakdown and totals.
    """
    repo_summaries: list[RepoDiffSummary] = []
    for repo_path, repo_name, branch, default_branch in repos:
        summary = await get_repo_diff_summary(
            repo_path=repo_path,
            mode=mode,
            repo_name=repo_name,
            branch=branch,
            default_branch=default_branch,
        )
        repo_summaries.append(summary)

    total_files = sum(r.files for r in repo_summaries)
    total_added = sum(r.added for r in repo_summaries)
    total_deleted = sum(r.deleted for r in repo_summaries)

    return DiffSummary(
        repos=repo_summaries,
        total_files=total_files,
        total_added=total_added,
        total_deleted=total_deleted,
    )
