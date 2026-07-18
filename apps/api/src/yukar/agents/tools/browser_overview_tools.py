"""Browser verification tools for overview roles (Manager / Reviewer).

The Worker/Evaluator bundle (``browser_tools.py``) fixes one agent to its
single assigned repo's trial worktree via AgentContext.  A Manager or Reviewer
oversees the WHOLE epic without a worktree of its own, so this bundle mirrors
``overview_tools``: ONE tool of each kind takes a required ``repo`` argument
and dispatches to that repo's browser target.  Every operation delegates to
the role-agnostic cores in ``browser_core`` — there is exactly one
implementation of the browser logic.

Target resolution happens PER CALL, not at bundle build time.  The active
trial's worktree for a repo is created by the first dispatch that touches it —
possibly in the MIDDLE of the manager run.  A target frozen at build time
would keep pointing at the base checkout even after the branch has real work,
and ``DevServerManager.ensure()`` reuses a healthy entry by KEY regardless of
the path it was launched in, so the stale tree would keep being served.
Resolving on every call gives:

- trial worktree exists → verify the branch's actual state (the real trial id
  keys the servers, so a Worker/Evaluator of the same trial shares them);
- no worktree yet (turn 0) → the worktree is CREATED eagerly via the
  ``ensure_tree`` callback (same machinery as dispatch), so the server always
  runs in an isolated per-trial tree.  Launching in the repo's base checkout
  is a collision hazard: the base is a shared directory — the user's own dev
  server may already run there, every epic would target the same tree, and
  Next.js refuses a second dev instance from one directory — and the launch
  would write build artifacts into the user's checkout.  At turn 0 the fresh
  worktree equals the default-branch tip, so "reproduce current behaviour
  before planning" still works.
- no active trial at all (every manager trial archived) → fall back to the
  repo's base checkout, keyed under the ``__base__`` sentinel trial id (no
  trial exists to own a worktree).  A lingering base entry is swept by the
  run-end ``stop_for_epic`` hook like every other entry of the epic.

Only repos that declare a ``dev_server`` config are dispatchable; when no
registered repo declares one the bundle is not built at all, so the agent has
nothing to reason about (design §4.2).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from strands import tool

from yukar.agents.tools import browser_core
from yukar.agents.tools.browser_core import BASE_TRIAL_ID, BrowserTarget, TreeEnsurer
from yukar.agents.tools.response_builder import make_error
from yukar.agents.trials import resolve_active_trial_id
from yukar.config import paths
from yukar.models.epic import Epic
from yukar.preview.browser import get_browser_session_manager
from yukar.preview.manager import DevServerError, get_dev_server_manager
from yukar.storage import project_repo

# Registry/session key for base-checkout targets — shared with browser_core so
# dependency-repo resolution uses the same sentinel.
_BASE_TRIAL_ID = BASE_TRIAL_ID


async def make_browser_overview_tools(
    root: str,
    project_id: str,
    epic_id: str,
    epic: Epic,
    owner_id: str,
    ensure_tree: TreeEnsurer | None = None,
) -> list[Any]:
    """Build the repo-dispatching browser bundle for a Manager / Reviewer.

    Args:
        root: Workspace root path.
        project_id: The project identifier.
        epic_id: The epic identifier.
        epic: The loaded epic (active-trial resolution needs it).
        owner_id: Manager/Reviewer thread id — keys this agent's own page so
            it never shares navigation state with Workers/Evaluators.
        ensure_tree: Creates the active trial's worktree for a repo when it
            does not exist yet (orchestrator-provided; see
            :data:`~yukar.agents.tools.browser_core.TreeEnsurer`).  Without it
            a missing worktree falls back to the repo's base checkout — the
            pre-eager-creation behaviour kept for callers outside a run.

    Returns:
        The tool list, or ``[]`` when the preview singletons are not
        initialised or no registered repo declares a ``dev_server`` config.
    """
    if get_dev_server_manager() is None or get_browser_session_manager() is None:
        return []

    configured = {
        r.name: r for r in await project_repo.list_repos(root, project_id) if r.dev_server
    }
    if not configured:
        return []
    repo_names = sorted(configured)

    # The active trial is fixed for the lifetime of a run (archiving is
    # blocked while a run is active), so resolve it once; only the EXISTENCE
    # of each repo's worktree is re-checked per call.
    trial_id = await resolve_active_trial_id(root, project_id, epic_id, epic)

    def _base_target(repo: str, base_path: Path) -> BrowserTarget:
        return BrowserTarget(
            workspace_root=root,
            project_id=project_id,
            epic_id=epic_id,
            trial_id=_BASE_TRIAL_ID,
            repo_name=repo,
            worktree_path=base_path,
            # SessionKey has no repo axis (TrialKey does), so fold the repo into
            # the owner: each repo gets its OWN page and egress allow-set.  A
            # shared page would let browser_read(repo="a") return repo b's DOM
            # and union the two repos' allowed origins into one allow-set.
            owner_id=f"{owner_id}/{repo}",
        )

    async def _target_for(repo: str) -> tuple[BrowserTarget | None, dict[str, Any] | None]:
        # `repo` is ALWAYS required — auto-picking would silently verify the
        # wrong app when the intended repo differs (same rule as overview_tools).
        if not repo:
            return None, make_error(
                f"`repo` is required — name the repo to verify. Available: {repo_names}."
            )
        repo_obj = configured.get(repo)
        if repo_obj is None:
            return None, make_error(
                f"unknown repo {repo!r} or it has no dev-server config. "
                f"Available: {repo_names}."
            )
        if trial_id is not None:
            worktree = paths.worktree_dir(root, project_id, epic_id, trial_id, repo)
            if not await asyncio.to_thread(worktree.is_dir) and ensure_tree is not None:
                # Eagerly create the trial worktree rather than launching in
                # the shared base checkout (duplicate-instance collision +
                # build-artifact pollution — see module docstring).
                try:
                    worktree = await ensure_tree(repo)
                except DevServerError as exc:
                    return None, make_error(str(exc))
            if await asyncio.to_thread(worktree.is_dir):
                return (
                    BrowserTarget(
                        workspace_root=root,
                        project_id=project_id,
                        epic_id=epic_id,
                        trial_id=trial_id,
                        repo_name=repo,
                        worktree_path=worktree,
                        owner_id=f"{owner_id}/{repo}",  # per-repo page — see _base_target
                    ),
                    None,
                )
        base_path = Path(repo_obj.path)
        if not await asyncio.to_thread(base_path.is_dir):
            return None, make_error(
                f"repo {repo!r} has neither a trial worktree nor a readable base "
                f"checkout on disk — nothing to launch the dev server in."
            )
        return _base_target(repo, base_path), None

    @tool
    async def browser_open(repo: str = "", service: str | None = None) -> dict[str, Any]:
        """Start the repo's declared dev services (if needed) and open the app.

        The host launches the services exactly as configured in the repo's
        dev-server settings and waits until each is ready; you receive the
        resulting URL plus a snapshot of the page.  Re-calling is cheap — a
        healthy server is reused, a crashed one is relaunched.  Services of
        other repos referenced via {port:repo/service} are started first,
        automatically, and the page may call their origins.

        What you are looking at: the epic branch's CURRENT worktree when one
        exists (i.e. the work implemented so far); before any task has run,
        the repo's base checkout (the default-branch state) — useful to
        reproduce current behaviour before planning.  When a worktree appears
        later in the run, call browser_open again to switch to it.

        If the user captured a login for this repo, the page starts already
        authenticated. If you hit a login wall instead, do NOT guess or invent
        credentials — report it and ask the user to log in via the repos
        page's manual browser action.

        Args:
            repo: Required — name the repo whose app to verify.
            service: Which declared service's origin to open. Defaults to the
                first service in the config.

        Returns:
            url, title, and a ref-annotated page snapshot (see browser_read).
        """
        target, err = await _target_for(repo)
        if err is not None:
            return err
        assert target is not None  # _target_for returns a target whenever err is None
        return await browser_core.open_app(target, service, ensure_tree=ensure_tree)

    @tool
    async def browser_navigate(url: str, repo: str = "") -> dict[str, Any]:
        """Navigate the page to a URL or an absolute path on the current origin.

        Only the launched service origins (plus origins explicitly allowed in
        the repo settings) can be visited; everything else is blocked.

        Args:
            url: Absolute URL, or a path starting with "/" resolved against
                the current page's origin.
            repo: Required — the repo whose page to act on (see browser_open).
        """
        target, err = await _target_for(repo)
        if err is not None:
            return err
        assert target is not None  # _target_for returns a target whenever err is None
        return await browser_core.navigate(target, url)

    @tool
    async def browser_read(repo: str = "") -> dict[str, Any]:
        """Read the current page as a ref-annotated accessibility outline.

        Each interactive element carries ``[ref=eN]`` — pass that ref to
        browser_click / browser_type.  Refs are reassigned on every read, so
        after the page changes take a fresh read before interacting.

        Args:
            repo: Required — the repo whose page to read (see browser_open).
        """
        target, err = await _target_for(repo)
        if err is not None:
            return err
        assert target is not None  # _target_for returns a target whenever err is None
        return await browser_core.read_page(target)

    @tool
    async def browser_click(ref: str, repo: str = "") -> dict[str, Any]:
        """Click the element with the given snapshot ref.

        Returns a terse confirmation only — call browser_read for the
        resulting page state (cheaper than returning it on every action).

        Args:
            ref: Element ref from the latest snapshot (e.g. "e12").
            repo: Required — the repo whose page to act on (see browser_open).
        """
        target, err = await _target_for(repo)
        if err is not None:
            return err
        assert target is not None  # _target_for returns a target whenever err is None
        return await browser_core.click(target, ref)

    @tool
    async def browser_type(
        ref: str, text: str, press_enter: bool = False, repo: str = ""
    ) -> dict[str, Any]:
        """Fill the input/textarea with the given snapshot ref.

        Never type guessed/invented credentials, and never fill OTP or
        CAPTCHA fields — logging in is the user's move, not yours.

        Args:
            ref: Element ref from the latest snapshot (e.g. "e7").
            text: Text to set (replaces the current value).
            press_enter: Press Enter after filling (submits most forms).
            repo: Required — the repo whose page to act on (see browser_open).
        """
        target, err = await _target_for(repo)
        if err is not None:
            return err
        assert target is not None  # _target_for returns a target whenever err is None
        return await browser_core.type_text(target, ref, text, press_enter)

    @tool
    async def browser_press(key: str, repo: str = "") -> dict[str, Any]:
        """Press a keyboard key on the focused element (e.g. "Enter", "Tab", "Escape").

        Args:
            key: Playwright key name.
            repo: Required — the repo whose page to act on (see browser_open).
        """
        target, err = await _target_for(repo)
        if err is not None:
            return err
        assert target is not None  # _target_for returns a target whenever err is None
        return await browser_core.press_key(target, key)

    @tool
    async def browser_screenshot(
        full_page: bool = False, save: bool = False, label: str = "", repo: str = ""
    ) -> dict[str, Any]:
        """Take a screenshot of the current page (returns an image).

        Costs far more tokens than browser_read — use it when you need to
        judge visual design/layout, not to locate elements or read text.

        Set save=True to also keep this screenshot in the epic's docs (the
        user can then review it on the Docs page). Only save shots worth
        keeping — saving every one wastes disk. Unsaved shots are still shown
        to you here; they just leave no file behind.

        Args:
            full_page: Capture the whole scrollable page instead of the
                1280x800 viewport.
            save: Persist this screenshot under the epic docs folder.
            label: Short slug for the saved file's name (e.g. "login-page");
                defaults to the repo name. Ignored unless save=True.
            repo: Required — the repo whose page to capture (see browser_open).
        """
        target, err = await _target_for(repo)
        if err is not None:
            return err
        assert target is not None  # _target_for returns a target whenever err is None
        return await browser_core.screenshot(target, full_page, save=save, label=label or None)

    @tool
    async def browser_console(lines: int = 50, repo: str = "") -> dict[str, Any]:
        """Read recent browser console output (log/warn/error + page errors).

        Args:
            lines: Maximum number of recent entries to return.
            repo: Required — the repo whose page to read (see browser_open).
        """
        target, err = await _target_for(repo)
        if err is not None:
            return err
        assert target is not None  # _target_for returns a target whenever err is None
        return await browser_core.console_tail(target, lines)

    @tool
    async def server_logs(
        service: str | None = None, lines: int = 100, repo: str = ""
    ) -> dict[str, Any]:
        """Read the dev server's stdout/stderr tail (host-captured).

        Args:
            service: One declared service name; omit for all services.
            lines: Maximum lines per service.
            repo: Required — the repo whose servers to read (see browser_open).
        """
        target, err = await _target_for(repo)
        if err is not None:
            return err
        assert target is not None  # _target_for returns a target whenever err is None
        return await browser_core.server_log_tail(target, service, lines)

    @tool
    async def server_stop(repo: str = "") -> dict[str, Any]:
        """Stop the repo's dev servers and close your page (idempotent).

        Use when verification is done to free resources early; the host also
        stops everything automatically when the run ends.  A later
        browser_open simply starts fresh.

        Args:
            repo: Required — the repo whose servers to stop.
        """
        target, err = await _target_for(repo)
        if err is not None:
            return err
        assert target is not None  # _target_for returns a target whenever err is None
        result = await browser_core.stop_servers(target)
        if target.trial_id != _BASE_TRIAL_ID:
            # A base-checkout server launched before the worktree appeared is
            # no longer reachable through the resolved target — sweep it too so
            # one server_stop frees everything this bundle started for the repo.
            repo_obj = configured[repo]
            await browser_core.stop_servers(_base_target(repo, Path(repo_obj.path)))
        return result

    return [
        browser_open,
        browser_navigate,
        browser_read,
        browser_click,
        browser_type,
        browser_press,
        browser_screenshot,
        browser_console,
        server_logs,
        server_stop,
    ]
