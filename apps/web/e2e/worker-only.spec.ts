/**
 * Worker-only dispatch E2E test (P6: task-composition freedom).
 *
 * Purpose:
 *   Verify in a real browser that dispatch(agents=["worker"]) runs ONLY the
 *   Worker — no Evaluator thread is registered, no host commit is made — and
 *   that the Worker's report text is the deliverable: the task becomes done
 *   and the Manager summarises the findings in body text before parking in
 *   "waiting".
 *
 * Verification flow:
 *   1. Create project → create epic → start run
 *   2. Work done: run/state == "waiting" AND T1 done (standard wait)
 *   3. Threads REST: exactly 1 worker thread (resolved, task=T1), ZERO
 *      evaluator threads (agents=["worker"] never starts an Evaluator —
 *      thread registration and EvaluatorStartedEvent share the same code
 *      path, so no thread ⇒ no evaluator event either)
 *   4. Git: the trial branch exists but has NO commits ahead of main
 *      (worker-only never triggers the host commit gate)
 *   5. UI: the agent tree shows the Worker node but no Evaluator node, and
 *      the Manager's summary text is visible in the conversation
 */

import { execFileSync } from "node:child_process";
import { expect, test } from "@playwright/test";
import { waitForWorkDone } from "./wait-helpers";
import { WORKER_ONLY_SEED, WORKER_ONLY_SUMMARY_TEXT } from "./worker-only-seed";

/** Run git in the seeded repo and return trimmed stdout. */
function gitInRepo(args: string[]): string {
  return execFileSync("git", args, { cwd: WORKER_ONLY_SEED.repoDir, stdio: "pipe" })
    .toString()
    .trim();
}

/** The trial branch is the only local branch other than main. */
function findTrialBranch(): string | undefined {
  const branches = gitInRepo(["for-each-ref", "--format=%(refname:short)", "refs/heads"])
    .split("\n")
    .filter(Boolean);
  return branches.find((b) => b !== "main");
}

test.describe
  .serial("Worker-only dispatch — investigation without evaluation", () => {
    const state = { projectId: "", epicId: "" };

    test("1. create project and epic", async ({ page }) => {
      await page.goto("/projects");

      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("project-name-input").fill("worker-only-project");
      await page.getByTestId("repo-path-input-0").fill(WORKER_ONLY_SEED.repoDir);
      await page.getByTestId("form-dialog-submit").click();

      const row = page.locator('[data-testid^="project-row-"]').first();
      await expect(row).toBeVisible({ timeout: 15_000 });
      const testId = await row.getAttribute("data-testid");
      state.projectId = testId?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}`);
      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("epic-title-input").fill("worker-only epic");
      await page
        .getByTestId("epic-description-input")
        .fill("Investigation via worker-only dispatch.");
      await page.getByTestId("epic-ac-input").fill("The report text is the deliverable.");
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

    test("3. work is done — T1 done via worker report alone", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      // Standard work-done wait: dispatch(agents=["worker"]) → Worker report →
      // T1 done → Manager summary text → run parks in "waiting"
      await waitForWorkDone(page, state.projectId, state.epicId, { timeout: 120_000 });

      // The epic stays open (1-bit user-owned status).
      const epicRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}`,
      );
      expect((await epicRes.json()).status).toBe("open");
    });

    test("4. no evaluator thread — worker resolved, task done", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      const threadsRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}/threads`,
      );
      expect(threadsRes.status()).toBe(200);
      const threads = (await threadsRes.json()) as Array<{
        id: string;
        role: string;
        status: string;
        task: string | null;
      }>;
      console.log(`[worker-only] threads = ${JSON.stringify(threads, null, 2)}`);

      // ZERO evaluator threads: agents=["worker"] must never start an Evaluator.
      const evalThreads = threads.filter((t) => t.role === "evaluator");
      expect(
        evalThreads.length,
        `agents=["worker"] must not start an Evaluator, but found ${evalThreads.length} evaluator thread(s)`,
      ).toBe(0);

      // Exactly 1 worker thread, resolved (the accepted-by-report attempt), on T1.
      const workerThreads = threads.filter((t) => t.role === "worker");
      expect(workerThreads.length).toBe(1);
      expect(workerThreads[0].status).toBe("resolved");
      expect(workerThreads[0].task).toBe("T1");

      // Task T1 is done — the report itself was the deliverable.
      const tasksRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}/tasks`,
      );
      const tasks = ((await tasksRes.json()) as { tasks: Array<{ id: string; status: string }> })
        .tasks;
      expect(tasks).toHaveLength(1);
      expect(tasks[0].id).toBe("T1");
      expect(tasks[0].status).toBe("done");
    });

    test("5. git: trial branch has no host commit", async () => {
      // The worker attempt created the trial worktree/branch...
      const trialBranch = findTrialBranch();
      expect(trialBranch, "a trial branch should exist after the worker attempt").toBeTruthy();

      // ...but worker-only never passes the "Evaluator accepted" host-commit
      // gate, so the branch has zero commits ahead of main.
      const aheadCount = gitInRepo(["rev-list", "--count", `main..${trialBranch}`]);
      expect(
        aheadCount,
        `worker-only dispatch must not host-commit, but ${trialBranch} is ${aheadCount} commit(s) ahead of main`,
      ).toBe("0");
    });

    test("6. UI: worker node without evaluator node + manager summary", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/threads/manager`, {
        waitUntil: "domcontentloaded",
      });

      // The Manager's final body-text summary is visible in the conversation.
      await expect(page.getByText(WORKER_ONLY_SUMMARY_TEXT)).toBeVisible({ timeout: 30_000 });

      // Agent tree (hydrated from the threads REST): the Worker node link is
      // present, and no Evaluator node link exists anywhere on the page.
      await expect(page.locator('a[href*="/threads/worker-"]').first()).toBeVisible({
        timeout: 30_000,
      });
      await expect(page.locator('a[href*="/threads/eval-"]')).toHaveCount(0);

      await page.screenshot({ path: "test-results/worker-only.png", fullPage: true });
      console.log("[worker-only] screenshot saved: test-results/worker-only.png");
    });
  });
