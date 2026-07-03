/**
 * Plan-approval-gate E2E test (bug ⑤).
 *
 * Proves the host-enforced approval gate in a real browser + real backend:
 *   - The Manager tries to `dispatch` BEFORE the user approves the plan. The
 *     host REJECTS it, so no Worker runs and the task stays "todo" — the run
 *     halts at ask_user (awaiting_input) instead of running away.
 *   - After the user replies (approval), the retried `dispatch` runs the Worker
 *     and the task reaches "done", and the run completes.
 *
 * The gate is proved deterministically via the tasks API: `run_dispatch` marks a
 * task in_progress before running the Worker, so a blocked dispatch leaves T1
 * "todo". Everything waits on expect.poll — no fixed sleeps. retries:0/workers:1.
 */

import { expect, test } from "@playwright/test";
import { PLAN_GATE_QUESTION, PLAN_GATE_SEED } from "./plan-gate-seed";

const REPLY_TEXT = "はい、承認します。進めてください。";

type RunStatus =
  | "idle"
  | "running"
  | "paused"
  | "awaiting_input"
  | "error"
  | "completed"
  | "interrupted";

test.describe
  .serial("plan-gate: dispatch is blocked until the user approves the plan", () => {
    const state = { projectId: "", epicId: "" };

    async function getRunStatus(
      page: import("@playwright/test").Page,
      projectId: string,
      epicId: string,
    ): Promise<RunStatus> {
      const res = await page.request.get(`/api/projects/${projectId}/epics/${epicId}/run/state`);
      const body = await res.json();
      return body.status as RunStatus;
    }

    async function getTaskStatus(
      page: import("@playwright/test").Page,
      projectId: string,
      epicId: string,
      taskId: string,
    ): Promise<string | undefined> {
      const res = await page.request.get(`/api/projects/${projectId}/epics/${epicId}/tasks`);
      if (!res.ok()) return undefined;
      const body = await res.json();
      const task = (body.tasks ?? []).find((t: { id: string }) => t.id === taskId);
      return task?.status as string | undefined;
    }

    // ---- 1. Create project ----
    test("1. create project", async ({ page }) => {
      await page.goto("/projects");
      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.getByTestId("project-name-input").fill("plan-gate-project");
      await page.getByTestId("repo-path-input-0").fill(PLAN_GATE_SEED.repoDir);
      await page.getByTestId("form-dialog-submit").click();

      const row = page.locator('[data-testid^="project-row-"]').first();
      await expect(row).toBeVisible({ timeout: 15_000 });
      state.projectId = (await row.getAttribute("data-testid"))?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();
    });

    // ---- 2. Create epic ----
    test("2. create epic", async ({ page }) => {
      expect(state.projectId, "projectId from test 1").toBeTruthy();
      await page.goto(`/projects/${state.projectId}`);

      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.getByTestId("epic-title-input").fill("plan-gate epic");
      await page.getByTestId("epic-description-input").fill("Verify the plan-approval gate.");
      await page.getByTestId("epic-ac-input").fill("Dispatch is blocked before approval.");
      await page.getByTestId("form-dialog-submit").click();

      await page.waitForURL(/\/epics\//, { timeout: 15_000 });
      const epicMatch = page.url().match(/\/epics\/([^/]+)/);
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

    // ---- 3. Start run → premature dispatch is BLOCKED; run halts at ask_user ----
    test("3. premature dispatch is rejected — task stays todo, run awaits approval", async ({
      page,
    }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}`);
      const startBtn = page.getByTestId("start-run-btn");
      await expect(startBtn).toBeVisible({ timeout: 10_000 });
      await startBtn.click();

      await expect(page).toHaveURL(/\/threads\/manager/, { timeout: 15_000 });

      // The plan question appears (ask_user was reached) …
      await expect(page.getByText(PLAN_GATE_QUESTION)).toBeVisible({ timeout: 30_000 });

      // … and the run is parked in awaiting_input rather than running workers.
      await expect
        .poll(() => getRunStatus(page, state.projectId, state.epicId), {
          timeout: 30_000,
          intervals: [500, 1000, 1000],
        })
        .toBe("awaiting_input");

      // THE GATE: the pre-approval dispatch was rejected, so the Worker never ran
      // and T1 is still "todo" (a failed gate would have moved it to in_progress/done).
      const taskStatus = await getTaskStatus(page, state.projectId, state.epicId, "T1");
      expect(taskStatus, "T1 must remain todo before approval (dispatch gate)").toBe("todo");
    });

    // ---- 4. Approve → dispatch now runs the Worker → task done, run completed ----
    test("4. after approval the dispatch runs and the task completes", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/threads/manager`);
      await expect(page.getByText(PLAN_GATE_QUESTION)).toBeVisible({ timeout: 30_000 });

      const composer = page.getByTestId("thread-composer");
      await expect(composer).toBeVisible({ timeout: 10_000 });
      await composer.fill(REPLY_TEXT);

      const sendBtn = page
        .locator("button")
        .filter({ hasText: /send|送信/i })
        .first();
      await sendBtn.click();

      // The run resumes and completes: reply → running → worker/evaluator → completed.
      await expect
        .poll(() => getRunStatus(page, state.projectId, state.epicId), {
          timeout: 90_000,
          intervals: [500, 1000, 2000],
        })
        .toBe("completed");

      // The post-approval dispatch actually ran the Worker → T1 is done.
      await expect
        .poll(() => getTaskStatus(page, state.projectId, state.epicId, "T1"), {
          timeout: 30_000,
          intervals: [500, 1000, 2000],
        })
        .toBe("done");

      await page.screenshot({ path: "test-results/plan-gate.png", fullPage: true });
    });
  });
