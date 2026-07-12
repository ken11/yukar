/**
 * Evaluator-only dispatch E2E test (P6: task-composition freedom).
 *
 * Purpose:
 *   Verify in a real browser that changes drafted by a worker-only dispatch
 *   can be certified LATER by dispatch(agents=["evaluator"]) — and that the
 *   host commit (the single deterministic side-effect gate: "commit only when
 *   the Evaluator accepts") actually happens, verified on the git side.
 *
 * Verification flow:
 *   Phase 1 (turn 0): T1 dispatched with agents=["worker"] — Worker drafts
 *     hello.py, T1 done, run parks in "waiting".
 *     Assert: no evaluator thread, trial branch is 0 commits ahead of main,
 *     hello.py exists in the trial worktree but is NOT committed.
 *   Phase 2 (user reply wakes the run): T2 dispatched with
 *     agents=["evaluator"] — Evaluator accepts the staged worktree contents.
 *     Assert: trial branch is exactly 1 commit ahead of main, the commit
 *     contains hello.py with the drafted content, the evaluator thread is
 *     resolved with the MANAGER as its parent (no worker ran in the attempt).
 */

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import { expect, type Page, test } from "@playwright/test";
import {
  EVALUATOR_ONLY_CERTIFIED_TEXT,
  EVALUATOR_ONLY_DRAFT_TEXT,
  EVALUATOR_ONLY_HELLO_CONTENT,
  EVALUATOR_ONLY_SEED,
} from "./evaluator-only-seed";
import { getRunState, waitForWorkDone } from "./wait-helpers";

const REPLY_TEXT = "Looks good — please certify the draft.";

/** Run git in the seeded repo and return trimmed stdout. */
function gitInRepo(args: string[]): string {
  return execFileSync("git", args, { cwd: EVALUATOR_ONLY_SEED.repoDir, stdio: "pipe" })
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

/** The trial worktree is the linked worktree (any entry other than the repo itself). */
function findTrialWorktree(): string | undefined {
  const repoReal = fs.realpathSync(EVALUATOR_ONLY_SEED.repoDir);
  const entries = gitInRepo(["worktree", "list", "--porcelain"])
    .split("\n")
    .filter((line) => line.startsWith("worktree "))
    .map((line) => line.slice("worktree ".length));
  return entries.find((p) => fs.realpathSync(p) !== repoReal);
}

/** Phase 2 done: run parked in "waiting" AND T2 (created after the reply) is done. */
async function waitForCertificationDone(
  page: Page,
  projectId: string,
  epicId: string,
): Promise<void> {
  await expect
    .poll(
      async () => {
        const s = await getRunState(page, projectId, epicId);
        if (!s.run_id || s.status !== "waiting") return `run:${s.status}`;
        const tRes = await page.request.get(`/api/projects/${projectId}/epics/${epicId}/tasks`);
        const body = (await tRes.json()) as { tasks?: Array<{ id: string; status: string }> };
        const t2 = (body.tasks ?? []).find((t) => t.id === "T2");
        if (!t2) return "no-T2-yet";
        return t2.status === "done" ? "waiting:T2-done" : `T2:${t2.status}`;
      },
      { timeout: 120_000, intervals: [500, 1000, 1000] },
    )
    .toBe("waiting:T2-done");
}

test.describe
  .serial("Evaluator-only dispatch — certify a worker-only draft", () => {
    const state = { projectId: "", epicId: "" };

    test("1. create project and epic", async ({ page }) => {
      await page.goto("/projects");

      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("project-name-input").fill("evaluator-only-project");
      await page.getByTestId("repo-path-input-0").fill(EVALUATOR_ONLY_SEED.repoDir);
      await page.getByTestId("form-dialog-submit").click();

      const row = page.locator('[data-testid^="project-row-"]').first();
      await expect(row).toBeVisible({ timeout: 15_000 });
      const testId = await row.getAttribute("data-testid");
      state.projectId = testId?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}`);
      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("epic-title-input").fill("evaluator-only epic");
      await page
        .getByTestId("epic-description-input")
        .fill("Draft via worker-only, certify via evaluator-only.");
      await page.getByTestId("epic-ac-input").fill("The host commits only on acceptance.");
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

      await expect(page).toHaveURL(/\/threads\//, { timeout: 15_000 });
    });

    test("3. phase 1: worker-only draft — done, nothing committed", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      // Turn 0 ends with T1 done (only task so far) and the run parked.
      await waitForWorkDone(page, state.projectId, state.epicId, { timeout: 120_000 });

      // No evaluator thread exists yet (worker-only never starts one).
      const threadsRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}/threads`,
      );
      const threads = (await threadsRes.json()) as Array<{ role: string; status: string }>;
      expect(threads.filter((t) => t.role === "evaluator")).toHaveLength(0);
      expect(threads.filter((t) => t.role === "worker")).toHaveLength(1);

      // Git side: the trial branch exists but has NO commits ahead of main.
      const trialBranch = findTrialBranch();
      expect(trialBranch, "a trial branch should exist after the worker attempt").toBeTruthy();
      expect(
        gitInRepo(["rev-list", "--count", `main..${trialBranch}`]),
        "worker-only must not host-commit",
      ).toBe("0");

      // The drafted file sits in the trial worktree, uncommitted.
      const worktree = findTrialWorktree();
      expect(worktree, "the trial worktree should exist").toBeTruthy();
      const helloPath = `${worktree}/hello.py`;
      expect(fs.existsSync(helloPath), "hello.py should be drafted in the worktree").toBe(true);
      expect(fs.readFileSync(helloPath, "utf8")).toBe(EVALUATOR_ONLY_HELLO_CONTENT);
      const dirty = execFileSync("git", ["status", "--porcelain"], {
        cwd: worktree as string,
        stdio: "pipe",
      })
        .toString()
        .trim();
      expect(dirty, "the draft must remain uncommitted after worker-only").toContain("hello.py");
    });

    test("4. phase 2: reply → evaluator-only certification → host commit", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/threads/manager`);

      // The phase-1 draft report is visible; the composer accepts the reply.
      await expect(page.getByText(EVALUATOR_ONLY_DRAFT_TEXT)).toBeVisible({ timeout: 30_000 });
      const composer = page.getByTestId("thread-composer");
      await expect(composer).toBeVisible({ timeout: 10_000 });
      await composer.fill(REPLY_TEXT);
      const sendBtn = page
        .locator("button")
        .filter({ hasText: /send|送信/i })
        .first();
      await sendBtn.click();

      // The reply wakes the parked run: task_update(T2) → dispatch(agents=
      // ["evaluator"]) → accept → host commit → report → park in "waiting".
      await waitForCertificationDone(page, state.projectId, state.epicId);

      // The certified report ends the turn.
      await expect(page.getByText(EVALUATOR_ONLY_CERTIFIED_TEXT)).toBeVisible({
        timeout: 30_000,
      });
    });

    test("5. git: host commit exists with the drafted content", async () => {
      const trialBranch = findTrialBranch();
      expect(trialBranch).toBeTruthy();

      // Exactly ONE commit ahead of main — made by the host on acceptance.
      expect(
        gitInRepo(["rev-list", "--count", `main..${trialBranch}`]),
        "acceptance must produce exactly one host commit",
      ).toBe("1");

      // The commit contains hello.py …
      const committedFiles = gitInRepo([
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        trialBranch as string,
      ]);
      expect(committedFiles.split("\n")).toContain("hello.py");

      // … with exactly the drafted content.
      const committedContent = execFileSync("git", ["show", `${trialBranch}:hello.py`], {
        cwd: EVALUATOR_ONLY_SEED.repoDir,
        stdio: "pipe",
      }).toString();
      expect(committedContent).toBe(EVALUATOR_ONLY_HELLO_CONTENT);
    });

    test("6. threads: evaluator resolved with manager as parent", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      const threadsRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}/threads`,
      );
      const threads = (await threadsRes.json()) as Array<{
        id: string;
        role: string;
        status: string;
        task: string | null;
        parent_thread_id: string | null;
      }>;
      console.log(`[evaluator-only] threads = ${JSON.stringify(threads, null, 2)}`);

      // One worker thread (phase-1 draft, T1) and one evaluator thread
      // (phase-2 certification, T2) — both resolved.
      const workers = threads.filter((t) => t.role === "worker");
      expect(workers).toHaveLength(1);
      expect(workers[0].status).toBe("resolved");
      expect(workers[0].task).toBe("T1");

      const evals = threads.filter((t) => t.role === "evaluator");
      expect(evals).toHaveLength(1);
      expect(evals[0].status).toBe("resolved");
      expect(evals[0].task).toBe("T2");

      // No Worker ran in the evaluator-only attempt, so the evaluator hangs
      // directly off the manager conversation, not off a worker.
      const manager = threads.find((t) => t.role === "manager");
      expect(manager).toBeTruthy();
      expect(evals[0].parent_thread_id).toBe(manager?.id);

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/threads/manager`, {
        waitUntil: "domcontentloaded",
      });
      await expect(page.locator('[data-testid="agent-message"]').first()).toBeVisible({
        timeout: 30_000,
      });
      await page.screenshot({ path: "test-results/evaluator-only.png", fullPage: true });
      console.log("[evaluator-only] screenshot saved: test-results/evaluator-only.png");
    });
  });
