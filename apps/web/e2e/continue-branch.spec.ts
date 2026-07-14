/**
 * "Continue on Branch" E2E smoke — real-device verification of the same-branch,
 * fresh-context Manager session (Phase 1 of the trial/session decoupling).
 *
 * Unlike "New Trial" (which forks a new branch), "Continue on Branch" keeps the
 * current trial's branch + worktree and starts a fresh Manager conversation.
 *
 * Verification items:
 *   1. setup: create project + epic → fake run until work done (run parks in waiting)
 *   2. Click "Continue on Branch" → navigate to the new thread id
 *   3. New session is writable (composer visible, no readonly banner) and empty
 *   4. API: previous conversation archived / new session active / SAME branch /
 *      new session shares the trial_id / epic.active_thread_id + epic.branch
 */

import { expect, test } from "@playwright/test";
import { SEED } from "./seed";
import { waitForWorkDone } from "./wait-helpers";

const SHOTS = "playwright-report";

test.describe
  .serial("continue-on-branch fake smoke", () => {
    const state = {
      projectId: "",
      epicId: "",
      firstManagerId: "",
      firstBranch: "",
      firstTrialId: "",
      contId: "",
    };

    test("setup: create project + epic, run until work is done", async ({ page }) => {
      await page.goto("/projects");
      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.getByTestId("project-name-input").fill("continue-branch-project");
      await page.getByTestId("repo-path-input-0").fill(SEED.repoDirs.continueBranch);
      await page.getByTestId("form-dialog-submit").click();

      const row = page
        .locator('[data-testid^="project-row-"]')
        .filter({ hasText: "continue-branch-project" });
      await expect(row).toBeVisible({ timeout: 15_000 });
      state.projectId = (await row.getAttribute("data-testid"))?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}`);
      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.getByTestId("epic-title-input").fill("Continue-branch epic");
      await page.getByTestId("epic-description-input").fill("Create hello.py and util.py.");
      await page.getByTestId("epic-ac-input").fill("hello.py exists and prints 'hello'");
      await page.getByTestId("form-dialog-submit").click();

      await page.waitForURL(/\/projects\/[^/]+\/epics\/[^/]+/, { timeout: 15_000 });
      state.epicId = page.url().match(/\/epics\/([^/?#]+)/)?.[1] ?? "";
      expect(state.epicId).toBeTruthy();

      await page.getByTestId("start-run-btn").click();
      await expect(page).toHaveURL(/\/threads\/manager/, { timeout: 15_000 });

      // Standard work-done wait: the run parks in "waiting" and all tasks are done.
      await waitForWorkDone(page, state.projectId, state.epicId);

      const tRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}/threads`,
      );
      const threads: Array<{
        id: string;
        role: string;
        status: string;
        branch?: string | null;
        trial_id?: string | null;
      }> = await tRes.json();
      const firstManager = threads.find((t) => t.role === "manager");
      state.firstManagerId = firstManager?.id ?? "manager";
      // The auto-created "manager" trial carries no per-trial branch (branch=null);
      // the canonical branch lives on the epic.  trial_id is anchored to its own id.
      state.firstTrialId = firstManager?.trial_id ?? state.firstManagerId;
      const eRes = await page.request.get(`/api/projects/${state.projectId}/epics/${state.epicId}`);
      state.firstBranch = ((await eRes.json()).branch as string) ?? "";
      expect(state.firstManagerId).toBeTruthy();
      expect(state.firstBranch).toBeTruthy();
    });

    test("Continue on Branch → navigates to a new thread id", async ({ page }) => {
      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicId}/threads/${state.firstManagerId}`,
      );
      // The thread list is persistent in the desktop sidebar.
      const threadsNav = page.locator('nav[aria-label="Threads"]');
      await expect(threadsNav).toBeVisible({ timeout: 10_000 });
      await threadsNav.getByTestId("continue-branch-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible({ timeout: 10_000 });

      await page.getByTestId("trial-title-input").fill("More work");

      const [createResponse] = await Promise.all([
        page.waitForResponse(
          (resp) => resp.url().includes("/threads") && resp.request().method() === "POST",
          { timeout: 15_000 },
        ),
        page.getByTestId("form-dialog-submit").click(),
      ]);
      expect(createResponse.status(), "Continuation creation API should return 201").toBe(201);
      const cont = await createResponse.json();
      state.contId = cont.id;

      // The continuation shares the trial and branch of the trial it continues.
      expect(cont.id, "Continuation is a new conversation thread").not.toBe(state.firstManagerId);
      expect(cont.branch, "SAME branch — no ordinal suffix").toBe(state.firstBranch);
      expect(cont.trial_id, "Continuation shares the trial (same worktree)").toBe(
        state.firstTrialId,
      );

      await page.waitForURL(new RegExp(`/threads/${state.contId}`), { timeout: 15_000 });

      const composerAfterNav = page.getByTestId("thread-composer");
      await expect(
        composerAfterNav,
        "Composer should be visible immediately after in-app navigation",
      ).toBeVisible({ timeout: 20_000 });
    });

    test("New session is writable and starts empty", async ({ page }) => {
      expect(state.contId).toBeTruthy();
      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/threads/${state.contId}`);

      const composer = page.getByTestId("thread-composer");
      await expect(composer, "Composer should be visible on the continuation").toBeVisible({
        timeout: 15_000,
      });
      const readonlyBanner = page.getByTestId("thread-readonly-banner");
      await expect(readonlyBanner, "Continuation should not be read-only").not.toBeVisible();

      // Fresh context: the message history is empty (via API — deterministic).
      const mRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}/threads/${state.contId}`,
      );
      expect(mRes.ok()).toBeTruthy();
      expect(await mRes.json(), "Continuation starts with no inherited conversation").toEqual([]);

      await page.screenshot({
        path: `${SHOTS}/continue-branch-new-session.png`,
        fullPage: true,
      });
    });

    test("API: previous archived / new active / same branch / same trial", async ({ page }) => {
      expect(state.contId).toBeTruthy();

      const tRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}/threads`,
      );
      expect(tRes.ok()).toBeTruthy();
      const threads: Array<{
        id: string;
        role: string;
        status: string;
        branch?: string | null;
        trial_id?: string | null;
      }> = await tRes.json();

      const old = threads.find((t) => t.id === state.firstManagerId);
      expect(old, "Previous conversation should still exist as history").toBeDefined();
      expect(old?.status, "Previous conversation should be archived").toBe("archived");

      const cont = threads.find((t) => t.id === state.contId);
      expect(cont?.status, "Continuation should be active").toBe("active");
      expect(cont?.branch, "Continuation keeps the SAME branch").toBe(state.firstBranch);
      expect(cont?.trial_id, "Continuation shares the trial id").toBe(state.firstTrialId);

      const eRes = await page.request.get(`/api/projects/${state.projectId}/epics/${state.epicId}`);
      expect(eRes.ok()).toBeTruthy();
      const epic: { active_thread_id?: string | null; branch?: string } = await eRes.json();
      expect(epic.active_thread_id, "active_thread_id points to the continuation").toBe(
        state.contId,
      );
      expect(epic.branch, "epic.branch is unchanged (no new trial)").toBe(state.firstBranch);
    });
  });
