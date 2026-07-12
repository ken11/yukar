/**
 * Plan-approval-gate E2E test (snapshot-bound approval).
 *
 * Proves the host-enforced approval gate in a real browser + real backend:
 *   - The Manager tries to `dispatch` BEFORE the user approves the plan. The
 *     host REJECTS it, so no Worker runs and the task stays "todo" — the
 *     Manager presents the plan in body text and the run parks in "waiting"
 *     instead of running away.
 *   - Approval is an EXPLICIT user operation: clicking approve-plan-btn records
 *     the approval (POST /plan/approval, bound to the plan-snapshot hash) and
 *     auto-posts the i18n "plan approved" user message, which wakes the parked
 *     agent. A chat reply alone would NOT open the gate.
 *   - Re-approval after a plan change: the woken Manager edits the plan
 *     (task_update re-title) → the snapshot hash changes → the recorded
 *     approval is stale → the next dispatch is rejected AGAIN and the user must
 *     re-approve the updated plan.
 *   - After the second approval the dispatch runs the Worker, the task reaches
 *     "done", and the run parks in "waiting" (work done — a conversation run
 *     never "completes").
 *
 * The gate is proved deterministically via the tasks API: `run_dispatch` marks a
 * task in_progress before running the Worker, so a blocked dispatch leaves T1
 * "todo". Everything waits on expect.poll — no fixed sleeps. retries:0/workers:1.
 */

import { expect, test } from "@playwright/test";
import ja from "../locales/ja";
import { PLAN_GATE_QUESTION, PLAN_GATE_REVISED_QUESTION, PLAN_GATE_SEED } from "./plan-gate-seed";
import { waitForRunWaiting, waitForWorkDone } from "./wait-helpers";

/** ja is the app's default locale — the exact message the approve button
 * auto-posts. Imported from the locale so a wording change cannot silently
 * desynchronise this spec. */
const APPROVAL_MESSAGE = ja.conversation.planApprovedMessage;

interface TasksApproval {
  plan_hash: string;
  approved_hash: string | null;
  plan_approved: boolean;
  taskStatus?: string;
}

test.describe
  .serial("plan-gate: dispatch is blocked until the user approves the plan snapshot", () => {
    const state = { projectId: "", epicId: "" };

    async function getTasksApproval(
      page: import("@playwright/test").Page,
      projectId: string,
      epicId: string,
      taskId: string,
    ): Promise<TasksApproval | undefined> {
      const res = await page.request.get(`/api/projects/${projectId}/epics/${epicId}/tasks`);
      if (!res.ok()) return undefined;
      const body = await res.json();
      const task = (body.tasks ?? []).find((t: { id: string }) => t.id === taskId);
      return {
        plan_hash: body.plan_hash,
        approved_hash: body.approved_hash ?? null,
        plan_approved: Boolean(body.plan_approved),
        taskStatus: task?.status as string | undefined,
      };
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

    // ---- 3. Start run → premature dispatch is BLOCKED; run parks unapproved ----
    test("3. premature dispatch is rejected — task stays todo, plan unapproved, approve button offered", async ({
      page,
    }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}`);
      const startBtn = page.getByTestId("start-run-btn");
      await expect(startBtn).toBeVisible({ timeout: 10_000 });
      await startBtn.click();

      await expect(page).toHaveURL(/\/threads\/manager/, { timeout: 15_000 });

      // The plan question appears (the Manager's body-text plan presentation) …
      await expect(page.getByText(PLAN_GATE_QUESTION)).toBeVisible({ timeout: 30_000 });

      // … and the run is parked in "waiting" rather than running workers.
      await waitForRunWaiting(page, state.projectId, state.epicId, { timeout: 30_000 });

      // THE GATE: the pre-approval dispatch was rejected, so the Worker never ran
      // and T1 is still "todo" (a failed gate would have moved it to in_progress/done).
      const approval = await getTasksApproval(page, state.projectId, state.epicId, "T1");
      expect(approval?.taskStatus, "T1 must remain todo before approval (dispatch gate)").toBe(
        "todo",
      );
      expect(approval?.plan_approved, "no approval is recorded yet").toBe(false);
      expect(approval?.approved_hash, "no approval hash on disk yet").toBeNull();

      // The explicit approval operation is offered in the conversation UI.
      await expect(page.getByTestId("approve-plan-btn")).toBeVisible({ timeout: 15_000 });
    });

    // ---- 4. First approval wakes the agent; its re-plan STALES the approval ----
    test("4. approving posts the approval message and wakes the agent; a task_update re-stales the gate", async ({
      page,
    }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/threads/manager`);
      await expect(page.getByText(PLAN_GATE_QUESTION)).toBeVisible({ timeout: 30_000 });

      const approveBtn = page.getByTestId("approve-plan-btn");
      await expect(approveBtn).toBeVisible({ timeout: 15_000 });
      await approveBtn.click();

      // One click = record the approval AND auto-post the user message that
      // wakes the parked agent — the operation leaves a trace in the thread.
      await expect(page.getByText(APPROVAL_MESSAGE)).toBeVisible({ timeout: 30_000 });

      // The woken Manager re-titles T1 (plan snapshot changes → hash changes),
      // tries to dispatch — REJECTED again (stale approval) — and re-asks.
      // The revised-question text is the deterministic marker that the woken
      // turn has ended (plain "waiting" polling would match the previous park).
      await expect(page.getByText(PLAN_GATE_REVISED_QUESTION)).toBeVisible({ timeout: 60_000 });
      await waitForRunWaiting(page, state.projectId, state.epicId, { timeout: 30_000 });

      // Snapshot binding: an approval IS recorded (approved_hash non-null) but
      // it no longer matches the changed plan → unapproved again, T1 still todo.
      const approval = await getTasksApproval(page, state.projectId, state.epicId, "T1");
      expect(approval?.approved_hash, "the first approval is on disk").toBeTruthy();
      expect(approval?.plan_approved, "the recorded approval must not match the CHANGED plan").toBe(
        false,
      );
      expect(approval?.approved_hash).not.toBe(approval?.plan_hash);
      expect(approval?.taskStatus, "T1 must still be todo after the stale-approval dispatch").toBe(
        "todo",
      );

      // The re-approval operation is offered again, live on the same page
      // (the task_update SSE event refreshed the plan snapshot in the cache).
      await expect(approveBtn).toBeVisible({ timeout: 15_000 });
    });

    // ---- 5. Second approval → dispatch runs the Worker → task done, run parks ----
    test("5. after re-approval the dispatch runs and the task completes", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/threads/manager`);
      await expect(page.getByText(PLAN_GATE_REVISED_QUESTION)).toBeVisible({ timeout: 30_000 });

      const approveBtn = page.getByTestId("approve-plan-btn");
      await expect(approveBtn).toBeVisible({ timeout: 15_000 });
      await approveBtn.click();

      // The run resumes and finishes the work: approval → running →
      // worker/evaluator → the run parks in "waiting" with every task done.
      await waitForWorkDone(page, state.projectId, state.epicId);

      // The post-approval dispatch actually ran the Worker → T1 is done, and
      // the recorded approval matches the (unchanged) final plan snapshot.
      await expect
        .poll(
          async () =>
            (await getTasksApproval(page, state.projectId, state.epicId, "T1"))?.taskStatus,
          { timeout: 30_000, intervals: [500, 1000, 2000] },
        )
        .toBe("done");
      const approval = await getTasksApproval(page, state.projectId, state.epicId, "T1");
      expect(approval?.plan_approved, "the final plan snapshot stays approved").toBe(true);

      await page.screenshot({ path: "test-results/plan-gate.png", fullPage: true });
    });
  });
