/**
 * Budget exceeded E2E test.
 *
 * Purpose:
 *   Injects a large usage into the Manager's first turn and verifies that the actual cost
 *   (approx. $42 USD) is recorded from the price table of the model_id (sonnet-4-6) that
 *   the fake identifies itself as via settings.
 *   Then sets a low positive budget limit ($1 USD) so the actual cost naturally exceeds it,
 *   and verifies that the Usage page shows the over-budget indicator and that a new
 *   POST /run is blocked with 409.
 *
 * Verification flow:
 *   1. Create project → create epic → start run
 *   2. Wait for run to complete
 *   3. GET /api/usage and confirm total_tokens > 0 and cost_usd > 0
 *   4. PUT /api/usage/budget with limit_usd=1 (a low positive value that the actual cost of $42 naturally exceeds)
 *   5. GET /api/usage and confirm budget.over_budget == true
 *   6. Confirm the over-budget indicator is shown on the Usage page (/usage)
 *   7. Confirm a new POST /run returns 409 (budget-exceeded block)
 *
 * Notes:
 *   Due to Part A, the fake identifies itself as the model_id from settings rather than "unknown",
 *   so the cost is calculated.
 *   over_budget is satisfied naturally by "actual cost >= positive limit", not by a limit=0 tautology.
 */

import { expect, test } from "@playwright/test";
import { BUDGET_SEED } from "./budget-seed";

test.describe
  .serial("Budget exceeded scenario", () => {
    const state = {
      projectId: "",
      epicId: "",
    };

    // ---- 1. Create project and epic ----

    test("1. create project", async ({ page }) => {
      await page.goto("/projects");

      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("project-name-input").fill("budget-project");
      await page.getByTestId("repo-path-input-0").fill(BUDGET_SEED.repoDir);
      await page.getByTestId("form-dialog-submit").click();

      const row = page.locator('[data-testid^="project-row-"]').first();
      await expect(row).toBeVisible({ timeout: 15_000 });

      const testId = await row.getAttribute("data-testid");
      state.projectId = testId?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();
    });

    test("2. create epic", async ({ page }) => {
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}`);

      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("epic-title-input").fill("budget epic");
      await page.getByTestId("epic-description-input").fill("Test budget exceeded scenario.");
      await page.getByTestId("epic-ac-input").fill("Budget is exceeded and run is blocked.");

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

    // ---- 2. Start run ----

    test("3. start run", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}`);

      const startBtn = page.getByTestId("start-run-btn");
      await expect(startBtn).toBeVisible({ timeout: 10_000 });
      await startBtn.click();

      // Redirects to the manager thread page
      await expect(page).toHaveURL(/\/threads\//, { timeout: 15_000 });
    });

    // ---- 3. Run completes & confirm total_tokens > 0 ----

    test("4. run completes and total_tokens > 0", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      // Wait for the run to reach a terminal-ish state (run/state — the epic
      // itself stays open under the 1-bit lifecycle).
      await expect
        .poll(
          async () => {
            const res = await page.request.get(
              `/api/projects/${state.projectId}/epics/${state.epicId}/run/state`,
            );
            const body = await res.json();
            return body.status;
          },
          { timeout: 90_000, intervals: [500, 1000, 1000] },
        )
        .toMatch(/^(completed|interrupted|idle|error)$/);

      // Check usage state via GET /api/usage
      const usageRes = await page.request.get("/api/usage");
      expect(usageRes.ok()).toBe(true);
      const usage = await usageRes.json();

      console.log(`[budget] GET /api/usage total_tokens = ${usage.total_tokens}`);
      console.log(
        `[budget] GET /api/usage total_cost_usd = ${usage.total_cost_usd}, total_cost_jpy = ${usage.total_cost_jpy}`,
      );

      // Confirm usage injection is working (total_tokens > 0)
      expect(
        usage.total_tokens,
        `total_tokens is 0 — usage injection may not be working (total_tokens=${usage.total_tokens})`,
      ).toBeGreaterThan(0);

      // Due to Part A, the fake identifies itself as the model_id from settings (sonnet-4-6),
      // so the actual cost is calculated from the injected tokens.
      // cost is required to be > 0 unconditionally (verifying natural cost recording).
      expect(
        usage.total_cost_usd,
        "total_cost_usd is 0 — the fake's priced model_id is not taking effect (Part A)",
      ).toBeGreaterThan(0);
      console.log(`[budget] cost calculated: $${usage.total_cost_usd} / ¥${usage.total_cost_jpy}`);
    });

    // ---- 4. Force budget to be exceeded ----

    test("5. natural over-budget: spent cost exceeds a low positive limit", async ({ page }) => {
      // After the run completes, set a low positive limit ($1 USD). The actual cost (approx. $42 USD)
      // naturally exceeds the limit: is_over_budget() = spent_usd >= limit_usd → 42 >= 1 → true
      // (eliminates the artificiality of limit=0).
      const budgetRes = await page.request.put("/api/usage/budget", {
        data: { limit_usd: 1 },
        headers: { "Content-Type": "application/json" },
      });
      expect(budgetRes.status()).toBe(200);

      const budgetBody = await budgetRes.json();
      console.log(
        `[budget] PUT /api/usage/budget {limit_usd:1} response: ${JSON.stringify(budgetBody)}`,
      );

      // Confirm over_budget=true via GET /api/usage
      const usageRes = await page.request.get("/api/usage");
      expect(usageRes.ok()).toBe(true);
      const usage = await usageRes.json();

      console.log(`[budget] GET /api/usage budget.over_budget = ${usage.budget?.over_budget}`);
      console.log(
        `[budget] GET /api/usage budget: spent_usd=${usage.budget?.spent_usd}, limit_usd=${usage.budget?.limit_usd}`,
      );

      expect(
        usage.budget?.over_budget,
        `actual cost should exceed the limit ($1) but over_budget is false (spent_usd=${usage.budget?.spent_usd}, limit_usd=${usage.budget?.limit_usd})`,
      ).toBe(true);
    });

    // ---- 5. Confirm over-budget indicator on the Usage page ----

    test("6. Usage page shows over_budget indicator", async ({ page }) => {
      // Navigate to the Usage page (/usage)
      await page.goto("/usage", { waitUntil: "domcontentloaded" });

      // Wait for the page to render
      await page.waitForTimeout(2_000);

      // Confirm the over-budget text (the over_budget display in budget-form.tsx)
      // When over_budget=true, the budget.overBudget i18n key is shown (detected for both JA and EN)
      const overBudgetText = page
        .getByText("予算上限を超過しています")
        .or(page.getByText("Budget limit exceeded"));
      const overBudgetCount = await overBudgetText.count();
      console.log(`[budget] over_budget text display count = ${overBudgetCount}`);

      expect(
        overBudgetCount,
        'over_budget text is not shown on the Usage page ("予算上限を超過しています" or "Budget limit exceeded")',
      ).toBeGreaterThanOrEqual(1);

      // Save screenshot
      await page.screenshot({
        path: "test-results/budget-usage-page.png",
        fullPage: true,
      });
      console.log("[budget] screenshot saved: test-results/budget-usage-page.png");
    });

    // ---- 6. New run returns 409 after budget is exceeded ----

    test("7. POST /run returns 409 after budget exceeded", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      // Re-confirm over_budget via the usage API to be safe
      const usageRes = await page.request.get("/api/usage");
      const usage = await usageRes.json();
      console.log(
        `[budget] over_budget before POST /run = ${usage.budget?.over_budget} (spent_usd=${usage.budget?.spent_usd}, limit_usd=${usage.budget?.limit_usd})`,
      );

      // Fail the test if over_budget is not true (should have been set in test 5)
      expect(
        usage.budget?.over_budget,
        "over_budget is false — the budget limit set in test 5 is not taking effect",
      ).toBe(true);

      // Attempt to run against the existing epic (re-running a completed epic is also subject to 409)
      // The budget check in start_run happens at the top of POST /run
      const runRes = await page.request.post(
        `/api/projects/${state.projectId}/epics/${state.epicId}/run`,
      );
      const runStatus = runRes.status();
      console.log(`[budget] POST /run status = ${runStatus}`);

      // Should return 409 when over_budget = true
      expect(
        runStatus,
        `POST /run while over budget should return 409 but returned ${runStatus}`,
      ).toBe(409);

      const runBody = await runRes.json();
      console.log(`[budget] POST /run 409 body: ${JSON.stringify(runBody)}`);
      // Confirm that detail contains "Budget" or "budget"
      expect(
        (runBody.detail ?? "").toLowerCase(),
        `409 detail is not a "budget"-related message: "${runBody.detail}"`,
      ).toContain("budget");
    });
  });
