/**
 * lifecycle-buttons E2E test.
 *
 * Scenario A — Manager effort persistence:
 *   Create epic with xhigh → create thread via API → effort control shows xhigh on thread page
 *   → change to max → page.reload() → max persists
 *   → verify manager_effort=max via GET /epics/{id}
 *
 * Scenario B — Epic complete / reopen (1-bit user-owned lifecycle):
 *   Complete an open epic with the complete button
 *   → verify status=completed via GET /epics/{id}
 *   → reopen it with the reopen button → verify status=open
 *
 * No run is needed (fake script is noop).
 *
 * Note: ManagerEffortControl lives inside the "{thread && ...}" block in ThreadChatInner,
 * so it is not rendered unless an actual thread object exists.
 * Tests pre-create a thread via the API before navigating.
 */

import { expect, test } from "@playwright/test";
import { LIFECYCLE_SEED } from "./lifecycle-buttons-seed";

test.describe
  .serial("lifecycle-buttons", () => {
    const state = {
      projectId: "",
      epicA_Id: "",
      threadA_Id: "",
      epicB_Id: "",
    };

    // ---- Prerequisite: create project ----

    test("0. create project", async ({ page }) => {
      await page.goto("/projects");

      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("project-name-input").fill("lifecycle-project");
      await page.getByTestId("repo-path-input-0").fill(LIFECYCLE_SEED.repoDir);
      await page.getByTestId("form-dialog-submit").click();

      const row = page.locator('[data-testid^="project-row-"]').first();
      await expect(row).toBeVisible({ timeout: 15_000 });

      const testId = await row.getAttribute("data-testid");
      state.projectId = testId?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();
    });

    // ---- Scenario A: effort persistence ----

    test("A-1. create epic with effort=xhigh and create thread via API", async ({ page }) => {
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}`);

      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("epic-title-input").fill("effort-test epic");
      await page.getByTestId("epic-description-input").fill("Testing effort persistence.");
      await page.getByTestId("epic-ac-input").fill("effort persists after reload");

      // Select xhigh (changing from the default high)
      await page.getByTestId("new-epic-effort-xhigh").click();
      await expect(page.getByTestId("new-epic-effort-xhigh")).toHaveAttribute(
        "aria-pressed",
        "true",
      );

      await page.getByTestId("form-dialog-submit").click();

      // Redirected to thread page after epic creation
      await page.waitForURL(/\/epics\//, { timeout: 15_000 });
      const url = page.url();
      const epicMatch = url.match(/\/epics\/([^/]+)/);
      state.epicA_Id = epicMatch?.[1] ?? "";
      expect(state.epicA_Id).toBeTruthy();

      // Verify via API that the epic's manager_effort is xhigh
      const epicRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicA_Id}`,
      );
      expect(epicRes.ok()).toBeTruthy();
      const epic = await epicRes.json();
      expect(epic.manager_effort).toBe("xhigh");

      // ManagerEffortControl is only shown when a thread exists.
      // Create a thread via the API and save its ID.
      const threadRes = await page.request.post(
        `/api/projects/${state.projectId}/epics/${state.epicA_Id}/threads`,
        { data: { role: "manager", title: "effort-test thread" } },
      );
      expect(threadRes.ok()).toBeTruthy();
      const thread = await threadRes.json();
      state.threadA_Id = thread.id;
      expect(state.threadA_Id).toBeTruthy();
    });

    test("A-2. effort control shows xhigh on thread page", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicA_Id).toBeTruthy();
      expect(state.threadA_Id).toBeTruthy();

      // Navigate using the actual thread ID (ensures the thread object exists)
      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicA_Id}/threads/${state.threadA_Id}`,
      );

      // Wait for ManagerEffortControl to appear
      await expect(page.getByTestId("effort-btn-xhigh")).toBeVisible({ timeout: 15_000 });

      // xhigh should be selected
      await expect(page.getByTestId("effort-btn-xhigh")).toHaveAttribute("aria-pressed", "true");
      await expect(page.getByTestId("effort-btn-high")).toHaveAttribute("aria-pressed", "false");
      await expect(page.getByTestId("effort-btn-max")).toHaveAttribute("aria-pressed", "false");
    });

    test("A-3. change effort to max and verify persistence", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicA_Id).toBeTruthy();
      expect(state.threadA_Id).toBeTruthy();

      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicA_Id}/threads/${state.threadA_Id}`,
      );
      await expect(page.getByTestId("effort-btn-max")).toBeVisible({ timeout: 15_000 });

      // Change to max
      await page.getByTestId("effort-btn-max").click();

      // Wait until max becomes selected
      await expect(page.getByTestId("effort-btn-max")).toHaveAttribute("aria-pressed", "true", {
        timeout: 10_000,
      });

      // Also verify via API
      const res = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicA_Id}`,
      );
      expect(res.ok()).toBeTruthy();
      const epic = await res.json();
      expect(epic.manager_effort).toBe("max");
    });

    test("A-4. reload and effort=max persists", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicA_Id).toBeTruthy();
      expect(state.threadA_Id).toBeTruthy();

      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicA_Id}/threads/${state.threadA_Id}`,
      );
      await expect(page.getByTestId("effort-btn-max")).toBeVisible({ timeout: 15_000 });

      // max should remain selected after reload
      await page.reload();
      await expect(page.getByTestId("effort-btn-max")).toBeVisible({ timeout: 15_000 });
      await expect(page.getByTestId("effort-btn-max")).toHaveAttribute("aria-pressed", "true");
    });

    // ---- Scenario B: Epic complete / reopen (1-bit user-owned lifecycle) ----

    test("B-1. create epic for complete test (status=open)", async ({ page }) => {
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}`);

      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("epic-title-input").fill("complete-test epic");
      await page.getByTestId("epic-description-input").fill("Testing epic complete/reopen.");
      await page.getByTestId("epic-ac-input").fill("epic can be completed and reopened");

      await page.getByTestId("form-dialog-submit").click();

      await page.waitForURL(/\/epics\//, { timeout: 15_000 });
      const url = page.url();
      const epicMatch = url.match(/\/epics\/([^/]+)/);
      state.epicB_Id = epicMatch?.[1] ?? "";
      expect(state.epicB_Id).toBeTruthy();

      // Verify via API that a fresh epic starts open
      const res = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicB_Id}`,
      );
      expect(res.ok()).toBeTruthy();
      const epic = await res.json();
      expect(epic.status).toBe("open");
    });

    test("B-2. complete epic and verify status=completed via API", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicB_Id).toBeTruthy();

      // Navigate to the epic page (thread page) — the complete button is in
      // RunControlsBar (desktop sidebar), shown on any thread page URL.
      await page.goto(`/projects/${state.projectId}/epics/${state.epicB_Id}/threads/manager`);

      // Complete is inline in the desktop sidebar; it renders once the controls
      // settle into an idle branch (readiness wait).
      const completeBtn = page.getByTestId("complete-epic-btn");
      await expect(completeBtn).toBeVisible({ timeout: 15_000 });
      await completeBtn.click();

      // Verify status=completed via API (no navigation — the page stays put and
      // the controls flip to the read-only completed branch).
      await expect
        .poll(
          async () => {
            const res = await page.request.get(
              `/api/projects/${state.projectId}/epics/${state.epicB_Id}`,
            );
            if (!res.ok()) return null;
            const epic = await res.json();
            return epic.status;
          },
          { timeout: 10_000, intervals: [500, 1000] },
        )
        .toBe("completed");

      // Completed epic is read-only: the controls now offer Reopen.
      await expect(page.getByTestId("reopen-btn")).toBeVisible({ timeout: 15_000 });
    });

    test("B-3. reopen epic and verify status=open via API", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicB_Id).toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicB_Id}/threads/manager`);

      const reopenBtn = page.getByTestId("reopen-btn");
      await expect(reopenBtn).toBeVisible({ timeout: 15_000 });
      await reopenBtn.click();

      // Reopening flips the bit back to open (PATCH {status:"open"}).
      await expect
        .poll(
          async () => {
            const res = await page.request.get(
              `/api/projects/${state.projectId}/epics/${state.epicB_Id}`,
            );
            if (!res.ok()) return null;
            const epic = await res.json();
            return epic.status;
          },
          { timeout: 10_000, intervals: [500, 1000] },
        )
        .toBe("open");

      // Open epic shows the full control set again (start run is back).
      await expect(page.getByTestId("start-run-btn")).toBeVisible({ timeout: 15_000 });
    });
  });
