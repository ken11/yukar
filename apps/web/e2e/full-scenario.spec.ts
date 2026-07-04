/**
 * Full-scenario E2E (fake provider, plan-approval gate ON).
 *
 * Exercises the product's core loop end to end, then the two features built on
 * top of it. Nothing is stubbed beyond the LLM: real FastAPI, real git worktrees,
 * real Next.js UI, real SSE.
 *
 * Block 1 — Same-trial new session (also the basic-scenario regression):
 *   basic HITL (plan → user revises → re-plan → user approves → dispatch →
 *   evaluate → manager self-check → in_review) → user merges → user starts a NEW
 *   session on the SAME trial (continue-on-branch) with an extra request → the
 *   new session's Manager re-runs the basic scenario on the same branch.
 *
 * Block 2 — Reviewer:
 *   basic HITL → in_review → user invokes the Reviewer → the Reviewer reads the
 *   branch (read_branch_diff + fs_read on the manager trial's worktree) and
 *   reports to the user via ask_user, WITHOUT changing the epic lifecycle → user
 *   reviews → user can instruct a fix on the Manager thread (composer available)
 *   → user merges.
 */

import { expect, type Page, test } from "@playwright/test";
import { FULL_SCENARIO_SEED, Q_PLAN, Q_REVIEW, Q_REVISED } from "./full-scenario-seed";

const SHOTS = "playwright-report/full-scenario";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function getEpic(
  page: Page,
  projectId: string,
  epicId: string,
): Promise<{ status: string; branch?: string; active_thread_id?: string | null }> {
  const res = await page.request.get(`/api/projects/${projectId}/epics/${epicId}`);
  return res.json();
}

async function getRunStatus(page: Page, projectId: string, epicId: string): Promise<string> {
  const res = await page.request.get(`/api/projects/${projectId}/epics/${epicId}/run/state`);
  return (await res.json()).status as string;
}

async function getTaskStatus(
  page: Page,
  projectId: string,
  epicId: string,
  taskId: string,
): Promise<string | undefined> {
  const res = await page.request.get(`/api/projects/${projectId}/epics/${epicId}/tasks`);
  if (!res.ok()) return undefined;
  const body = await res.json();
  return (body.tasks ?? []).find((t: { id: string }) => t.id === taskId)?.status;
}

async function createProjectAndEpic(
  page: Page,
  projectName: string,
  repoDir: string,
  epicTitle: string,
): Promise<{ projectId: string; epicId: string }> {
  await page.goto("/projects");
  await page.getByTestId("new-project-btn").click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByTestId("project-name-input").fill(projectName);
  await page.getByTestId("repo-path-input-0").fill(repoDir);
  await page.getByTestId("form-dialog-submit").click();

  const row = page.locator('[data-testid^="project-row-"]').filter({ hasText: projectName });
  await expect(row).toBeVisible({ timeout: 15_000 });
  const projectId = (await row.getAttribute("data-testid"))?.replace("project-row-", "") ?? "";
  expect(projectId).toBeTruthy();

  await page.goto(`/projects/${projectId}`);
  await page.getByTestId("new-epic-btn").first().click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByTestId("epic-title-input").fill(epicTitle);
  await page.getByTestId("epic-description-input").fill("Create hello.py that prints hello.");
  await page.getByTestId("epic-ac-input").fill("hello.py exists and prints 'hello'");
  await page.getByTestId("form-dialog-submit").click();

  await page.waitForURL(/\/projects\/[^/]+\/epics\/[^/]+/, { timeout: 15_000 });
  const epicId = page.url().match(/\/epics\/([^/?#]+)/)?.[1] ?? "";
  expect(epicId).toBeTruthy();
  return { projectId, epicId };
}

/** Fill the thread composer and send (⌘/Ctrl-independent: click the send button). */
async function replyOnThread(page: Page, replyText: string): Promise<void> {
  const composer = page.getByTestId("thread-composer");
  await expect(composer).toBeVisible({ timeout: 20_000 });
  await composer.fill(replyText);
  await page
    .locator("button")
    .filter({ hasText: /send|送信/i })
    .first()
    .click();
}

/**
 * Drive the Manager HITL dance from the plan question to in_review, assuming the
 * Manager has already started and the page is on its thread: it presents a plan
 * (Q_PLAN), the user requests a revision, it re-plans (Q_REVISED), the user
 * approves, and only then does the gated dispatch run through to in_review.
 */
async function approvePlanToInReview(page: Page, projectId: string, epicId: string): Promise<void> {
  await expect(page.getByText(Q_PLAN)).toBeVisible({ timeout: 60_000 });
  // The approval gate: before approval no Worker runs — T1 stays "todo".
  await expect
    .poll(() => getRunStatus(page, projectId, epicId), { timeout: 30_000, intervals: [500, 1000] })
    .toBe("awaiting_input");
  expect(await getTaskStatus(page, projectId, epicId, "T1"), "T1 stays todo pre-approval").toBe(
    "todo",
  );

  await replyOnThread(page, "テストの観点も計画に含めてください。");
  await expect(page.getByText(Q_REVISED)).toBeVisible({ timeout: 60_000 });

  await replyOnThread(page, "はい、その計画で承認します。進めてください。");
  await expect
    .poll(() => getEpicStatus(page, projectId, epicId), {
      timeout: 90_000,
      intervals: [500, 1000, 2000],
    })
    .toBe("in_review");
}

async function getEpicStatus(page: Page, projectId: string, epicId: string): Promise<string> {
  return (await getEpic(page, projectId, epicId)).status;
}

async function mergeEpic(page: Page, projectId: string, epicId: string): Promise<void> {
  const res = await page.request.post(`/api/projects/${projectId}/epics/${epicId}/git/merge`, {
    data: { repo: "myrepo", message: "Merge epic" },
  });
  expect(res.ok(), `merge should succeed: ${res.status()} ${await res.text()}`).toBeTruthy();
  await expect
    .poll(() => getEpicStatus(page, projectId, epicId), { timeout: 30_000, intervals: [500, 1000] })
    .toBe("merged");
}

// ===========================================================================
// Block 1 — Same-trial new session (basic scenario regression + continuation)
// ===========================================================================

test.describe
  .serial("same-trial new session (basic HITL → merge → continue on branch)", () => {
    const s = {
      projectId: "",
      epicId: "",
      managerThreadId: "manager",
      branch: "",
      trialId: "",
      contId: "",
    };

    test("basic scenario: plan → revise → approve → implement → in_review", async ({ page }) => {
      const ids = await createProjectAndEpic(
        page,
        "same-trial-project",
        FULL_SCENARIO_SEED.repoDirs.sameTrial,
        "Same-trial epic",
      );
      s.projectId = ids.projectId;
      s.epicId = ids.epicId;

      await page.getByTestId("start-run-btn").click();
      await expect(page).toHaveURL(/\/threads\/manager/, { timeout: 15_000 });

      await approvePlanToInReview(page, s.projectId, s.epicId);

      // The Worker actually implemented + Evaluator accepted → T1 done, and the
      // branch carries hello.py (the Manager self-checked it via read_branch_diff).
      expect(await getTaskStatus(page, s.projectId, s.epicId, "T1")).toBe("done");
      const epic = await getEpic(page, s.projectId, s.epicId);
      s.branch = epic.branch ?? "";
      expect(s.branch, "epic has a branch").toBeTruthy();

      // Capture the trial id for the continuation invariant.
      const tRes = await page.request.get(`/api/projects/${s.projectId}/epics/${s.epicId}/threads`);
      const threads: Array<{ id: string; role: string; trial_id?: string | null }> =
        await tRes.json();
      const mgr = threads.find((t) => t.role === "manager");
      s.managerThreadId = mgr?.id ?? "manager";
      s.trialId = mgr?.trial_id ?? s.managerThreadId;
    });

    test("user merges the epic", async ({ page }) => {
      expect(s.epicId).toBeTruthy();
      await mergeEpic(page, s.projectId, s.epicId);
    });

    test("continue on the same branch: a new session re-runs the basic scenario", async ({
      page,
    }) => {
      expect(s.epicId).toBeTruthy();

      // Start a fresh conversation on the SAME trial (branch + worktree kept).
      await page.goto(`/projects/${s.projectId}/epics/${s.epicId}/threads/${s.managerThreadId}`);
      const threadsNav = page.locator('nav[aria-label="Threads"]');
      await expect(threadsNav).toBeVisible({ timeout: 10_000 });
      await threadsNav.getByTestId("continue-branch-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible({ timeout: 10_000 });
      await page.getByTestId("trial-title-input").fill("追加対応");

      const [createResp] = await Promise.all([
        page.waitForResponse(
          (r) => r.url().includes("/threads") && r.request().method() === "POST",
          { timeout: 15_000 },
        ),
        page.getByTestId("form-dialog-submit").click(),
      ]);
      expect(createResp.status(), "continuation thread created (201)").toBe(201);
      const cont = await createResp.json();
      s.contId = cont.id;

      // Same trial: new conversation id, SAME branch + trial_id (shared worktree).
      expect(cont.id).not.toBe(s.managerThreadId);
      expect(cont.branch, "continuation keeps the same branch").toBe(s.branch);
      expect(cont.trial_id, "continuation shares the trial id").toBe(s.trialId);

      await page.waitForURL(new RegExp(`/threads/${s.contId}`), { timeout: 15_000 });

      // Post the additional request → the NEW session's Manager runs the basic
      // scenario again on the same branch.
      await replyOnThread(page, "util.py も追加してください。");
      await approvePlanToInReview(page, s.projectId, s.epicId);

      // The epic lifecycle reopened and completed on the SAME branch; the active
      // trial is now the continuation conversation.
      const epic = await getEpic(page, s.projectId, s.epicId);
      expect(epic.branch, "still the same branch — no new trial").toBe(s.branch);
      expect(epic.active_thread_id, "active session is the continuation").toBe(s.contId);

      await page.screenshot({ path: `${SHOTS}/same-trial-continuation.png`, fullPage: true });
    });
  });

// ===========================================================================
// Block 2 — Reviewer
// ===========================================================================

test.describe
  .serial("reviewer (basic HITL → review → report → fix-entry + merge)", () => {
    const s = { projectId: "", epicId: "", reviewerId: "" };

    test("basic scenario to in_review", async ({ page }) => {
      const ids = await createProjectAndEpic(
        page,
        "reviewer-project",
        FULL_SCENARIO_SEED.repoDirs.reviewer,
        "Reviewer epic",
      );
      s.projectId = ids.projectId;
      s.epicId = ids.epicId;

      await page.getByTestId("start-run-btn").click();
      await expect(page).toHaveURL(/\/threads\/manager/, { timeout: 15_000 });
      await approvePlanToInReview(page, s.projectId, s.epicId);
    });

    test("user invokes the Reviewer → it reviews and reports, epic untouched", async ({ page }) => {
      expect(s.epicId).toBeTruthy();
      await page.goto(`/projects/${s.projectId}/epics/${s.epicId}/threads/manager`);
      await expect(page.getByTestId("start-review-btn")).toBeVisible({ timeout: 15_000 });

      const [reviewResp] = await Promise.all([
        page.waitForResponse(
          (r) => r.url().endsWith("/review") && r.request().method() === "POST",
          { timeout: 20_000 },
        ),
        page.getByTestId("start-review-btn").click(),
      ]);
      expect(reviewResp.status(), "POST /review 201").toBe(201);
      const reviewer = await reviewResp.json();
      s.reviewerId = reviewer.id;
      expect(reviewer.role).toBe("reviewer");

      await page.waitForURL(new RegExp(`/threads/${s.reviewerId}`), { timeout: 15_000 });

      // The Reviewer reports to the user (ask_user) → parks at awaiting_input.
      await expect(page.getByText(Q_REVIEW)).toBeVisible({ timeout: 60_000 });

      // It read the branch first-hand: fs_read on the manager trial's worktree
      // returned hello.py ("greet"), and it did NOT touch the epic lifecycle.
      const mRes = await page.request.get(
        `/api/projects/${s.projectId}/epics/${s.epicId}/threads/${s.reviewerId}`,
      );
      expect(JSON.stringify(await mRes.json())).toContain("greet");
      const epic = await getEpic(page, s.projectId, s.epicId);
      expect(epic.status, "reviewer leaves the epic in_review").toBe("in_review");
      expect(epic.active_thread_id, "manager trial stays the active trial").toBe("manager");

      await page.screenshot({ path: `${SHOTS}/reviewer-report.png`, fullPage: true });
    });

    test("user reviews, can instruct a fix on the Manager thread, then merges", async ({
      page,
    }) => {
      expect(s.reviewerId).toBeTruthy();

      // User acknowledges the report → the reviewer run wraps up.
      await page.goto(`/projects/${s.projectId}/epics/${s.epicId}/threads/${s.reviewerId}`);
      await replyOnThread(page, "確認しました。ありがとうございました。");
      await expect
        .poll(() => getRunStatus(page, s.projectId, s.epicId), {
          timeout: 60_000,
          intervals: [500, 1000, 2000],
        })
        .toBe("completed");

      // Path "issue found": the user can instruct a fix on the Manager thread —
      // its composer is available (the reviewer run did not hijack the trial).
      await page.goto(`/projects/${s.projectId}/epics/${s.epicId}/threads/manager`);
      await expect(
        page.getByTestId("thread-composer"),
        "Manager thread stays writable for a fix instruction",
      ).toBeVisible({ timeout: 20_000 });

      // Path "no issue": the user merges.
      await mergeEpic(page, s.projectId, s.epicId);
    });
  });
