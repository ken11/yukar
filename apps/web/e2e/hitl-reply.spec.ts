/**
 * HITL reply E2E test — replying to a parked run wakes it.
 *
 * Purpose:
 *   After the manager asks its question in body text and the run parks in
 *   "waiting" (your turn), verify in a real browser that submitting a reply
 *   from the thread-composer wakes the run, and that subsequent manager turns
 *   (task_update → dispatch), worker, and evaluator all execute until the work
 *   is done (run parks in "waiting" with every task done — a conversation run
 *   never "completes").
 *
 * Verification flow (serial):
 *   1. Create project
 *   2. Create epic
 *   3. Start run → navigate to manager thread
 *      Poll until the question text is shown and run/state.status is "waiting"
 *   4. Enter reply in thread-composer and submit
 *      Poll until the work is done (waiting + all tasks done)
 *      Assert that subsequent agent bubbles (task_update / dispatch) appear
 *      Save screenshot (test-results/hitl-reply.png)
 *
 * Waiting / determinism:
 *   Everything waits on expect.poll(run/state + tasks) or DOM visibility.
 *   No fixed sleeps to assume state. retries:0 / workers:1.
 */

import { expect, test } from "@playwright/test";
import { HITL_REPLY_SEED } from "./hitl-reply-seed";
import { waitForRunWaiting, waitForWorkDone } from "./wait-helpers";

const QUESTION_TEXT = "この実装計画で進めてよいですか？";
const REPLY_TEXT = "はい、進めてください。";

test.describe
  .serial("hitl-reply: replying to a parked run wakes it and drives the work to done", () => {
    const state = {
      projectId: "",
      epicId: "",
    };

    // ---- 1. Create project ----

    test("1. create project", async ({ page }) => {
      await page.goto("/projects");

      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("project-name-input").fill("hitl-reply-project");
      await page.getByTestId("repo-path-input-0").fill(HITL_REPLY_SEED.repoDir);

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

      await page.getByTestId("epic-title-input").fill("hitl-reply epic");
      await page.getByTestId("epic-description-input").fill("Test the HITL reply flow.");
      await page.getByTestId("epic-ac-input").fill("User replies and the work is done.");

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

    // ---- 3. Start run → question bubble + waiting ----

    test("3. start run, verify the question bubble and the waiting park", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}`);

      const startBtn = page.getByTestId("start-run-btn");
      await expect(startBtn).toBeVisible({ timeout: 10_000 });
      await startBtn.click();

      // Redirect to the manager thread page
      await expect(page).toHaveURL(/\/threads\/manager/, { timeout: 15_000 });

      // Wait until the question bubble appears (an ordinary assistant message)
      const questionBubble = page.getByText(QUESTION_TEXT);
      await expect(questionBubble).toBeVisible({ timeout: 30_000 });

      // Standard turn-end wait: the run parks in "waiting"
      await waitForRunWaiting(page, state.projectId, state.epicId, { timeout: 30_000 });
    });

    // ---- 4. Submit reply via composer → verify the work is done ----

    test("4. reply via composer — the run wakes and drives the work to done", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      // Navigate to the manager thread page
      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/threads/manager`);

      // Confirm the question bubble is visible
      const questionBubble = page.getByText(QUESTION_TEXT);
      await expect(questionBubble).toBeVisible({ timeout: 30_000 });

      // Confirm thread-composer is visible (it should appear because this is an active trial)
      const composer = page.getByTestId("thread-composer");
      await expect(composer).toBeVisible({ timeout: 10_000 });

      // Type the reply and submit (Cmd+Enter or click the send button)
      await composer.fill(REPLY_TEXT);

      // Click the send button (button with a send icon)
      const sendBtn = page
        .locator("button")
        .filter({ hasText: /send|送信/i })
        .first();
      await sendBtn.click();

      // Standard work-done wait: the reply wakes the run, the manager
      // dispatches, worker/evaluator run, and the run parks in "waiting" with
      // every task done. ("waiting" alone would match the pre-reply park —
      // the all-tasks-done condition is what proves the wake happened.)
      await waitForWorkDone(page, state.projectId, state.epicId);

      // Assert that subsequent agent bubbles appear after the reply
      // Tool-call entries from task_update / dispatch should be present in agent-message bubbles
      const agentMessages = page.locator('[data-testid="agent-message"]');
      await expect(agentMessages.first()).toBeVisible({ timeout: 30_000 });

      // Verify that the task_update tool call is displayed
      const taskUpdateBubble = page
        .locator('[data-testid="agent-message"]')
        .filter({ hasText: "task_update" });
      await expect(taskUpdateBubble.first()).toBeVisible({ timeout: 30_000 });

      // Save screenshot
      await page.screenshot({
        path: "test-results/hitl-reply.png",
        fullPage: true,
      });
    });
  });
