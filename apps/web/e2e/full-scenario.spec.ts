/**
 * Full-scenario E2E (fake provider, plan-approval gate ON).
 *
 * Exercises the product's core loop end to end, then the two features built on
 * top of it. Nothing is stubbed beyond the LLM: real FastAPI, real git worktrees,
 * real Next.js UI, real SSE.
 *
 * Block 1 — Same-trial new session (also the basic-scenario regression):
 *   basic HITL (plan → user revises → re-plan → user approves via the explicit
 *   approve-plan operation (snapshot-hash bound; a chat reply alone does
 *   not approve) → gated dispatch → evaluate → manager self-check → run parks
 *   in "waiting" with all tasks done; the epic stays open) → user merges (merge fact recorded, epic
 *   still open) → user starts a NEW session on the SAME trial
 *   (continue-on-branch) with an extra request → the new session's Manager
 *   re-runs the basic scenario on the same branch; its re-plan reproduces the
 *   already-approved snapshot, so the recorded approval still matches and a
 *   plain reply (not a re-approval) wakes it into the allowed dispatch.
 *
 * Block 2 — Reviewer:
 *   basic HITL → work done → user invokes the Reviewer → the Reviewer reads
 *   the branch (read_branch_diff + fs_read on the manager trial's worktree) and
 *   reports to the user in BODY TEXT (its turn end parks the run in "waiting"),
 *   WITHOUT changing the epic lifecycle → user reviews → user can instruct a
 *   fix on the Manager thread (composer available) → user merges.
 */

import { expect, type Page, test } from "@playwright/test";
import { FULL_SCENARIO_SEED, Q_PLAN, Q_REVIEW, Q_REVISED } from "./full-scenario-seed";
import { getRunState, waitForRunWaiting, waitForWorkDone } from "./wait-helpers";

const SHOTS = "playwright-report/full-scenario";

// Your-turn banner wordings (ja locale). The neutral thread/epic texts are the
// same string; the Reviewer variants (role attribution) differ per surface.
const NEUTRAL_TURN_BANNER = "あなたの番です — 返信するとエージェントが続けます";
const REVIEWER_TURN_BANNER = "Reviewer の報告があります — 返信すると続けます";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function getEpic(
  page: Page,
  projectId: string,
  epicId: string,
): Promise<{
  status: string;
  branch?: string;
  active_thread_id?: string | null;
  merged_at?: string | null;
}> {
  const res = await page.request.get(`/api/projects/${projectId}/epics/${epicId}`);
  return res.json();
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

async function getPlanApproved(page: Page, projectId: string, epicId: string): Promise<boolean> {
  const res = await page.request.get(`/api/projects/${projectId}/epics/${epicId}/tasks`);
  const body = await res.json();
  return Boolean(body.plan_approved);
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
 * Drive the Manager HITL dance from the plan question to work done,
 * assuming the Manager has already started and the page is on its thread: it
 * presents a plan (Q_PLAN, body text — the turn end parks the run in
 * "waiting"), the user requests a revision (a plain reply — it does NOT
 * approve), it re-plans (Q_REVISED), the user approves via the explicit
 * approve-plan operation (snapshot-hash bound), and only then does the
 * gated dispatch run through; the run parks in "waiting" with every task done
 * (a conversation run never "completes"; the epic itself stays open —
 * finishing work never transitions the 1-bit epic status).
 *
 * `alreadyApproved` covers the continuation session: its re-plan reproduces a
 * snapshot identical to the one the user already approved, so the recorded
 * hash still matches — no approve button is offered and a plain reply wakes
 * the agent into the (allowed) dispatch.
 */
async function approvePlanToWorkDone(
  page: Page,
  projectId: string,
  epicId: string,
  opts: { alreadyApproved?: boolean } = {},
): Promise<void> {
  await expect(page.getByText(Q_PLAN)).toBeVisible({ timeout: 60_000 });
  // The approval gate: before approval no Worker runs — T1 stays "todo".
  await waitForRunWaiting(page, projectId, epicId, { timeout: 30_000 });
  expect(await getTaskStatus(page, projectId, epicId, "T1"), "T1 stays todo pre-approval").toBe(
    "todo",
  );

  await replyOnThread(page, "テストの観点も計画に含めてください。");
  // Q_REVISED appearing is the deterministic marker that the woken turn has
  // ended (plain "waiting" polling would match the previous park); the approve
  // banner (if any) has had its plan snapshot refreshed by the re-plan's
  // task_update event.
  await expect(page.getByText(Q_REVISED)).toBeVisible({ timeout: 60_000 });
  await waitForRunWaiting(page, projectId, epicId, { timeout: 30_000 });

  if (opts.alreadyApproved) {
    // Snapshot identity: the re-plan reproduced the approved plan, so the
    // stored approval still matches — no re-approval is asked of the user.
    expect(
      await getPlanApproved(page, projectId, epicId),
      "identical snapshot stays approved",
    ).toBe(true);
    await expect(page.getByTestId("approve-plan-btn")).toHaveCount(0);
    // A plain reply merely wakes the parked agent; the gate is already open.
    await replyOnThread(page, "はい、その計画で進めてください。");
  } else {
    // A chat reply alone did NOT approve the plan (the revision reply above
    // left it unapproved) — approval is the explicit operation.
    expect(
      await getPlanApproved(page, projectId, epicId),
      "plan stays unapproved until the explicit operation",
    ).toBe(false);
    const approveBtn = page.getByTestId("approve-plan-btn");
    await expect(approveBtn).toBeVisible({ timeout: 15_000 });
    // One click records the approval AND auto-posts the "plan approved"
    // message that wakes the parked agent into the now-allowed dispatch.
    await approveBtn.click();
  }

  // Standard work-done wait: the gated dispatch runs the Worker/Evaluator and
  // the Manager's final report parks the run in "waiting" with T1 done.
  await waitForWorkDone(page, projectId, epicId);
}

async function mergeEpic(page: Page, projectId: string, epicId: string): Promise<void> {
  const res = await page.request.post(`/api/projects/${projectId}/epics/${epicId}/git/merge`, {
    data: { repo: "myrepo", message: "Merge epic" },
  });
  expect(res.ok(), `merge should succeed: ${res.status()} ${await res.text()}`).toBeTruthy();
  // Merging records a fact attribute (merged_at); the epic stays open.
  await expect
    .poll(async () => Boolean((await getEpic(page, projectId, epicId)).merged_at), {
      timeout: 30_000,
      intervals: [500, 1000],
    })
    .toBe(true);
  expect((await getEpic(page, projectId, epicId)).status, "merge leaves the epic open").toBe(
    "open",
  );
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

    test("basic scenario: plan → revise → approve → implement → work done", async ({ page }) => {
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

      await approvePlanToWorkDone(page, s.projectId, s.epicId);

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
      // The thread list is persistent in the desktop sidebar.
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
      // scenario again on the same branch. Its re-plan reproduces the snapshot
      // approved in the first session, so no re-approval is needed (
      // approval is bound to the plan snapshot, not to a run or a session).
      await replyOnThread(page, "util.py も追加してください。");
      await approvePlanToWorkDone(page, s.projectId, s.epicId, { alreadyApproved: true });

      // The continuation ran on the SAME branch (the epic stayed open the whole
      // time); the active trial is now the continuation conversation.
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

    test("basic scenario to work done", async ({ page }) => {
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
      await approvePlanToWorkDone(page, s.projectId, s.epicId);
    });

    test("user invokes the Reviewer → it reviews and reports, epic untouched", async ({ page }) => {
      expect(s.epicId).toBeTruthy();
      await page.goto(`/projects/${s.projectId}/epics/${s.epicId}/threads/manager`);
      // Ask Reviewer is inline in the desktop sidebar; it renders once the
      // controls settle into an idle branch (readiness wait).
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

      // The Reviewer reports to the user in body text → its turn end parks the
      // run in "waiting", with the reviewer thread driving the run.
      // The attribution split root-fixed the live attribution (activeTrialId vs currentRun), so
      // the SPA session we arrived on shows the report streaming in live —
      // NO reload (this assertion was previously downgraded to a fresh load).
      await expect(
        page.getByText(Q_REVIEW),
        "Reviewer report streams live into the reviewer thread (no reload)",
      ).toBeVisible({ timeout: 60_000 });
      await expect
        .poll(
          async () => {
            const st = await getRunState(page, s.projectId, s.epicId);
            return `${st.status}:${st.thread_id ?? ""}`;
          },
          { timeout: 60_000, intervals: [500, 1000] },
        )
        .toBe(`waiting:${s.reviewerId}`);

      // The your-turn state appears on the reviewer thread's composer and
      // names the Reviewer (RunState.role); the header chip shows the passive
      // your-turn state (the epic-level banner was removed — one voice per state).
      await expect(
        page.getByText(REVIEWER_TURN_BANNER),
        "Composer state names the Reviewer",
      ).toBeVisible({ timeout: 30_000 });
      await expect(
        page.getByText("あなたの番", { exact: true }),
        "Header status chip shows the your-turn state",
      ).toBeVisible({ timeout: 15_000 });

      // It read the branch first-hand: fs_read on the manager trial's worktree
      // returned hello.py ("greet"), and it did NOT touch the epic lifecycle.
      const mRes = await page.request.get(
        `/api/projects/${s.projectId}/epics/${s.epicId}/threads/${s.reviewerId}`,
      );
      expect(JSON.stringify(await mRes.json())).toContain("greet");
      const epic = await getEpic(page, s.projectId, s.epicId);
      expect(epic.status, "reviewer leaves the epic open").toBe("open");
      expect(epic.active_thread_id, "manager trial stays the active trial").toBe("manager");

      await page.screenshot({ path: `${SHOTS}/reviewer-report.png`, fullPage: true });
    });

    test("user reviews, can instruct a fix on the Manager thread, then merges", async ({
      page,
    }) => {
      expect(s.reviewerId).toBeTruthy();

      // User acknowledges the report → the reviewer wraps up in body text and
      // its run parks in "waiting" again (a conversation never ends).
      await page.goto(`/projects/${s.projectId}/epics/${s.epicId}/threads/${s.reviewerId}`);
      await replyOnThread(page, "確認しました。ありがとうございました。");
      await expect(page.getByText("承知しました。ご確認ありがとうございました。")).toBeVisible({
        timeout: 60_000,
      });
      await waitForRunWaiting(page, s.projectId, s.epicId, { timeout: 30_000 });

      // Path "issue found": the user can instruct a fix on the Manager thread —
      // its composer is available (the reviewer run did not hijack the trial).
      await page.goto(`/projects/${s.projectId}/epics/${s.epicId}/threads/manager`);
      await expect(
        page.getByTestId("thread-composer"),
        "Manager thread stays writable for a fix instruction",
      ).toBeVisible({ timeout: 20_000 });

      // Reload-misattribution regression guard: the reviewer run is
      // parked in waiting, so a fresh load of the TRIAL thread must NOT show
      // any your-turn wording on the trial itself — the parked marker belongs
      // to the reviewer conversation. The header chip stays (role-agnostic).
      await expect(
        page.getByText("あなたの番", { exact: true }),
        "Header status chip shows the your-turn state on the trial page",
      ).toBeVisible({ timeout: 15_000 });
      await expect(
        page.getByText(NEUTRAL_TURN_BANNER),
        "No neutral your-turn banner misattributed to the Trial thread",
      ).toHaveCount(0);
      await expect(
        page.getByText(REVIEWER_TURN_BANNER),
        "No reviewer reply banner on the Trial thread",
      ).toHaveCount(0);

      // Path "no issue": the user merges.
      await mergeEpic(page, s.projectId, s.epicId);
    });
  });
