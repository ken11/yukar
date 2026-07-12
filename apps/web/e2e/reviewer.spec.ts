/**
 * Reviewer E2E smoke — real-device verification of the read-only Reviewer
 * (Phase 2 of the trial/session decoupling: an independent reviewer the user
 * spawns after the run to check the branch and report back).
 *
 * Verification items:
 *   1. setup: create project + epic → fake run until work done (the manager
 *      run parks in "waiting" — a conversation never completes)
 *   2. "Ask Reviewer" works while the manager run is parked in waiting (the
 *      parked run is shelved, not a 409) → creates a reviewer thread
 *      (role=reviewer) and navigates to it; the composer is shown (repliable).
 *      Live attribution (same SPA session, no reload): the report streams
 *      into the REVIEWER thread, the your-turn banner appears there with
 *      Reviewer wording, and an SPA transition to the Trial thread shows NO
 *      misattributed banner (the old attribution bug).
 *   3. The reviewer runs read-only: it inspects read_branch_diff, reports in
 *      plain BODY TEXT and parks at "waiting", WITHOUT changing epic.status or
 *      epic.active_thread_id (the manager trial stays the active trial).
 *      REST-restore attribution: a fresh load of the reviewer thread shows the
 *      Reviewer-worded banner (RunState.role).
 *   4. The reviewer thread is listed in the sidebar; the manager thread keeps
 *      its composer (a reviewer run must not hijack the active-trial pointer)
 *      and shows NO your-turn banner on a fresh load (reload misattribution
 *      regression guard)
 *   5. The user can reply to the reviewer (post_message routes in reviewer mode)
 *   6. A reviewer parked in waiting does NOT block manager operations
 *      (new-trial creation succeeds — shelving semantics)
 */

import { expect, test } from "@playwright/test";
import { SEED } from "./seed";
import { getRunState, waitForWorkDone } from "./wait-helpers";

const SHOTS = "playwright-report";

// Your-turn banner wordings (ja locale — same approach as ask-user.spec).
// Thread-level (thread-chat-inner) and epic-level (epic-shell) neutral texts
// are identical strings; the Reviewer variants differ per surface.
const NEUTRAL_TURN_BANNER = "あなたの番です — 返信するとエージェントが続けます";
const REVIEWER_TURN_BANNER = "Reviewer の報告があります — 返信すると続けます";
const REVIEWER_SHELL_BANNER = "Reviewer の報告があります — 会話を開いて確認してください";

test.describe
  .serial("reviewer fake smoke", () => {
    const state = {
      projectId: "",
      epicId: "",
      managerThreadId: "",
      reviewerId: "",
    };

    test("setup: create project + epic, run until work is done", async ({ page }) => {
      await page.goto("/projects");
      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.getByTestId("project-name-input").fill("reviewer-project");
      await page.getByTestId("repo-path-input-0").fill(SEED.repoDirs.reviewer);
      await page.getByTestId("form-dialog-submit").click();

      const row = page
        .locator('[data-testid^="project-row-"]')
        .filter({ hasText: "reviewer-project" });
      await expect(row).toBeVisible({ timeout: 15_000 });
      state.projectId = (await row.getAttribute("data-testid"))?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}`);
      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.getByTestId("epic-title-input").fill("Reviewer epic");
      await page.getByTestId("epic-description-input").fill("Create hello.py and util.py.");
      await page.getByTestId("epic-ac-input").fill("hello.py exists and prints 'hello'");
      await page.getByTestId("form-dialog-submit").click();

      await page.waitForURL(/\/projects\/[^/]+\/epics\/[^/]+/, { timeout: 15_000 });
      state.epicId = page.url().match(/\/epics\/([^/?#]+)/)?.[1] ?? "";
      expect(state.epicId).toBeTruthy();

      await page.getByTestId("start-run-btn").click();

      // Standard work-done wait: the run parks in "waiting" and all tasks are done.
      await waitForWorkDone(page, state.projectId, state.epicId);

      // The run establishes the active-trial pointer (epic.active_thread_id).
      const eRes = await page.request.get(`/api/projects/${state.projectId}/epics/${state.epicId}`);
      const epic: { active_thread_id?: string | null } = await eRes.json();
      state.managerThreadId = epic.active_thread_id ?? "manager";
      expect(state.managerThreadId).toBeTruthy();
    });

    test("Ask Reviewer (while the manager run is parked) → creates a reviewer thread and navigates to it", async ({
      page,
    }) => {
      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicId}/threads/${state.managerThreadId}`,
      );
      await expect(page.getByTestId("start-review-btn")).toBeVisible({ timeout: 15_000 });

      // Stream continuity: starting the Reviewer shelves the parked
      // manager run. A shelve no longer publishes the SSE sentinel,
      // so the epic EventSource opened by this page load must stay connected
      // — count any NEW connection to the epic event stream from here on.
      let sseReconnects = 0;
      page.on("request", (req) => {
        if (req.url().includes(`/epics/${state.epicId}/run/events`)) {
          sseReconnects += 1;
        }
      });

      const [reviewResponse] = await Promise.all([
        page.waitForResponse(
          (resp) => resp.url().endsWith("/review") && resp.request().method() === "POST",
          { timeout: 20_000 },
        ),
        page.getByTestId("start-review-btn").first().click(),
      ]);
      expect(reviewResponse.status(), "POST /review should return 201").toBe(201);
      const reviewer = await reviewResponse.json();
      state.reviewerId = reviewer.id;
      expect(reviewer.role, "The new thread is a reviewer conversation").toBe("reviewer");
      expect(reviewer.id, "Reviewer thread is distinct from the manager trial").not.toBe(
        state.managerThreadId,
      );

      await page.waitForURL(new RegExp(`/threads/${state.reviewerId}`), { timeout: 15_000 });

      // A reviewer conversation is repliable — the composer is shown.
      await expect(
        page.getByTestId("thread-composer"),
        "Reviewer thread shows a reply composer",
      ).toBeVisible({ timeout: 20_000 });

      // ---- Live attribution (same SPA session — NO reload from here) ----

      // The reviewer's report streams into ITS OWN thread live (SSE events
      // carry the reviewer thread_id; before the attribution split this
      // required a fresh load).
      await expect(
        page.getByText("Reviewed the branch").first(),
        "Reviewer report streams live into the reviewer thread (no reload)",
      ).toBeVisible({ timeout: 60_000 });

      // The turn end parks the run in "waiting" → the your-turn banner appears
      // on the REVIEWER thread and names the Reviewer (RunState.role via the
      // REST role refresh triggered by the your-turn signal).
      await expect(
        page.getByText(REVIEWER_TURN_BANNER),
        "Thread-level banner names the Reviewer",
      ).toBeVisible({ timeout: 30_000 });
      await expect(
        page.getByText(REVIEWER_SHELL_BANNER),
        "Epic-level banner names the Reviewer",
      ).toBeVisible({ timeout: 15_000 });

      // SPA transition to the Trial thread (sidebar link — still no reload):
      // the banner must NOT follow. The parked marker belongs to the reviewer
      // conversation; showing it on the Trial thread was the old
      // misattribution bug (managerThreadId doubling as run attribution).
      const threadsNav = page.locator('nav[aria-label="Threads"]');
      await expect(threadsNav).toBeVisible({ timeout: 10_000 });
      // .first(): the sidebar has two links to the trial (the thread row and
      // the agent-state tree row) — either performs the same SPA navigation.
      await threadsNav.locator(`a[href$="/threads/${state.managerThreadId}"]`).first().click();
      await page.waitForURL(new RegExp(`/threads/${state.managerThreadId}`), {
        timeout: 15_000,
      });
      await expect(
        page.getByTestId("thread-composer"),
        "Trial thread keeps its composer during the parked reviewer run",
      ).toBeVisible({ timeout: 20_000 });
      await expect(
        page.getByText(NEUTRAL_TURN_BANNER),
        "No neutral your-turn banner is misattributed to the Trial thread",
      ).toHaveCount(0);
      await expect(
        page.getByText(REVIEWER_TURN_BANNER),
        "No reviewer reply banner on the Trial thread",
      ).toHaveCount(0);
      // The epic-level notification stays visible — it points at the reviewer
      // conversation (correct attribution, not suppression).
      await expect(page.getByText(REVIEWER_SHELL_BANNER)).toBeVisible();

      // Stream continuity: the whole shelve → reviewer-run → park → SPA-navigation window
      // above ran on the ORIGINAL EventSource — no reconnect was needed
      // (previously the shelve severed the stream and forced one).
      expect(
        sseReconnects,
        "Shelving the manager run must not sever the epic SSE stream (no EventSource reconnect)",
      ).toBe(0);
    });

    test("Reviewer runs read-only: reports in body text, parks in waiting, epic untouched", async ({
      page,
    }) => {
      expect(state.reviewerId).toBeTruthy();

      // The reviewer inspects the diff then reports in plain body text and
      // ends its turn → the run parks in "waiting" (your turn). Gate on the
      // reviewer thread driving the run so the wait cannot match the shelved
      // manager run's earlier "waiting".
      await expect
        .poll(
          async () => {
            const s = await getRunState(page, state.projectId, state.epicId);
            return `${s.status}:${s.thread_id ?? ""}`;
          },
          { timeout: 60_000, intervals: [500, 1000, 1000] },
        )
        .toBe(`waiting:${state.reviewerId}`);

      // The reviewer must NOT drive the epic lifecycle: status stays open and
      // the active trial stays the manager thread (not the reviewer).
      const eRes = await page.request.get(`/api/projects/${state.projectId}/epics/${state.epicId}`);
      const epic: { status: string; active_thread_id?: string | null } = await eRes.json();
      expect(epic.status, "Reviewer leaves the epic open").toBe("open");
      expect(epic.active_thread_id, "Reviewer does not become the active trial").toBe(
        state.managerThreadId,
      );

      // The reviewer's read-only conversation was persisted (read_branch_diff +
      // fs_read + the report). The report is an ordinary assistant message —
      // there is no pending_question carrier any more.
      const mRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}/threads/${state.reviewerId}`,
      );
      const messages: unknown[] = await mRes.json();
      expect(messages.length, "Reviewer produced a conversation").toBeGreaterThan(0);
      // The reviewer's worktree-backed fs_read read hello.py from the active
      // manager trial's worktree — its content ("greet") appears in the tool
      // result, proving the read-only worktree tools are wired end-to-end.
      expect(
        JSON.stringify(messages),
        "Reviewer's fs_read returned hello.py content from the manager trial worktree",
      ).toContain("greet");
      expect(
        JSON.stringify(messages),
        "The reviewer's verdict is persisted as a plain assistant message",
      ).toContain("Reviewed the branch");

      // The report body is visible in the conversation UI on a FRESH load —
      // this exercises the REST-restore path (thread history + GET /run/state
      // with thread_id + role), as opposed to the live SSE path already
      // asserted in the previous test.
      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicId}/threads/${state.reviewerId}`,
      );
      await expect(
        page.getByTestId("agent-message").filter({ hasText: "Reviewed the branch" }).first(),
        "The reviewer's report text renders as an agent bubble",
      ).toBeVisible({ timeout: 20_000 });

      // REST-restore attribution: the reloaded reviewer thread shows the
      // your-turn banner with Reviewer wording (RunState.role — no SSE replay).
      await expect(
        page.getByText(REVIEWER_TURN_BANNER),
        "Fresh load restores the Reviewer-worded banner from REST",
      ).toBeVisible({ timeout: 15_000 });
      await expect(
        page.getByText(REVIEWER_SHELL_BANNER),
        "Fresh load restores the epic-level Reviewer banner from REST",
      ).toBeVisible({ timeout: 15_000 });

      await page.screenshot({ path: `${SHOTS}/reviewer-awaiting.png`, fullPage: true });
    });

    test("Reviewer is listed; manager thread keeps its composer", async ({ page }) => {
      expect(state.reviewerId).toBeTruthy();

      // Reviewer conversation appears in the thread sidebar.
      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicId}/threads/${state.reviewerId}`,
      );
      const threadsNav = page.locator('nav[aria-label="Threads"]');
      await expect(threadsNav).toBeVisible({ timeout: 10_000 });
      await expect(
        threadsNav.locator(`a[href$="/threads/${state.reviewerId}"]`),
        "Reviewer thread is listed in the sidebar",
      ).toBeVisible();

      // A reviewer run must not hijack the active-trial pointer: the manager
      // thread still shows its composer (regression guard for the reviewer run
      // overwriting RunState.thread_id).
      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicId}/threads/${state.managerThreadId}`,
      );
      await expect(
        page.getByTestId("thread-composer"),
        "Manager thread keeps its composer after a reviewer run",
      ).toBeVisible({ timeout: 20_000 });

      // Reload-misattribution regression guard (the original attribution bug): a fresh
      // load of the TRIAL thread while the reviewer run is parked must NOT
      // show a your-turn banner on the trial — the parked marker belongs to
      // the reviewer conversation (REST restore uses RunState.thread_id,
      // never the active-trial fallback).
      await expect(
        page.getByText(REVIEWER_SHELL_BANNER),
        "Epic-level banner still names the Reviewer on the trial page",
      ).toBeVisible({ timeout: 15_000 });
      await expect(
        page.getByText(NEUTRAL_TURN_BANNER),
        "No neutral your-turn banner on a fresh load of the Trial thread",
      ).toHaveCount(0);
      await expect(
        page.getByText(REVIEWER_TURN_BANNER),
        "No reviewer reply banner on a fresh load of the Trial thread",
      ).toHaveCount(0);
    });

    test("User can reply to the reviewer (reviewer-mode routing)", async ({ page }) => {
      expect(state.reviewerId).toBeTruthy();
      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicId}/threads/${state.reviewerId}`,
      );

      const composer = page.getByTestId("thread-composer");
      await expect(composer).toBeVisible({ timeout: 15_000 });
      await composer.fill("Thanks — please double-check util.py too.");

      const [postResponse] = await Promise.all([
        page.waitForResponse(
          (resp) =>
            resp.url().includes(`/threads/${state.reviewerId}/messages`) &&
            resp.request().method() === "POST",
          { timeout: 15_000 },
        ),
        composer.press("Meta+Enter"),
      ]);
      expect(postResponse.status(), "Reply to the reviewer is accepted (201)").toBe(201);
    });

    test("A reviewer parked in waiting does not block manager operations (shelving)", async ({
      page,
    }) => {
      expect(state.reviewerId).toBeTruthy();

      // The reply above woke the reviewer run — wait until its turn ends and
      // the run parks in "waiting" again (the reviewer thread drives the run).
      await expect
        .poll(
          async () => {
            const s = await getRunState(page, state.projectId, state.epicId);
            return `${s.status}:${s.thread_id ?? ""}`;
          },
          { timeout: 60_000, intervals: [500, 1000, 1000] },
        )
        .toBe(`waiting:${state.reviewerId}`);

      // Shelving semantics: a live run parked in "waiting" does not hold
      // the epic's run slot. A manager operation that used to 409 on
      // "run active" — creating a new trial — must now succeed (the parked
      // reviewer run is shelved).
      const createRes = await page.request.post(
        `/api/projects/${state.projectId}/epics/${state.epicId}/threads`,
        {
          data: {
            role: "manager",
            title: "Follow-up trial",
            archive_active: true,
            same_branch: false,
          },
        },
      );
      expect(
        createRes.status(),
        "New-trial creation succeeds while the reviewer run is parked in waiting",
      ).toBe(201);
    });
  });

/**
 * Reviewer stays available on a completed epic — regression guard for the case
 * where the Manager finishes and the user completes the epic (the 1-bit
 * "finish" action) before asking the Reviewer to check. The "Ask Reviewer"
 * button must remain on a completed epic (the reviewer is read-only, so
 * inspecting finished work never requires reopening), and POST /review must
 * accept a completed epic without moving it off completed.
 */
test.describe
  .serial("reviewer available on a completed epic", () => {
    const state = {
      projectId: "",
      epicId: "",
      managerThreadId: "",
    };

    test("setup: run until work done, then complete → completed", async ({ page }) => {
      await page.goto("/projects");
      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.getByTestId("project-name-input").fill("reviewer-completed-project");
      await page.getByTestId("repo-path-input-0").fill(SEED.repoDirs.reviewer);
      await page.getByTestId("form-dialog-submit").click();

      const row = page
        .locator('[data-testid^="project-row-"]')
        .filter({ hasText: "reviewer-completed-project" });
      await expect(row).toBeVisible({ timeout: 15_000 });
      state.projectId = (await row.getAttribute("data-testid"))?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}`);
      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.getByTestId("epic-title-input").fill("Completed reviewer epic");
      await page.getByTestId("epic-description-input").fill("Create hello.py and util.py.");
      await page.getByTestId("epic-ac-input").fill("hello.py exists and prints 'hello'");
      await page.getByTestId("form-dialog-submit").click();

      await page.waitForURL(/\/projects\/[^/]+\/epics\/[^/]+/, { timeout: 15_000 });
      state.epicId = page.url().match(/\/epics\/([^/?#]+)/)?.[1] ?? "";
      expect(state.epicId).toBeTruthy();

      await page.getByTestId("start-run-btn").click();
      await waitForWorkDone(page, state.projectId, state.epicId);

      const eRes = await page.request.get(`/api/projects/${state.projectId}/epics/${state.epicId}`);
      const epic: { active_thread_id?: string | null } = await eRes.json();
      state.managerThreadId = epic.active_thread_id ?? "manager";
      expect(state.managerThreadId).toBeTruthy();

      // Complete from the UI (open → completed, the user's single "finish"
      // action) — no turn is executing (the run is parked in waiting, which
      // is shelved), so the guard allows it.
      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicId}/threads/${state.managerThreadId}`,
      );
      await page.getByTestId("complete-epic-btn").click();
      await expect
        .poll(
          async () => {
            const res = await page.request.get(
              `/api/projects/${state.projectId}/epics/${state.epicId}`,
            );
            return (await res.json()).status;
          },
          { timeout: 15_000, intervals: [500, 1000] },
        )
        .toBe("completed");
    });

    test("Ask Reviewer is available on a completed epic and starts a review", async ({ page }) => {
      expect(state.managerThreadId).toBeTruthy();
      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicId}/threads/${state.managerThreadId}`,
      );

      // The reviewer button remains available on a completed epic (read-only
      // inspection never requires reopening).
      await expect(
        page.getByTestId("start-review-btn"),
        "Ask Reviewer is shown on a completed epic",
      ).toBeVisible({ timeout: 15_000 });

      const [reviewResponse] = await Promise.all([
        page.waitForResponse(
          (resp) => resp.url().endsWith("/review") && resp.request().method() === "POST",
          { timeout: 20_000 },
        ),
        page.getByTestId("start-review-btn").first().click(),
      ]);
      expect(reviewResponse.status(), "POST /review is accepted for a completed epic").toBe(201);
      const reviewer = await reviewResponse.json();
      expect(reviewer.role, "The new thread is a reviewer conversation").toBe("reviewer");

      await page.waitForURL(new RegExp(`/threads/${reviewer.id}`), { timeout: 15_000 });

      // Starting a review must not move the epic off completed (reviewer is read-only).
      const eRes = await page.request.get(`/api/projects/${state.projectId}/epics/${state.epicId}`);
      expect((await eRes.json()).status, "Reviewer leaves the completed epic completed").toBe(
        "completed",
      );
    });
  });
