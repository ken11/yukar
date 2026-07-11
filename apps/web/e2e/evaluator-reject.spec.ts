/**
 * Evaluator reject → Worker retry → accept E2E test.
 *
 * Purpose:
 *   Using the FakeModel per_call evaluator, verify in a real browser that:
 *   the 1st Evaluator rejects → Manager re-dispatches with feedback →
 *   the 2nd Evaluator accepts → run reaches completed.
 *
 * Verification flow:
 *   1. Create project → create epic → start run
 *   2. Confirm via API polling that the run advances to completed
 *   3. Assert the reject cycle is observable:
 *      - The thread list contains exactly 1 thread with role="evaluator" and status="failed"
 *        (the 1st reject evaluator is recorded as failed)
 *      - The thread list contains exactly 1 thread with role="evaluator" and status="resolved"
 *        (the 2nd accept evaluator is recorded as resolved)
 *   4. Save screenshot (test-results/evaluator-reject.png)
 *
 * Mechanism of the reject cycle:
 *   - dispatch.py: accepted=false → task.status="todo", evaluator thread="failed"
 *   - Manager FakeScript: retries with feedback on the next dispatch turn
 *   - accepted=true → task.status="done", evaluator thread="resolved"
 *   - complete_epic: no runnable tasks → ok:true → epic completed
 */

import { expect, test } from "@playwright/test";
import { EVALUATOR_REJECT_SEED } from "./evaluator-reject-seed";

test.describe
  .serial("Evaluator reject → retry → accept", () => {
    const state = { projectId: "", epicId: "" };

    test("1. create project and epic", async ({ page }) => {
      await page.goto("/projects");

      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("project-name-input").fill("evaluator-reject-project");
      await page.getByTestId("repo-path-input-0").fill(EVALUATOR_REJECT_SEED.repoDir);
      await page.getByTestId("form-dialog-submit").click();

      const row = page.locator('[data-testid^="project-row-"]').first();
      await expect(row).toBeVisible({ timeout: 15_000 });
      const testId = await row.getAttribute("data-testid");
      state.projectId = testId?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}`);
      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("epic-title-input").fill("evaluator-reject epic");
      await page.getByTestId("epic-description-input").fill("Evaluator reject → retry → accept.");
      await page.getByTestId("epic-ac-input").fill("Run completes after reject and retry.");
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
      state.epicId = epicId;
      expect(state.epicId).toBeTruthy();
    });

    test("2. start run", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}`);

      const startBtn = page.getByTestId("start-run-btn");
      await expect(startBtn).toBeVisible({ timeout: 10_000 });
      await startBtn.click();

      // Redirect to the manager thread page
      await expect(page).toHaveURL(/\/threads\//, { timeout: 15_000 });
    });

    test("3. run completes after reject → retry cycle", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      // Poll run/state until the run completes after the
      // Evaluator reject → re-dispatch → accept → complete_epic cycle (max 120 s)
      await expect
        .poll(
          async () => {
            const res = await page.request.get(
              `/api/projects/${state.projectId}/epics/${state.epicId}/run/state`,
            );
            return (await res.json()).status;
          },
          { timeout: 120_000, intervals: [500, 1000, 2000] },
        )
        .toBe("completed");

      // The epic itself stays open — finishing a run never transitions the
      // 1-bit user-owned epic status.
      const epicRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}`,
      );
      expect((await epicRes.json()).status).toBe("open");
    });

    test("4. assert reject cycle via thread list", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      // Fetch the thread list and verify the reject/accept cycle
      const threadsRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}/threads`,
      );
      expect(threadsRes.status()).toBe(200);
      const threads = await threadsRes.json();
      console.log(`[evaluator-reject] threads = ${JSON.stringify(threads, null, 2)}`);

      // Extract evaluator threads
      const evalThreads = (threads as Array<{ role: string; status: string }>).filter(
        (t) => t.role === "evaluator",
      );
      console.log(`[evaluator-reject] evaluator threads count = ${evalThreads.length}`);
      console.log(`[evaluator-reject] evaluator threads = ${JSON.stringify(evalThreads, null, 2)}`);

      // 2 dispatches → 2 evaluator threads
      expect(
        evalThreads.length,
        `Evaluator should be called 2 times (reject + accept) but only ${evalThreads.length} thread(s) found`,
      ).toBe(2);

      // 1st reject: status="failed"
      const failedEvalCount = evalThreads.filter((t) => t.status === "failed").length;
      expect(
        failedEvalCount,
        `There should be 1 rejected evaluator thread (status=failed) but got ${failedEvalCount}`,
      ).toBe(1);

      // 2nd accept: status="resolved"
      const resolvedEvalCount = evalThreads.filter((t) => t.status === "resolved").length;
      expect(
        resolvedEvalCount,
        `There should be 1 accepted evaluator thread (status=resolved) but got ${resolvedEvalCount}`,
      ).toBe(1);

      console.log(
        "[evaluator-reject] reject cycle confirmed: failed=1, resolved=1 — reject → retry → accept completed",
      );
    });

    test("5. screenshot of manager thread", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      // Navigate to the Manager thread page and save a screenshot
      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/threads/manager`, {
        waitUntil: "domcontentloaded",
      });

      // Wait until at least one agent-message bubble is visible
      await expect(page.locator('[data-testid="agent-message"]').first()).toBeVisible({
        timeout: 30_000,
      });
      // Briefly confirm no additional messages arrive from SSE
      await page.waitForTimeout(2_000);

      await page.screenshot({
        path: "test-results/evaluator-reject.png",
        fullPage: true,
      });
      console.log("[evaluator-reject] screenshot saved: test-results/evaluator-reject.png");
    });
  });
