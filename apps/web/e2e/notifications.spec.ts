/**
 * Notifications E2E test — SSE unread badge through the run lifecycle → navigation
 *
 * Purpose:
 *   Run a fake run and verify in a real browser that the notification unread
 *   badge increments from run-lifecycle SSE events. Under P3 a conversation
 *   run never emits run_completed (it parks in "waiting"), so the badge source
 *   here is run_started; a richer "your turn" inbox is P4 territory.
 *   Then open the popover, click a notification entry, and confirm navigation
 *   to the epics/{id}/tasks page.
 *
 * Design note:
 *   Notification state is managed in-memory (useProjectNotifications) and resets
 *   on page navigation or reload.
 *   Badge check, popover check, and click-navigation must all run within the same
 *   test (page) instance in sequence.
 *
 * Verification flow:
 *   1. Create project → create epic
 *   2. Start a run while staying on the project page
 *   3. Poll the API until the work is done (run "waiting" + all tasks done)
 *      → confirm the notification badge appears
 *      → open the popover and verify the notification entry
 *      → click the notification to navigate to /epics/{id}/tasks
 */

import { expect, test } from "@playwright/test";
import { NOTIF_SEED } from "./notifications-seed";
import { waitForWorkDone } from "./wait-helpers";

test.describe
  .serial("Notifications — run lifecycle SSE unread badge", () => {
    const state = {
      projectId: "",
      epicId: "",
    };

    // ---- 1. Create project ----

    test("1. create project", async ({ page }) => {
      await page.goto("/projects");

      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("project-name-input").fill("notif-project");
      await page.getByTestId("repo-path-input-0").fill(NOTIF_SEED.repoDir);
      await page.getByTestId("form-dialog-submit").click();

      const row = page.locator('[data-testid^="project-row-"]').first();
      await expect(row).toBeVisible({ timeout: 15_000 });

      const testId = await row.getAttribute("data-testid");
      state.projectId = testId?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();
    });

    // ---- 2. Create epic ----

    test("2. create epic", async ({ page }) => {
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}`);

      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("epic-title-input").fill("notif-test epic");
      await page.getByTestId("epic-description-input").fill("Testing notifications.");
      await page.getByTestId("epic-ac-input").fill("Notifications appear after run.");
      await page.getByTestId("form-dialog-submit").click();

      await page.waitForURL(/\/epics\//, { timeout: 15_000 });
      const url = page.url();
      const epicMatch = url.match(/\/epics\/([^/]+)/);
      state.epicId = epicMatch?.[1] ?? "";
      expect(state.epicId).toBeTruthy();
    });

    // ---- 3. Start run → notification badge → popover → click navigation (all in the same page) ----

    test("3. work done → badge → popover → navigate", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      // Mount the project page FIRST (NotificationsPopover is part of the
      // project-header) so its SSE subscription is live before the run starts:
      // under P3 the only badge source is run_started, which fires immediately
      // on POST /run — a fake run can start and park before a subsequent
      // page.goto could connect, and notification state is in-memory.
      await page.goto(`/projects/${state.projectId}`);
      await expect(page.locator('[data-testid^="epic-card-"]').first()).toBeVisible({
        timeout: 15_000,
      });

      // Start the run via REST from the mounted page (no navigation).
      const runRes = await page.request.post(
        `/api/projects/${state.projectId}/epics/${state.epicId}/run`,
      );
      expect(runRes.ok(), `POST /run should succeed: ${runRes.status()}`).toBeTruthy();

      // Standard work-done wait (up to 90 seconds): the run parks in "waiting"
      // with every task done.
      await waitForWorkDone(page, state.projectId, state.epicId);

      // The run_started SSE event has updated the badge (P3: no run_completed
      // for conversation runs — the "your turn" inbox is P4).
      // The notification button must become visible with "(N unread)" in its aria-label
      const notifBtn = page.getByRole("button", { name: /notifications.*unread/i });
      await expect(notifBtn).toBeVisible({ timeout: 30_000 });

      // The badge count span (not aria-hidden) must be visible
      // <Icon> renders a <span aria-hidden>, so filter with :not([aria-hidden])
      const badge = page.locator("button[aria-label*='unread'] span:not([aria-hidden])");
      await expect(badge).toBeVisible({ timeout: 10_000 });
      const badgeText = await badge.textContent();
      // The badge must show a number >= 1 or "9+"
      expect(Number.parseInt(badgeText ?? "0", 10) >= 1 || badgeText === "9+").toBeTruthy();

      // ---- Open the popover and verify notifications ----
      await notifBtn.click();

      // Verify that the popover content is visible.
      // ("Notifications" also appears as text in the Icon span, so we confirm
      //  the popover is open by waiting for a notification entry containing "の Run が")

      await expect(page.getByText(/の Run が/).first()).toBeVisible({ timeout: 10_000 });

      // ---- Click a notification to navigate to epics/{id}/tasks ----
      // Click the notification entry (li > button inside the popover)
      const notifEntry = page.locator("[data-radix-popper-content-wrapper] li button").first();
      await expect(notifEntry).toBeVisible({ timeout: 10_000 });
      await notifEntry.click();

      // Assert navigation to /epics/{epicId}/tasks
      await expect(page).toHaveURL(/\/epics\/[^/]+\/tasks$/, { timeout: 10_000 });
    });
  });
