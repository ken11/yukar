/**
 * Notifications E2E test — SSE unread badge through the run lifecycle → navigation
 *
 * Purpose:
 *   Run a fake run to completion and verify in a real browser that the notification
 *   unread badge increments when the run_completed SSE event is received.
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
 *   3. Poll the API until the run reaches completed
 *      → confirm the notification badge appears
 *      → open the popover and verify the notification entry
 *      → click the notification to navigate to /epics/{id}/tasks
 */

import { expect, test } from "@playwright/test";
import { NOTIF_SEED } from "./notifications-seed";

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

    test("3. run completes → badge → popover → navigate", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      // Navigate to the epic page (the page that has the start-run button)
      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}`);

      const startBtn = page.getByTestId("start-run-btn");
      await expect(startBtn).toBeVisible({ timeout: 10_000 });
      await startBtn.click();

      // The UI navigates to the thread page, but we move to the project page to receive SSE notifications
      // (NotificationsPopover is part of the project-header)
      await page.goto(`/projects/${state.projectId}`);

      // Poll the API until the run reaches completed (up to 90 seconds)
      await expect
        .poll(
          async () => {
            const res = await page.request.get(
              `/api/projects/${state.projectId}/epics/${state.epicId}`,
            );
            if (!res.ok()) return null;
            return (await res.json()).status;
          },
          { timeout: 90_000, intervals: [500, 1000, 2000] },
        )
        .toBe("in_review");

      // Wait for the run_completed SSE event to arrive and update the badge
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
