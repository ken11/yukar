"""Browser verification tools — Worker/Evaluator view of the trial's dev servers.

``make_browser_tools(ctx, owner_id)`` returns the bundle only when the
assigned repo declares a ``dev_server`` config; without one the agent never
sees these tools (docs/browser-verification-design.md §4).

This module is a thin strands adapter: the target is fixed from the agent's
AgentContext (single assigned repo) and every operation delegates to the
role-agnostic cores in ``browser_core`` — the phase-2 Manager bundle will
wrap the same cores with a ``repo`` argument instead (overview_tools pattern).

The agents hold no launch capability: ``browser_open`` asks the host
DevServerManager to ensure the user-declared services (idempotent), and the
BrowserSession enforces the fail-closed egress gate on every request the page
makes.  Action tools return a terse result only — page state is fetched
explicitly with ``browser_read`` (§4.0 token-economy lesson).

Design decisions carried here:
- Evaluator gets the full bundle including click/type — "read-only" is a
  *repository* invariant and browser interaction never touches the worktree.
- Screenshots are a separate tool returning an image block; they cost far
  more tokens than the textual snapshot and are meant for design checks.
"""

from __future__ import annotations

from typing import Any

from strands import tool

from yukar.agents.context import AgentContext
from yukar.agents.tools import browser_core
from yukar.agents.tools.browser_core import BrowserTarget, TreeEnsurer, load_dev_server_config
from yukar.config import paths
from yukar.preview.browser import get_browser_session_manager
from yukar.preview.manager import get_dev_server_manager


def _target_from_ctx(ctx: AgentContext, owner_id: str) -> BrowserTarget:
    return BrowserTarget(
        workspace_root=ctx.workspace_root,
        project_id=ctx.project_id,
        epic_id=ctx.epic_id,
        trial_id=paths.trial_id_of_worktree(ctx.worktree_path),
        repo_name=ctx.repo_name,
        worktree_path=ctx.worktree_path,
        owner_id=owner_id,
    )


async def make_browser_tools_if_configured(
    ctx: AgentContext, owner_id: str, ensure_tree: TreeEnsurer | None = None
) -> list[Any]:
    """Return the browser bundle only when the assigned repo declares dev_server.

    Repos without a launch config never expose these tools, so the agent has
    nothing to reason about (design §4.2).
    """
    if await load_dev_server_config(ctx.workspace_root, ctx.project_id, ctx.repo_name) is None:
        return []
    return make_browser_tools(ctx, owner_id, ensure_tree=ensure_tree)


def make_browser_tools(
    ctx: AgentContext, owner_id: str, ensure_tree: TreeEnsurer | None = None
) -> list[Any]:
    """Build the browser bundle for one agent, bound to its trial worktree.

    Args:
        ctx: Agent context (fixes project/epic/repo/worktree).
        owner_id: Worker/Evaluator thread id — keys this agent's own page so
            parallel agents never share navigation state.
        ensure_tree: Creates a DEPENDENCY repo's worktree for this trial when a
            ``{port:repo/service}`` reference targets a repo with no worktree
            yet (the agent's OWN repo always has one — it runs inside it).
            Without it a missing dependency worktree falls back to the base
            checkout (see :data:`~yukar.agents.tools.browser_core.TreeEnsurer`).

    Returns:
        The tool list, or ``[]`` when the singletons are not initialised
        (outside a running app).
    """
    if get_dev_server_manager() is None or get_browser_session_manager() is None:
        return []

    target = _target_from_ctx(ctx, owner_id)

    @tool
    async def browser_open(service: str | None = None) -> dict[str, Any]:
        """Start the repo's declared dev services (if needed) and open the app.

        The host launches the services exactly as configured in the repo's
        dev-server settings and waits until each is ready; you receive the
        resulting URL plus a snapshot of the page. Re-calling is cheap — a
        healthy server is reused, a crashed one is relaunched. Services of
        other repos referenced via {port:repo/service} are started first,
        automatically, and the page may call their origins.

        If the user captured a login for this repo, the page starts already
        authenticated. If you hit a login wall instead, do NOT guess or invent
        credentials — report it and ask the user to log in via the repos
        page's manual browser action.

        Args:
            service: Which declared service's origin to open. Defaults to the
                first service in the config.

        Returns:
            url, title, and a ref-annotated page snapshot (see browser_read).
        """
        return await browser_core.open_app(target, service, ensure_tree=ensure_tree)

    @tool
    async def browser_navigate(url: str) -> dict[str, Any]:
        """Navigate the page to a URL or an absolute path on the current origin.

        Only the trial's own service origins (plus origins explicitly allowed
        in the repo settings) can be visited; everything else is blocked.

        Args:
            url: Absolute URL, or a path starting with "/" resolved against
                the current page's origin.

        Returns:
            url, title, and a fresh page snapshot.
        """
        return await browser_core.navigate(target, url)

    @tool
    async def browser_read() -> dict[str, Any]:
        """Read the current page as a ref-annotated accessibility outline.

        Each interactive element carries ``[ref=eN]`` — pass that ref to
        browser_click / browser_type. Refs are reassigned on every read, so
        after the page changes take a fresh read before interacting.

        Returns:
            url, title, and the snapshot outline.
        """
        return await browser_core.read_page(target)

    @tool
    async def browser_click(ref: str) -> dict[str, Any]:
        """Click the element with the given snapshot ref.

        Returns a terse confirmation only — call browser_read for the
        resulting page state (cheaper than returning it on every action).

        Args:
            ref: Element ref from the latest snapshot (e.g. "e12").
        """
        return await browser_core.click(target, ref)

    @tool
    async def browser_type(ref: str, text: str, press_enter: bool = False) -> dict[str, Any]:
        """Fill the input/textarea with the given snapshot ref.

        Never type guessed/invented credentials, and never fill OTP or
        CAPTCHA fields — logging in is the user's move, not yours.

        Args:
            ref: Element ref from the latest snapshot (e.g. "e7").
            text: Text to set (replaces the current value).
            press_enter: Press Enter after filling (submits most forms).
        """
        return await browser_core.type_text(target, ref, text, press_enter)

    @tool
    async def browser_press(key: str) -> dict[str, Any]:
        """Press a keyboard key on the focused element (e.g. "Enter", "Tab", "Escape").

        Args:
            key: Playwright key name.
        """
        return await browser_core.press_key(target, key)

    @tool
    async def browser_screenshot(
        full_page: bool = False, save: bool = False, label: str = ""
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
        """
        return await browser_core.screenshot(
            target, full_page, save=save, label=label or None
        )

    @tool
    async def browser_console(lines: int = 50) -> dict[str, Any]:
        """Read recent browser console output (log/warn/error + page errors).

        Args:
            lines: Maximum number of recent entries to return.
        """
        return await browser_core.console_tail(target, lines)

    @tool
    async def server_logs(service: str | None = None, lines: int = 100) -> dict[str, Any]:
        """Read the dev server's stdout/stderr tail (host-captured).

        Args:
            service: One declared service name; omit for all services.
            lines: Maximum lines per service.
        """
        return await browser_core.server_log_tail(target, service, lines)

    @tool
    async def server_stop() -> dict[str, Any]:
        """Stop this trial's dev servers and close the browser (idempotent).

        Use when verification is done to free resources early; the host also
        stops everything automatically when the run ends. A later
        browser_open simply starts fresh.
        """
        return await browser_core.stop_servers(target)

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
