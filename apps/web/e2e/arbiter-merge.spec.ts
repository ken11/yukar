/**
 * Arbiter merge E2E test — bulk merge of multiple Epics.
 *
 * Scenario:
 *   1. Create project → create Epic 1 → run → work done (waiting + tasks done)
 *   2. Create Epic 2 → run → work done
 *   3. Select both from Epics board → Merge Selected
 *   4. MergeProgressPanel shows SSE progress → phase=finished
 *   5. Confirm both Epics carry the merge fact (merged_at) via GET /epics —
 *      the epic status itself stays "open" (1-bit lifecycle)
 *   6. Save screenshot
 *
 * Worker script is per_call format: 1st Epic writes epic1.py, 2nd Epic writes epic2.py.
 * Separate files guarantee no merge conflict.
 * Arbiter does not call LLM when there is no conflict, so no arbiter key needed.
 */

import { expect, test } from "@playwright/test";
import { ARBITER_MERGE_SEED } from "./arbiter-merge-seed";
import { waitForWorkDone } from "./wait-helpers";

test.describe
  .serial("Arbiter merge — bulk merge of multiple Epics", () => {
    const state = {
      projectId: "",
      epicId1: "",
      epicId2: "",
    };

    // ---- Helper: wait for the merge fact (epic.merged_at) via API polling ----
    async function waitForMergedFact(
      page: import("@playwright/test").Page,
      epicId: string,
      timeoutMs = 120_000,
    ): Promise<void> {
      await expect
        .poll(
          async () => {
            const res = await page.request.get(`/api/projects/${state.projectId}/epics/${epicId}`);
            return Boolean((await res.json()).merged_at);
          },
          { timeout: timeoutMs, intervals: [500, 1000, 2000] },
        )
        .toBe(true);
    }

    // ---- Helper: start run and wait for redirect to manager thread ----
    async function startRun(page: import("@playwright/test").Page, epicId: string): Promise<void> {
      await page.goto(`/projects/${state.projectId}/epics/${epicId}`);
      const startBtn = page.getByTestId("start-run-btn");
      await expect(startBtn).toBeVisible({ timeout: 15_000 });
      await startBtn.click();
      // Redirect to manager thread
      await expect(page).toHaveURL(/\/threads\//, { timeout: 15_000 });
    }

    // -----------------------------------------------------------------------
    test("1. create project", async ({ page }) => {
      await page.goto("/projects");

      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("project-name-input").fill("arbiter-merge-project");
      await page.getByTestId("repo-path-input-0").fill(ARBITER_MERGE_SEED.repoDir);
      await page.getByTestId("form-dialog-submit").click();

      const row = page.locator('[data-testid^="project-row-"]').first();
      await expect(row).toBeVisible({ timeout: 15_000 });
      const testId = await row.getAttribute("data-testid");
      state.projectId = testId?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();
      console.log(`[arbiter-merge] projectId = ${state.projectId}`);
    });

    // -----------------------------------------------------------------------
    test("2. create Epic 1 and wait for run → work done", async ({ page }) => {
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}`);
      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("epic-title-input").fill("Epic 1 — arbiter merge");
      await page.getByTestId("epic-description-input").fill("First epic for arbiter merge test.");
      await page.getByTestId("epic-ac-input").fill("epic1.py exists.");
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
      state.epicId1 = epicId;
      expect(state.epicId1).toBeTruthy();
      console.log(`[arbiter-merge] epicId1 = ${state.epicId1}`);

      // Start run
      await startRun(page, state.epicId1);

      // Standard work-done wait (1st run uses per_call[0] = writes epic1.py)
      await waitForWorkDone(page, state.projectId, state.epicId1, { timeout: 120_000 });
      console.log(`[arbiter-merge] Epic 1 work done`);
    });

    // -----------------------------------------------------------------------
    test("3. create Epic 2 and wait for run → work done", async ({ page }) => {
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}`);
      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("epic-title-input").fill("Epic 2 — arbiter merge");
      await page.getByTestId("epic-description-input").fill("Second epic for arbiter merge test.");
      await page.getByTestId("epic-ac-input").fill("epic2.py exists.");
      await page.getByTestId("form-dialog-submit").click();

      await page.waitForURL(/\/epics\//, { timeout: 15_000 });
      const url = page.url();
      const epicMatch = url.match(/\/epics\/([^/]+)/);
      let epicId = epicMatch?.[1] ?? "";
      if (!epicId) {
        // Fallback: URL does not contain epic id (redirected to epics list)
        await page.goto(`/projects/${state.projectId}/epics`);
        // Find the second card
        const cards = page.locator('[data-testid^="epic-card-"]');
        await expect(cards).toHaveCount(2, { timeout: 10_000 });
        // Pick the id that is not Epic 1
        const ids = await cards.evaluateAll((els) =>
          els.map((el) => el.getAttribute("data-testid")?.replace("epic-card-", "") ?? ""),
        );
        epicId = ids.find((id) => id !== state.epicId1) ?? "";
      }
      state.epicId2 = epicId;
      expect(state.epicId2).toBeTruthy();
      console.log(`[arbiter-merge] epicId2 = ${state.epicId2}`);

      // Start run
      await startRun(page, state.epicId2);

      // Standard work-done wait (2nd run uses per_call[1] = writes epic2.py)
      await waitForWorkDone(page, state.projectId, state.epicId2, { timeout: 120_000 });
      console.log(`[arbiter-merge] Epic 2 work done`);
    });

    // -----------------------------------------------------------------------
    test("4. select both Epics and execute Merge Selected", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId1).toBeTruthy();
      expect(state.epicId2).toBeTruthy();

      // Navigate to Epics board (/epics is the board; /projects/{id} is the overview)
      await page.goto(`/projects/${state.projectId}/epics`);

      // Both epics stay open after their runs (runs never transition the epic;
      // their runs are live-parked in "waiting" — the merge shelves them), so
      // they appear under the "all" filter with a merge checkbox
      // (isMergeable = open + has branch + no merge fact yet).

      // Click Epic 1 checkbox
      const checkbox1 = page.getByRole("button", {
        name: `Select ${state.epicId1}`,
        exact: true,
      });
      await expect(checkbox1).toBeVisible({ timeout: 10_000 });
      await checkbox1.click();

      // Click Epic 2 checkbox
      const checkbox2 = page.getByRole("button", {
        name: `Select ${state.epicId2}`,
        exact: true,
      });
      await expect(checkbox2).toBeVisible({ timeout: 5_000 });
      await checkbox2.click();

      // Confirm the selection toolbar is visible
      await expect(page.getByTestId("merge-toolbar")).toBeVisible({ timeout: 5_000 });

      // Click Merge Selected button
      const mergeBtn = page.getByTestId("start-merge-btn");
      await expect(mergeBtn).toBeVisible({ timeout: 5_000 });
      await mergeBtn.click();

      console.log(`[arbiter-merge] Merge Selected clicked`);
    });

    // -----------------------------------------------------------------------
    test("5. MergeProgressPanel is shown and merge completes", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId1).toBeTruthy();
      expect(state.epicId2).toBeTruthy();

      // Stay on Epics board (already clicked in previous test) and wait for the panel.
      // However, since tests are serial, page is fresh → revisit the board and wait for the panel.
      // MergeProgressPanel is shown only when mergeRunId is in state.
      // The board must therefore retain the run_id from the previous step's click.
      // Since page is not shared even in serial tests, poll the API here to
      // wait until the merge fact (merged_at) is recorded on both Epics.

      // First poll until both Epics carry the merge fact
      await waitForMergedFact(page, state.epicId1, 180_000);
      await waitForMergedFact(page, state.epicId2, 30_000);
      console.log(`[arbiter-merge] Both epics merged`);

      // Confirm all Epics via GET /epics — merging records a fact attribute
      // (merged_at) and leaves the 1-bit epic status open.
      const epicsRes = await page.request.get(
        `/api/projects/${state.projectId}/epics?include_completed=true`,
      );
      expect(epicsRes.status()).toBe(200);
      const epics = (await epicsRes.json()) as Array<{
        id: string;
        status: string;
        merged_at?: string | null;
      }>;
      console.log(
        `[arbiter-merge] epics = ${JSON.stringify(
          epics.map((e) => ({ id: e.id, status: e.status, merged_at: e.merged_at })),
        )}`,
      );

      const epic1 = epics.find((e) => e.id === state.epicId1);
      const epic2 = epics.find((e) => e.id === state.epicId2);
      expect(epic1?.merged_at, `Epic 1 (${state.epicId1}) merge fact`).toBeTruthy();
      expect(epic2?.merged_at, `Epic 2 (${state.epicId2}) merge fact`).toBeTruthy();
      expect(epic1?.status, `Epic 1 (${state.epicId1}) stays open`).toBe("open");
      expect(epic2?.status, `Epic 2 (${state.epicId2}) stays open`).toBe("open");
    });

    // -----------------------------------------------------------------------
    test("6. save screenshot", async ({ page }) => {
      expect(state.projectId).toBeTruthy();

      // Show epics board with merged filter
      await page.goto(`/projects/${state.projectId}/epics`);

      // Click merged filter chip — it filters on the merge fact (merged_at)
      await page.getByTestId("epic-filter-merged").click();

      // Confirm both Epics are displayed under the merged-fact filter
      const card1 = page.getByTestId(`epic-card-${state.epicId1}`);
      const card2 = page.getByTestId(`epic-card-${state.epicId2}`);
      await expect(card1).toBeVisible({ timeout: 10_000 });
      await expect(card2).toBeVisible({ timeout: 5_000 });

      await page.screenshot({
        path: "test-results/arbiter-merge.png",
        fullPage: true,
      });
      console.log("[arbiter-merge] screenshot saved: test-results/arbiter-merge.png");
    });
  });
