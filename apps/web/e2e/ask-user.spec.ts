/**
 * Question / reload E2E test (historically the ask_user scenario).
 *
 * Purpose:
 *   Under P3 a question is plain assistant BODY TEXT: the manager writes it
 *   and ends its turn, which parks the run in "waiting" (your turn).  Verify
 *   in a real browser that the question bubble appears, that it survives a
 *   page reload (it is an ordinary conversation message — no pending_question
 *   carrier), and that a conversational reply parks the run again without any
 *   host-injected dispatch.
 *
 * Verification flow:
 *   1. Create project → create epic → start run
 *   2. Navigate to the manager thread
 *   3. Assert the run parks in "waiting" and the question bubble appears
 *   4. page.reload() → the same question bubble is still present (thread history)
 *   5. Reply with a question → manager answers in text → run parks in "waiting"
 *      again and the "your turn" banner shows
 */

import { expect, test } from "@playwright/test";
import { ASK_USER_ANSWER_TEXT, ASK_USER_SEED } from "./ask-user-seed";
import { waitForRunWaiting } from "./wait-helpers";

const QUESTION_TEXT = "この計画で進めてよいですか？";
const YOUR_TURN_BANNER_TEXT = "あなたの番です — 返信するとエージェントが続けます";

test.describe
  .serial("question text turn + reload restore", () => {
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
      await page.getByTestId("epic-description-input").fill("Test the question-in-body-text flow.");
      await page.getByTestId("epic-ac-input").fill("User approves the plan.");

      await page.getByTestId("form-dialog-submit").click();

      // After epic creation the app redirects to the epic page.
      // Extract the epic ID from the URL (/projects/{p}/epics/{epicId}/...).
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

    // ---- 3. Start run → turn ends → question bubble + waiting ----

    test("3. start run: the question renders as a normal bubble and the run parks in waiting", async ({
      page,
    }) => {
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

      // The question is an ordinary assistant message (no synthetic
      // __awaiting__ bubble any more).
      const questionBubble = page.getByText(QUESTION_TEXT);
      await expect(questionBubble).toBeVisible({ timeout: 30_000 });

      // Standard turn-end wait: the run parks in "waiting".
      await waitForRunWaiting(page, state.projectId, state.epicId, { timeout: 30_000 });
    });

    // ---- 4. Assert question bubble persists after reload ----

    test("4. reload — the question bubble persists via the thread history", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      // Navigate to the manager thread page first and confirm the question bubble appears
      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/threads/manager`);

      const questionBubble = page.getByText(QUESTION_TEXT);
      await expect(questionBubble).toBeVisible({ timeout: 30_000 });

      // Reload the page
      await page.reload();

      // The question is a persisted conversation message, so it is restored
      // from the thread history (successor of the pending_question REST
      // restore guarantee — the question must never be lost on reload).
      await expect(questionBubble).toBeVisible({ timeout: 15_000 });

      // The "your turn" banner is restored from GET /run/state ("waiting").
      await expect(page.getByText(YOUR_TURN_BANNER_TEXT).first()).toBeVisible({ timeout: 15_000 });
    });

    // ---- 5. Conversational reply → tool-less answer parks the run ----

    test("5. question reply — manager answers in text and the run parks again (no auto-dispatch)", async ({
      page,
    }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/threads/manager`);
      await expect(page.getByText(QUESTION_TEXT)).toBeVisible({ timeout: 30_000 });

      // Ask a question instead of approving.
      const composer = page.getByTestId("thread-composer");
      await expect(composer).toBeVisible({ timeout: 10_000 });
      await composer.fill("その計画は具体的に何をしますか？");
      await page
        .locator("button")
        .filter({ hasText: /send|送信/i })
        .first()
        .click();

      // The manager answers in plain text (no tools) — visible as a new bubble.
      // This text is the deterministic marker that the woken turn has ended
      // (plain "waiting" polling could match the pre-reply park).
      await expect(page.getByText(ASK_USER_ANSWER_TEXT)).toBeVisible({ timeout: 30_000 });

      // The run parks in "waiting" — no dispatch was injected by the host.
      await waitForRunWaiting(page, state.projectId, state.epicId, { timeout: 30_000 });

      // The banner invites the next reply.
      await expect(page.getByText(YOUR_TURN_BANNER_TEXT).first()).toBeVisible({ timeout: 15_000 });

      // Reload: the waiting state and the full conversation must survive via
      // REST restore (run/state + thread history).
      await page.reload();
      await expect(page.getByText(YOUR_TURN_BANNER_TEXT).first()).toBeVisible({ timeout: 15_000 });
      await expect(page.getByText(ASK_USER_ANSWER_TEXT)).toBeVisible({ timeout: 15_000 });
    });
  });
