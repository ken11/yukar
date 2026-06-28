/**
 * ask_user / awaiting_input reload E2E test.
 *
 * Purpose:
 *   Verify in a real browser that the question bubble is restored after a page reload
 *   when the manager has called ask_user and the run is in awaiting_input.
 *
 * What is being validated:
 *   Demonstrates that the USER_INPUT_REQUESTED handler in lifecycle.ts
 *   no longer mutates awaitingInput when question="" (fix already applied).
 *
 * Verification flow:
 *   1. Create project → create epic → start run
 *   2. Navigate to the manager thread
 *   3. Assert that the run enters awaiting_input and the question bubble appears
 *   4. page.reload()
 *   5. Assert that the same question bubble is still present after reload
 *      (restored primarily from GET /run/state pending_question (REST); SSE backfill is secondary)
 */

import { expect, test } from "@playwright/test";
import { ASK_USER_SEED } from "./ask-user-seed";

const QUESTION_TEXT = "この計画で進めてよいですか？";

test.describe
  .serial("ask_user awaiting_input reload", () => {
    const state = {
      projectId: "",
      epicId: "",
    };

    // ---- 1. Create project ----

    test("1. create project", async ({ page }) => {
      await page.goto("/projects");

      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("project-name-input").fill("ask-user-project");
      await page.getByTestId("repo-path-input-0").fill(ASK_USER_SEED.repoDir);

      await page.getByTestId("form-dialog-submit").click();

      // Wait for project-row-{id} (changed from project-card-*)
      const row = page.locator('[data-testid^="project-row-"]').first();
      await expect(row).toBeVisible({ timeout: 15_000 });

      // Extract project ID from data-testid
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

      await page.getByTestId("epic-title-input").fill("ask-user epic");
      await page.getByTestId("epic-description-input").fill("Test ask_user HITL.");
      await page.getByTestId("epic-ac-input").fill("User approves the plan.");

      await page.getByTestId("form-dialog-submit").click();

      // After epic creation the app redirects to the epic page.
      // Extract the epic ID from the URL (/projects/{p}/epics/{epicId}/...).
      // If no epic-card is present, fall back to reading the ID from the redirected URL.
      await page.waitForURL(/\/epics\//, { timeout: 15_000 });
      const url = page.url();
      const epicMatch = url.match(/\/epics\/([^/]+)/);
      if (epicMatch) {
        state.epicId = epicMatch[1];
      } else {
        // Fallback: use epic-card-* if it becomes visible
        const epicCard = page.locator('[data-testid^="epic-card-"]').first();
        await expect(epicCard).toBeVisible({ timeout: 5_000 });
        state.epicId =
          (await epicCard.getAttribute("data-testid"))?.replace("epic-card-", "") ?? "";
      }
      expect(state.epicId).toBeTruthy();
    });

    // ---- 3. Start run → awaiting_input → verify question bubble ----

    test("3. start run and verify question bubble appears", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      // Navigate directly to the epic page (the page the app redirects to after epic creation)
      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}`);

      // Click the Start Run button
      const startBtn = page.getByTestId("start-run-btn");
      await expect(startBtn).toBeVisible({ timeout: 10_000 });
      await startBtn.click();

      // Expect redirect to the manager thread page
      await expect(page).toHaveURL(/\/threads\/manager/, { timeout: 15_000 });

      // Wait for ask_user to be called and the question bubble to appear in the conversation.
      // runtime.ts: when awaitingInput.question is non-empty, a synthetic message with id="__awaiting__" is appended.
      // message-row.tsx: its text content is rendered by MessageContent.
      const questionBubble = page.getByText(QUESTION_TEXT);
      await expect(questionBubble).toBeVisible({ timeout: 30_000 });
    });

    // ---- 4. Assert question bubble persists after reload ----

    test("4. reload - question bubble persists via REST pending_question (SSE backfill secondary)", async ({
      page,
    }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      // Navigate to the manager thread page first and confirm the question bubble appears
      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/threads/manager`);

      const questionBubble = page.getByText(QUESTION_TEXT);
      await expect(questionBubble).toBeVisible({ timeout: 30_000 });

      // Reload the page
      await page.reload();

      // Confirm the question text is restored after reload.
      // Primary source of restoration is GET /run/state pending_question (REST); SSE backfill is secondary.
      await expect(questionBubble).toBeVisible({ timeout: 15_000 });
    });
  });
