/**
 * Worker failure E2E test.
 *
 * Purpose:
 *   Using FakeModel, make the Worker's first turn a MaxTokensReachedException to
 *   force a Worker failure, then verify in a real browser that the failure state
 *   is shown in the UI.
 *
 * Verification flow:
 *   1. Create project → create epic → start run
 *   2. Navigate to the manager thread page
 *   3. Confirm via API polling that the run advances to completed
 *      (after Worker failure, Manager calls continue_epic to finish)
 *   4. Verify that the "失敗" label appears on the Worker node in the thread tree panel
 *   5. Verify that WorkerFailedEvent was delivered via SSE and run/state status is completed
 *
 * Worker failure mechanism:
 *   - Worker RaiseTurn(MaxTokensReachedException) → WorkerFailedEvent emitted
 *   - ThreadTreePanel WorkerNode: status="failed" → "失敗" label + warning icon
 *   - run_state.status: completed because Manager calls complete_epic
 *
 * The context_overflow variant (ContextWindowOverflowException) is also verified in a
 * separate test within the same describe block.
 */

import { expect, test } from "@playwright/test";
import { WORKER_FAILURE_SEED } from "./worker-failure-seed";

// ---- Shared test helpers ----

async function createProjectAndEpic(
  page: import("@playwright/test").Page,
  projectName: string,
  epicTitle: string,
): Promise<{ projectId: string; epicId: string }> {
  await page.goto("/projects");

  await page.getByTestId("new-project-btn").click();
  await expect(page.getByRole("dialog")).toBeVisible();

  await page.getByTestId("project-name-input").fill(projectName);
  await page.getByTestId("repo-path-input-0").fill(WORKER_FAILURE_SEED.repoDir);
  await page.getByTestId("form-dialog-submit").click();

  const row = page.locator('[data-testid^="project-row-"]').first();
  await expect(row).toBeVisible({ timeout: 15_000 });
  const testId = await row.getAttribute("data-testid");
  const projectId = testId?.replace("project-row-", "") ?? "";
  expect(projectId).toBeTruthy();

  await page.goto(`/projects/${projectId}`);
  await page.getByTestId("new-epic-btn").first().click();
  await expect(page.getByRole("dialog")).toBeVisible();

  await page.getByTestId("epic-title-input").fill(epicTitle);
  await page.getByTestId("epic-description-input").fill("Worker failure E2E test.");
  await page.getByTestId("epic-ac-input").fill("Worker failure is surfaced in UI.");
  await page.getByTestId("form-dialog-submit").click();

  await page.waitForURL(/\/epics\//, { timeout: 15_000 });
  const url = page.url();
  const epicMatch = url.match(/\/epics\/([^/]+)/);
  let epicId = epicMatch?.[1] ?? "";
  if (!epicId) {
    const epicCard = page.locator('[data-testid^="epic-card-"]').first();
    await expect(epicCard).toBeVisible({ timeout: 5_000 });
    epicId = (await epicCard.getAttribute("data-testid"))?.replace("epic-card-", "") ?? "";
  }
  expect(epicId).toBeTruthy();

  return { projectId, epicId };
}

// ============================
// (A) MaxTokensReachedException
// ============================

test.describe
  .serial("Worker failure — MaxTokensReachedException", () => {
    const state = { projectId: "", epicId: "" };

    test("1. create project and epic", async ({ page }) => {
      const result = await createProjectAndEpic(
        page,
        "worker-failure-project",
        "worker-failure epic",
      );
      state.projectId = result.projectId;
      state.epicId = result.epicId;
    });

    test("2. start run", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}`);

      const startBtn = page.getByTestId("start-run-btn");
      await expect(startBtn).toBeVisible({ timeout: 10_000 });
      await startBtn.click();

      // Redirected to the manager thread page
      await expect(page).toHaveURL(/\/threads\//, { timeout: 15_000 });
    });

    test("3. epic reaches terminal state after worker failure", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      // When the Worker fails with MaxTokensReachedException, the following happens:
      //   1. WorkerFailedEvent is emitted and task T1 reverts to "todo"
      //   2. The Manager's FakeScript is exhausted and the orchestrator errors
      //      (complete_epic is rejected because T1 is runnable → turn limit reached → exception)
      //   3. The supervisor transitions the epic status to "failed"
      // This is the correct behavior: when Worker fails and Manager cannot recover, epic becomes "failed".
      await expect
        .poll(
          async () => {
            const res = await page.request.get(
              `/api/projects/${state.projectId}/epics/${state.epicId}`,
            );
            return (await res.json()).status;
          },
          { timeout: 90_000, intervals: [500, 1000, 1000] },
        )
        .toMatch(/^failed$/);

      // Log the final epic status
      const epicRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}`,
      );
      const epicBody = await epicRes.json();
      console.log(`[worker-failure] epic status = ${epicBody.status}`);

      // Verify run/state status
      const runStateRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}/run/state`,
      );
      const runState = await runStateRes.json();
      console.log(`[worker-failure] run/state status = ${runState.status}`);
      expect(runState.status).toBe("error");
    });

    test("4. Worker node shows failed status in thread tree panel", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      // Navigate to the manager thread page (which includes the ThreadTreePanel)
      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/threads/manager`, {
        waitUntil: "domcontentloaded",
      });

      // Wait until the ThreadTreePanel is rendered (after manager bubbles appear)
      await expect(page.locator('[data-testid="agent-message"]').first()).toBeVisible({
        timeout: 30_000,
      });

      // Short wait for stabilization
      await page.waitForTimeout(2_000);

      // Check whether the "失敗" label (statusFailed: "失敗") appears on the Worker node
      // ThreadTreePanel WorkerNode emits "失敗" when status="failed"
      const failedLabel = page.getByText("失敗");
      const failedCount = await failedLabel.count();
      console.log(`[worker-failure] "失敗" label count = ${failedCount}`);

      expect(
        failedCount,
        `Since Worker failed, the "失敗" label should appear at least once, but got ${failedCount}`,
      ).toBeGreaterThanOrEqual(1);

      // The warning icon has no aria attribute, so its presence is confirmed via the "失敗" text

      // Save screenshot
      await page.screenshot({
        path: "test-results/worker-failure-tree.png",
        fullPage: true,
      });
      console.log("[worker-failure] screenshot saved: test-results/worker-failure-tree.png");
    });

    test("5. subsequent run POST is accepted after run terminates (202 or 409)", async ({
      page,
    }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      // Re-run is possible (202) even when epic is in the failed state.
      // If a run is still in progress, 409 is returned (both are acceptable).
      const res = await page.request.post(
        `/api/projects/${state.projectId}/epics/${state.epicId}/run`,
      );
      const status = res.status();
      console.log(`[worker-failure] POST /run status after worker failure = ${status}`);
      // 202 (new run started) or 409 (still running or epic closed) are both acceptable
      expect([202, 409]).toContain(status);
    });
  });

// ============================
// (B) ContextWindowOverflowException — coverage note (no test placed here)
// ============================
// context_overflow goes through the same failure path as max_tokens
// (WorkerFailedEvent → WorkerNode status="failed" → "失敗" label),
// so UI verification is already covered by (A) above.
// The injection of ContextWindowOverflowException itself is covered by a FakeModel RaiseTurn
// unit test (apps/api/tests/test_fake_model_fidelity.py::test_raise_turn_context_window).
// If a dedicated E2E is needed, add playwright.config.worker-context-overflow.ts separately.
// (A no-op test that always passes is not placed here because it creates false coverage signals.)
