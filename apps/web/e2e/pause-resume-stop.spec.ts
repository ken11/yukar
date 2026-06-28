/**
 * pause/resume/stop E2E test.
 *
 * Purpose:
 *   Verify Run pause, resume, and stop through the real UI.
 *   YUKAR_FAKE_SLEEP=6.0 + task_update(T1 todo) + 29 text turns keep the run
 *   in the "running" state for 540s or more, providing a window for pause/resume/stop.
 *
 *   Key point 1: the first manager-script turn calls task_update to register T1 as
 *   "todo". This bypasses the backend deadlock guard (which would break immediately
 *   when runnable/in_flight are both zero), so subsequent text turns are consumed
 *   continuously (dispatch/complete_epic are not called, so the run keeps advancing
 *   through manager turns).
 *
 *   Key point 2: FAKE_SLEEP=6.0 > the supervisor's 5s timeout.
 *   When stopping from the running state, the manager is mid asyncio.sleep(6.0) and
 *   the supervisor cancels the asyncio.Task after 5s → CancelledError →
 *   the orchestrator sets state.status = "idle".
 *   (Stopping from the paused state causes the manager to return cleanly → "completed",
 *   so test 6 stops directly from the running state.)
 *
 * Verification flow (serial):
 *   1. Create project
 *   2. Create epic
 *   3. Start Run → poll until status becomes "running"
 *      Assert button visibility for running state (pause-run-btn / stop-run-btn visible,
 *      start-run-btn hidden)
 *   4. Click pause button (data-testid="pause-run-btn")
 *      → poll until status becomes "paused"
 *      → resume-run-btn / stop-run-btn visible, pause-run-btn / start-run-btn hidden
 *   5. Click resume button (data-testid="resume-run-btn")
 *      → poll until status returns to "running"
 *      → pause-run-btn / stop-run-btn visible, resume-run-btn hidden
 *   6. Click stop button (stop-run-btn) while still in running state
 *      → click stop-confirm-btn in StopConfirmDialog
 *      → supervisor cancels task after 5s → CancelledError → status = "idle"
 *      → poll until status becomes "idle" (up to 30s: 5s supervisor + 25s buffer)
 *      → start-run-btn visible
 *   7. Save screenshot (test-results/pause-resume-stop.png)
 *
 * All state waits use expect.poll to monitor GET /api/projects/{p}/epics/{e}/run/state.
 * No fixed sleeps or state assumptions.
 */

import fs from "node:fs";
import { expect, test } from "@playwright/test";
import { PAUSE_RESUME_SEED } from "./pause-resume-stop-seed";

// RunState.status enum (apps/api/src/yukar/models/run.py)
// "idle" | "running" | "paused" | "awaiting_input" | "error" | "completed" | "interrupted"
type RunStatus =
  | "idle"
  | "running"
  | "paused"
  | "awaiting_input"
  | "error"
  | "completed"
  | "interrupted";

test.describe
  .serial("pause / resume / stop", () => {
    const state = {
      projectId: "",
      epicId: "",
    };

    // ---- helper: GET /api/projects/{p}/epics/{e}/run/state ----
    async function getRunStatus(
      page: import("@playwright/test").Page,
      projectId: string,
      epicId: string,
    ): Promise<RunStatus> {
      const res = await page.request.get(`/api/projects/${projectId}/epics/${epicId}/run/state`);
      const body = await res.json();
      return body.status as RunStatus;
    }

    // ---- 1. Create project ----

    test("1. create project", async ({ page }) => {
      await page.goto("/projects");

      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("project-name-input").fill("pause-resume-project");
      await page.getByTestId("repo-path-input-0").fill(PAUSE_RESUME_SEED.repoDir);

      await page.getByTestId("form-dialog-submit").click();

      const row = page.locator('[data-testid^="project-row-"]').first();
      await expect(row).toBeVisible({ timeout: 15_000 });

      const testId = await row.getAttribute("data-testid");
      state.projectId = testId?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();
    });

    // ---- 2. Create epic ----

    test("2. create epic", async ({ page }) => {
      expect(state.projectId, "projectId from test 1").toBeTruthy();

      await page.goto(`/projects/${state.projectId}`);

      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("epic-title-input").fill("pause resume stop epic");
      await page
        .getByTestId("epic-description-input")
        .fill("Test run pause, resume, and stop lifecycle.");
      await page.getByTestId("epic-ac-input").fill("Run can be paused, resumed, and stopped.");

      await page.getByTestId("form-dialog-submit").click();

      await page.waitForURL(/\/epics\//, { timeout: 15_000 });
      const url = page.url();
      const epicMatch = url.match(/\/epics\/([^/]+)/);
      if (epicMatch) {
        state.epicId = epicMatch[1];
      } else {
        const epicCard = page.locator('[data-testid^="epic-card-"]').first();
        await expect(epicCard).toBeVisible({ timeout: 5_000 });
        state.epicId =
          (await epicCard.getAttribute("data-testid"))?.replace("epic-card-", "") ?? "";
      }
      expect(state.epicId).toBeTruthy();
    });

    // ---- 3. Start Run → confirm running ----

    test("3. start run and wait for running", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}`);

      const startBtn = page.getByTestId("start-run-btn");
      await expect(startBtn).toBeVisible({ timeout: 10_000 });
      await startBtn.click();

      // Navigate to manager thread page
      await expect(page).toHaveURL(/\/threads\//, { timeout: 15_000 });

      // Poll until status becomes "running"
      await expect
        .poll(() => getRunStatus(page, state.projectId, state.epicId), {
          message: "Run status should become 'running'",
          timeout: 30_000,
          intervals: [300, 500, 500, 1000],
        })
        .toBe("running");

      // Button visibility for running state:
      //   pause-run-btn (pause) and stop-run-btn (stop) are visible
      //   start-run-btn (start) is hidden
      await expect(page.getByTestId("pause-run-btn")).toBeVisible({ timeout: 10_000 });
      await expect(page.getByTestId("stop-run-btn")).toBeVisible({ timeout: 5_000 });
      await expect(page.getByTestId("start-run-btn")).not.toBeVisible();
    });

    // ---- 4. Pause → confirm paused state ----

    test("4. pause run and verify paused state", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      // Navigate to epic page (EpicScopeHeader is present on all sub-pages)
      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}`);

      // Confirm still running (should carry over from the previous test, but poll to be safe)
      await expect
        .poll(() => getRunStatus(page, state.projectId, state.epicId), {
          message: "Run should be running before pausing",
          timeout: 20_000,
          intervals: [300, 500, 500, 1000],
        })
        .toBe("running");

      // Click the pause button (data-testid="pause-run-btn")
      const pauseBtn = page.getByTestId("pause-run-btn");
      await expect(pauseBtn).toBeVisible({ timeout: 10_000 });
      await pauseBtn.click();

      // Poll until status becomes "paused"
      await expect
        .poll(() => getRunStatus(page, state.projectId, state.epicId), {
          message: "Run status should become 'paused' after clicking pause",
          timeout: 30_000,
          intervals: [300, 500, 1000, 1000],
        })
        .toBe("paused");

      // Button visibility for paused state:
      //   resume-run-btn (resume) and stop-run-btn (stop) are visible
      //   pause-run-btn (pause) and start-run-btn (start) are hidden
      await expect(page.getByTestId("resume-run-btn")).toBeVisible({ timeout: 10_000 });
      await expect(page.getByTestId("stop-run-btn")).toBeVisible({ timeout: 5_000 });
      await expect(page.getByTestId("pause-run-btn")).not.toBeVisible();
      await expect(page.getByTestId("start-run-btn")).not.toBeVisible();
    });

    // ---- 5. Resume → confirm return to running ----

    test("5. resume run and verify running state", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}`);

      // Confirm currently paused
      await expect
        .poll(() => getRunStatus(page, state.projectId, state.epicId), {
          message: "Run should be paused before resuming",
          timeout: 20_000,
          intervals: [300, 500, 500, 1000],
        })
        .toBe("paused");

      // Click the resume button (data-testid="resume-run-btn")
      const resumeBtn = page.getByTestId("resume-run-btn");
      await expect(resumeBtn).toBeVisible({ timeout: 10_000 });
      await resumeBtn.click();

      // Poll until status returns to "running"
      await expect
        .poll(() => getRunStatus(page, state.projectId, state.epicId), {
          message: "Run status should return to 'running' after resuming",
          timeout: 30_000,
          intervals: [300, 500, 1000, 1000],
        })
        .toBe("running");

      // Button visibility for running state:
      //   pause-run-btn and stop-run-btn are visible
      //   resume-run-btn is hidden
      await expect(page.getByTestId("pause-run-btn")).toBeVisible({ timeout: 10_000 });
      await expect(page.getByTestId("stop-run-btn")).toBeVisible({ timeout: 5_000 });
      await expect(page.getByTestId("resume-run-btn")).not.toBeVisible();
    });

    // ---- 6. Stop → confirm idle ----
    //
    // Reaching "idle" requires the CancelledError path.
    //
    // Stopping from running state:
    //   supervisor.stop() → runner.stop() (_stopped=True, _paused.set())
    //   → wait 5s → timeout while mid asyncio.sleep(6.0)
    //   → handle.task.cancel() → CancelledError
    //   → orchestrator: _stopped=True → state.status = "idle"
    //
    // Stopping from paused state:
    //   _paused.set() unblocks → sees _stopped=True and breaks
    //   → _run_loop returns normally → state.status = "completed" (not idle)
    //
    // Therefore test 6 stops directly from the running state.

    test("6. stop run and verify idle state", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}`);

      // Confirm currently running (should have returned to running after resume)
      await expect
        .poll(() => getRunStatus(page, state.projectId, state.epicId), {
          message: "Run should be running before stopping",
          timeout: 20_000,
          intervals: [300, 500, 500, 1000],
        })
        .toBe("running");

      // Stop directly from the running state.
      // (Pausing first then stopping would result in "completed", so we skip the pause here.)
      const stopBtn = page.getByTestId("stop-run-btn");
      await expect(stopBtn).toBeVisible({ timeout: 10_000 });
      await stopBtn.click();

      // Wait for StopConfirmDialog to appear
      const confirmBtn = page.getByTestId("stop-confirm-btn");
      await expect(confirmBtn).toBeVisible({ timeout: 10_000 });
      await confirmBtn.click();

      // Supervisor cancels the asyncio.Task after 5s → CancelledError → "idle".
      // With YUKAR_FAKE_SLEEP=6.0 the manager is mid asyncio.sleep(6.0).
      // Timeout: 5s (supervisor wait) + 25s (buffer) = 30s
      await expect
        .poll(() => getRunStatus(page, state.projectId, state.epicId), {
          message: "Run status should become 'idle' after stopping (via CancelledError)",
          timeout: 30_000,
          intervals: [500, 1000, 1000, 2000],
        })
        .toBe("idle");

      // Button visibility for idle state: start-run-btn is visible
      await expect(page.getByTestId("start-run-btn")).toBeVisible({ timeout: 10_000 });
      // pause/resume/stop buttons are hidden
      await expect(page.getByTestId("pause-run-btn")).not.toBeVisible();
      await expect(page.getByTestId("resume-run-btn")).not.toBeVisible();
      await expect(page.getByTestId("stop-run-btn")).not.toBeVisible();
    });

    // ---- 7. Screenshot ----

    test("7. screenshot after stop", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}`);

      // Wait until idle is confirmed (just to be safe)
      await expect
        .poll(() => getRunStatus(page, state.projectId, state.epicId), {
          message: "Run status should be 'idle' for screenshot",
          timeout: 15_000,
          intervals: [500, 1000],
        })
        .toBe("idle");

      // Take screenshot in idle state with the start button visible
      await expect(page.getByTestId("start-run-btn")).toBeVisible({ timeout: 10_000 });

      fs.mkdirSync("test-results", { recursive: true });
      await page.screenshot({
        path: "test-results/pause-resume-stop.png",
        fullPage: true,
      });

      console.log("[pause-resume-stop] Screenshot saved: test-results/pause-resume-stop.png");
    });
  });
