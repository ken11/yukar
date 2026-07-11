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
 *      (role=reviewer) and navigates to it; the composer is shown (repliable)
 *   3. The reviewer runs read-only: it inspects read_branch_diff, reports in
 *      plain BODY TEXT and parks at "waiting", WITHOUT changing epic.status or
 *      epic.active_thread_id (the manager trial stays the active trial)
 *   4. The reviewer thread is listed in the sidebar; the manager thread keeps
 *      its composer (a reviewer run must not hijack the active-trial pointer)
 *   5. The user can reply to the reviewer (post_message routes in reviewer mode)
 *   6. A reviewer parked in waiting does NOT block manager operations
 *      (new-trial creation succeeds — shelving semantics)
 */

import { expect, test } from "@playwright/test";
import { SEED } from "./seed";
import { getRunState, waitForWorkDone } from "./wait-helpers";

const SHOTS = "playwright-report";

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
            return `${s.status}:${s.manager_thread ?? ""}`;
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

      // The report body is visible in the conversation UI.
      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicId}/threads/${state.reviewerId}`,
      );
      await expect(
        page.getByTestId("agent-message").filter({ hasText: "Reviewed the branch" }).first(),
        "The reviewer's report text renders as an agent bubble",
      ).toBeVisible({ timeout: 20_000 });

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
      // overwriting RunState.manager_thread).
      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicId}/threads/${state.managerThreadId}`,
      );
      await expect(
        page.getByTestId("thread-composer"),
        "Manager thread keeps its composer after a reviewer run",
      ).toBeVisible({ timeout: 20_000 });
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
            return `${s.status}:${s.manager_thread ?? ""}`;
          },
          { timeout: 60_000, intervals: [500, 1000, 1000] },
        )
        .toBe(`waiting:${state.reviewerId}`);

      // P3 shelving semantics: a live run parked in "waiting" does not hold
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
