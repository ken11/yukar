/**
 * MessageTurn streaming E2E test.
 *
 * Purpose:
 *   Verify in a real browser that the FakeModel's MessageTurn (text + tool_use mixed
 *   in a single message) renders as "a single assistant bubble containing both a text
 *   block and a tool-call row", and capture a screenshot as evidence.
 *
 * Verification flow:
 *   1. Create project → create epic → start run
 *   2. Navigate to the manager thread at /threads/manager
 *   3. Assert that the text "まず計画を整理します" and the task_update ToolCallRow
 *      coexist inside a single [data-testid="agent-message"] bubble
 *   4. Save a screenshot to test-results/streaming-message-bubble.png
 *   5. Confirm via API polling that the work is done (run "waiting" + all tasks done)
 *   6. After the work is done, count the bubbles to detect duplicate rendering (expected: 1)
 *   7. Save an element screenshot to test-results/streaming-combined-bubble.png
 */

import { expect, test } from "@playwright/test";
import { STREAMING_SEED } from "./streaming-seed";
import { waitForWorkDone } from "./wait-helpers";

test.describe
  .serial("MessageTurn streaming — mixed bubble", () => {
    const state = {
      projectId: "",
      epicId: "",
      threadUrl: "",
      /** Console error messages collected while the run is in progress */
      consoleErrors: [] as Array<{ type: string; text: string; location: string }>,
      pageErrors: [] as Array<{ message: string; stack?: string }>,
    };

    // ---- 1. Create project ----

    test("1. create project", async ({ page }) => {
      // Console error collection hook (only active within this test, but since page is
      // recreated in subsequent tests we install it in each test; state is shared across describe)
      page.on("console", (msg) => {
        if (msg.type() === "error" || msg.type() === "warning") {
          state.consoleErrors.push({
            type: msg.type(),
            text: msg.text(),
            location: msg.location().url ?? "",
          });
        }
      });
      page.on("pageerror", (err) => {
        state.pageErrors.push({ message: err.message, stack: err.stack });
      });

      await page.goto("/projects");

      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("project-name-input").fill("streaming-project");
      await page.getByTestId("repo-path-input-0").fill(STREAMING_SEED.repoDir);

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

      page.on("console", (msg) => {
        if (msg.type() === "error" || msg.type() === "warning") {
          state.consoleErrors.push({
            type: msg.type(),
            text: msg.text(),
            location: msg.location().url ?? "",
          });
        }
      });
      page.on("pageerror", (err) => {
        state.pageErrors.push({ message: err.message, stack: err.stack });
      });

      await page.goto(`/projects/${state.projectId}`);

      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("epic-title-input").fill("streaming epic");
      await page.getByTestId("epic-description-input").fill("Test MessageTurn mixed bubble.");
      await page.getByTestId("epic-ac-input").fill("Mixed bubble renders correctly.");

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

    // ---- 3. Start Run → verify MessageTurn bubble (while streaming) ----

    test("3. start run and verify mixed-content bubble", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      page.on("console", (msg) => {
        if (msg.type() === "error" || msg.type() === "warning") {
          state.consoleErrors.push({
            type: msg.type(),
            text: msg.text(),
            location: msg.location().url ?? "",
          });
        }
      });
      page.on("pageerror", (err) => {
        state.pageErrors.push({ message: err.message, stack: err.stack });
      });

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}`);

      const startBtn = page.getByTestId("start-run-btn");
      await expect(startBtn).toBeVisible({ timeout: 10_000 });
      await startBtn.click();

      // Redirected to the manager thread page
      await expect(page).toHaveURL(/\/threads\/manager/, { timeout: 15_000 });

      // Save the URL for reuse in later tests
      state.threadUrl = page.url();

      // Wait until the text portion of the MessageTurn is rendered
      const textContent = page.getByText("まず計画を整理します").first();
      await expect(textContent).toBeVisible({ timeout: 30_000 });

      // Identify the agent-message bubble that contains the text
      const bubble = page
        .locator('[data-testid="agent-message"]')
        .filter({ hasText: "まず計画を整理します" })
        .first();
      await expect(bubble).toBeVisible({ timeout: 10_000 });

      // Verify that the task_update ToolCallRow exists inside the same bubble
      const toolCallRow = bubble.locator("button").filter({ hasText: "task_update" }).first();
      await expect(toolCallRow).toBeVisible({ timeout: 10_000 });

      // Save a full-page screenshot
      await page.screenshot({
        path: "test-results/streaming-message-bubble.png",
        fullPage: true,
      });
    });

    // ---- 4. Confirm the run advances to work done ----

    test("4. run completes", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      page.on("console", (msg) => {
        if (msg.type() === "error" || msg.type() === "warning") {
          state.consoleErrors.push({
            type: msg.type(),
            text: msg.text(),
            location: msg.location().url ?? "",
          });
        }
      });
      page.on("pageerror", (err) => {
        state.pageErrors.push({ message: err.message, stack: err.stack });
      });

      await waitForWorkDone(page, state.projectId, state.epicId);
    });

    // ---- 5. Confirm whether duplicate rendering occurs & take element screenshot ----

    test("5. verify no duplicate bubble after the work is done", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      page.on("console", (msg) => {
        if (msg.type() === "error" || msg.type() === "warning") {
          state.consoleErrors.push({
            type: msg.type(),
            text: msg.text(),
            location: msg.location().url ?? "",
          });
        }
      });
      page.on("pageerror", (err) => {
        state.pageErrors.push({ message: err.message, stack: err.stack });
      });

      // After the work is confirmed done, navigate back to the manager thread (opening a fresh page after SSE ended)
      const managerUrl = state.threadUrl
        ? state.threadUrl
        : `/projects/${state.projectId}/epics/${state.epicId}/threads/manager`;
      await page.goto(managerUrl, { waitUntil: "domcontentloaded" });

      // SSE connections are always open, so networkidle is never reached.
      // Wait until at least one agent-message bubble appears from the REST-loaded messages.
      await expect(page.locator('[data-testid="agent-message"]').first()).toBeVisible({
        timeout: 30_000,
      });
      // Short wait for bubbles to stabilize (confirm no additional messages arrive via SSE)
      await page.waitForTimeout(2_000);

      // Count the number of agent-message bubbles containing "まず計画を整理します"
      const bubbles = page
        .locator('[data-testid="agent-message"]')
        .filter({ hasText: "まず計画を整理します" });

      const count = await bubbles.count();
      console.log(
        `[streaming-test] bubble count for "まず計画を整理します" after work done = ${count}`,
      );

      if (count !== 1) {
        console.error(
          `[streaming-test] duplicate rendering detected: expected 1 bubble, got ${count}. ` +
            "The live bubble may not have been cleared after REST messages arrived.",
        );
      }

      // Save an element-level screenshot
      const firstBubble = bubbles.first();
      await firstBubble.scrollIntoViewIfNeeded();

      // Element screenshot (test-results/streaming-combined-bubble.png)
      await firstBubble.screenshot({
        path: "test-results/streaming-combined-bubble.png",
      });
      console.log(
        "[streaming-test] element screenshot saved: test-results/streaming-combined-bubble.png",
      );

      // Verify that the task_update ToolCallRow exists inside the same bubble
      const toolCallInBubble = firstBubble.locator("button").filter({ hasText: "task_update" });
      const toolCount = await toolCallInBubble.count();
      console.log(
        `[streaming-test] task_update ToolCallRow count inside the same bubble = ${toolCount}`,
      );

      // Report a summary of console errors / page errors
      const errorOnly = state.consoleErrors.filter((e) => e.type === "error");
      const warnOnly = state.consoleErrors.filter((e) => e.type === "warning");
      if (errorOnly.length > 0) {
        console.error(
          `[streaming-test] console error (${errorOnly.length} items):\n` +
            errorOnly.map((e) => `  [${e.type}] ${e.text} (${e.location})`).join("\n"),
        );
      } else {
        console.log("[streaming-test] console error: none");
      }
      if (warnOnly.length > 0) {
        console.warn(
          `[streaming-test] console warning (${warnOnly.length} items):\n` +
            warnOnly.map((e) => `  [${e.type}] ${e.text} (${e.location})`).join("\n"),
        );
      }
      if (state.pageErrors.length > 0) {
        console.error(
          `[streaming-test] pageerror (${state.pageErrors.length} items):\n` +
            state.pageErrors.map((e) => `  ${e.message}\n    ${e.stack ?? ""}`).join("\n"),
        );
      } else {
        console.log("[streaming-test] pageerror: none");
      }

      // Report duplicate rendering as a failure (include the actual count for investigation)
      expect(
        count,
        `The "まず計画を整理します" bubble should be exactly 1, but ${count} detected ` +
          "(suspected duplicate rendering: live bubble was not cleared after REST messages arrived)",
      ).toBe(1);

      // Verify that task_update is inside the same bubble
      expect(
        toolCount,
        `task_update ToolCallRow should be exactly 1 inside the same bubble, but ${toolCount} detected`,
      ).toBeGreaterThanOrEqual(1);
    });

    // ---- 6. Strict bubble isolation test (permanent guard against a past bug) ----
    //
    // There was a past bug where "Manager responses kept being appended to the same existing bubble".
    // The STREAMING_FAKE_SCRIPT script consists of the following utterances:
    //   (0) MessageTurn: text "まず計画を整理します" + task_update ToolCall           → 1 bubble
    //   (1) ToolUseTurn: dispatch                                                    → 1 bubble
    //   (2) TextTurn: "Epic work is done." (final report; run parks in waiting)      → 1 bubble
    // There should be 3 bubbles total in the manager thread; fewer indicates the append bug.
    //
    // Additionally, assert that the first bubble (containing "まず計画を整理します")
    // does not include content from subsequent utterances
    // (i.e., strings like dispatch / the final report must not bleed in).

    test("6. bubble isolation — exact count and no cross-contamination", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      // Navigate to the manager thread after the work is done
      const managerUrl = state.threadUrl
        ? state.threadUrl
        : `/projects/${state.projectId}/epics/${state.epicId}/threads/manager`;
      await page.goto(managerUrl, { waitUntil: "domcontentloaded" });

      // Wait until at least one bubble is visible
      await expect(page.locator('[data-testid="agent-message"]').first()).toBeVisible({
        timeout: 30_000,
      });
      // Short wait to confirm no additional messages arrive via SSE
      await page.waitForTimeout(2_000);

      // Count all bubbles and log the result
      const allBubbles = page.locator('[data-testid="agent-message"]');
      const totalCount = await allBubbles.count();
      console.log(
        `[bubble-isolation] total agent-message bubble count = ${totalCount} (expected: 3 utterances)`,
      );

      // Verify the count matches the number of scripted utterances
      // MessageTurn(1) + dispatch(1) + "Epic work is done."(1) = 3
      expect(
        totalCount,
        `Manager should have 3 utterances, but ${totalCount} bubbles detected ` +
          "(totalCount < 3 suggests the append bug where bubbles are merged; " +
          "totalCount > 3 suggests duplicate rendering)",
      ).toBe(3);

      // Get the textContent of the first bubble (which contains "まず計画を整理します")
      const firstBubble = allBubbles.first();
      const firstBubbleText = (await firstBubble.textContent()) ?? "";
      console.log(
        `[bubble-isolation] first bubble textContent (excerpt): ${firstBubbleText.slice(0, 120)}`,
      );

      // Assert that content from subsequent utterances has not bled into the first bubble
      // (if the append bug is present, strings like dispatch / the final report will appear)
      expect(
        firstBubbleText.includes("dispatch"),
        'The first bubble contains the string "dispatch" (subsequent utterance may have been appended)',
      ).toBe(false);
      expect(
        firstBubbleText.includes("Epic work is done."),
        'The first bubble contains the string "Epic work is done." (subsequent utterance may have been appended)',
      ).toBe(false);

      // Re-confirm that the first bubble contains "まず計画を整理します" (the core of the script)
      expect(
        firstBubbleText.includes("まず計画を整理します"),
        'The first bubble does not contain "まず計画を整理します"',
      ).toBe(true);

      console.log("[bubble-isolation] bubble isolation confirmed: OK (no append bug)");
    });
  });
