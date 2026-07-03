/**
 * P1 E2E: Diff conflict → Resolve with Agent → Fake resolves → merge succeeds
 *
 * Scenario:
 *   1. Create project → Create Epic → run → completed
 *      (Worker per_call[0]: writes conflict.txt with "EPIC" version; Evaluator accepts; host commits)
 *   2. Inside the spec, commit a "MAIN" version of conflict.txt to main
 *      (epic branch and main diverge at line2 → merge conflict)
 *   3. diff page (epic ⇔ default mode) → Merge to default → 409 conflict
 *   4. Resolve with Agent button → POST /git/resolve → resolve run starts
 *      (Worker per_call[1]: writes conflict.txt with "RESOLVED" version, git_add → git_commit)
 *   5. Poll the API until the resolve run reaches completed
 *   6. Merge to default again → 200 + merge SHA (no conflict because already resolved)
 *   7. Verify via git that default(main) contains the resolved content (RESOLVED, no markers)
 *
 * Note: When all repos in a single epic are merged, the backend updates epic.status=merged (after git.py fix).
 *       This spec proves merge success via both "epic.status=merged" and the actual git content
 *       (resolved version on default, no conflict markers) — without relying on a self-PATCH.
 *
 * Note: The "Resolve with Agent" button has no data-testid, so it is clicked by the
 *       Button's text content (resolveWithAgent i18n key).
 */

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { CONFLICT_RESOLVE_SEED } from "./conflict-resolve-seed";

test.describe
  .serial("conflict resolve — Fake resolves and merge succeeds", () => {
    const state = {
      projectId: "",
      epicId: "",
      epicBranch: "",
    };

    // ---- Helper: wait for epic status via API polling ----
    async function waitForEpicStatus(
      page: import("@playwright/test").Page,
      expectedStatus: string,
      timeoutMs = 120_000,
    ): Promise<void> {
      await expect
        .poll(
          async () => {
            const res = await page.request.get(
              `/api/projects/${state.projectId}/epics/${state.epicId}`,
            );
            return (await res.json()).status;
          },
          { timeout: timeoutMs, intervals: [500, 1000, 2000] },
        )
        .toBe(expectedStatus);
    }

    // -----------------------------------------------------------------------
    test("1. create project", async ({ page }) => {
      await page.goto("/projects");

      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("project-name-input").fill("conflict-resolve-project");
      await page.getByTestId("repo-path-input-0").fill(CONFLICT_RESOLVE_SEED.repoDir);
      await page.getByTestId("form-dialog-submit").click();

      const row = page.locator('[data-testid^="project-row-"]').first();
      await expect(row).toBeVisible({ timeout: 15_000 });
      const testId = await row.getAttribute("data-testid");
      state.projectId = testId?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();
      console.log(`[conflict-resolve] projectId = ${state.projectId}`);
    });

    // -----------------------------------------------------------------------
    test("2. create Epic, run it and wait until completed", async ({ page }) => {
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}`);
      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("epic-title-input").fill("Conflict resolve epic");
      await page
        .getByTestId("epic-description-input")
        .fill("Write conflict.txt to trigger a merge conflict.");
      await page.getByTestId("epic-ac-input").fill("conflict.txt exists with EPIC content.");
      await page.getByTestId("form-dialog-submit").click();

      await page.waitForURL(/\/epics\//, { timeout: 15_000 });
      state.epicId = page.url().match(/\/epics\/([^/?#]+)/)?.[1] ?? "";
      expect(state.epicId).toBeTruthy();
      console.log(`[conflict-resolve] epicId = ${state.epicId}`);

      // Start run
      const startBtn = page.getByTestId("start-run-btn");
      await expect(startBtn).toBeVisible({ timeout: 10_000 });
      await startBtn.click();
      await expect(page).toHaveURL(/\/threads\//, { timeout: 15_000 });

      // Wait until completed (per_call[0]: writes conflict.txt with EPIC version)
      await waitForEpicStatus(page, "in_review");
      console.log("[conflict-resolve] Epic run completed");

      // Retrieve epic branch name (used to verify the resolve run)
      const epicRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}`,
      );
      const epicData = await epicRes.json();
      state.epicBranch = epicData.branch ?? "";
      console.log(`[conflict-resolve] epic branch = ${state.epicBranch}`);
    });

    // -----------------------------------------------------------------------
    test("3. commit MAIN version of conflict.txt to main to create a conflict", async () => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      // Use Node's execFileSync to commit the MAIN version to myrepo's main branch.
      // Committing a conflicting change to main after the epic run causes line2 to conflict.
      const git = (args: string[]) =>
        execFileSync("git", args, {
          cwd: CONFLICT_RESOLVE_SEED.repoDir,
          stdio: "pipe",
          env: {
            ...process.env,
            GIT_AUTHOR_NAME: "yukar-e2e",
            GIT_AUTHOR_EMAIL: "e2e@yukar.local",
            GIT_COMMITTER_NAME: "yukar-e2e",
            GIT_COMMITTER_EMAIL: "e2e@yukar.local",
          },
        });

      // Checkout main
      git(["checkout", "main"]);

      // Overwrite conflict.txt with the MAIN version
      const conflictPath = path.join(CONFLICT_RESOLVE_SEED.repoDir, "conflict.txt");
      fs.writeFileSync(conflictPath, "line1\nMAIN\nline3\n", "utf8");

      git(["add", "conflict.txt"]);
      git(["commit", "-m", "main: update conflict.txt to MAIN version"]);

      // Verify HEAD on main (sanity check after worktree operations)
      const log = execFileSync("git", ["log", "--oneline", "-3"], {
        cwd: CONFLICT_RESOLVE_SEED.repoDir,
        encoding: "utf8",
      });
      console.log(`[conflict-resolve] main log after MAIN commit:\n${log}`);

      // Confirm the conflict was created (API side does nothing; git state check only)
      expect(state.epicBranch).toBeTruthy();
    });

    // -----------------------------------------------------------------------
    test("4. on the diff page, Merge to default returns 409 conflict", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/diff`);

      // Switch to epic ⇔ default mode
      const epicModeBtn = page.getByRole("button", { name: /⇔/ });
      await expect(epicModeBtn).toBeVisible({ timeout: 15_000 });
      await epicModeBtn.click();

      // Verify that conflict.txt appears in the changed-files-panel
      const changedFilesPanel = page.getByTestId("changed-files-panel");
      await expect(changedFilesPanel.locator("text=conflict.txt")).toBeVisible({ timeout: 15_000 });

      // Click the Merge to default button
      const mergeBtn = page.getByTestId("merge-to-default-btn");
      await expect(mergeBtn).toBeEnabled({ timeout: 10_000 });
      await mergeBtn.click();

      // Confirm merge
      const confirmBtn = page.getByTestId("confirm-merge-btn");
      await expect(confirmBtn).toBeVisible({ timeout: 5_000 });
      await confirmBtn.click();

      // The conflict banner appears (409 conflict → conflictFiles is set)
      // The "Resolve with Agent" button appears inside the DiffStatusBanners conflict banner
      const resolveBtn = page.getByRole("button", {
        name: /Resolve with Agent|エージェントで解決/i,
      });
      await expect(resolveBtn).toBeVisible({ timeout: 15_000 });
      console.log("[conflict-resolve] Conflict detected, Resolve with Agent button visible");
    });

    // -----------------------------------------------------------------------
    test("5. Resolve with Agent → resolve run reaches completed", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/diff`);

      // Switch to epic ⇔ default mode
      const epicModeBtn = page.getByRole("button", { name: /⇔/ });
      await expect(epicModeBtn).toBeVisible({ timeout: 15_000 });
      await epicModeBtn.click();

      // Merge to default → confirm → 409
      const mergeBtn = page.getByTestId("merge-to-default-btn");
      await expect(mergeBtn).toBeEnabled({ timeout: 10_000 });
      await mergeBtn.click();
      const confirmBtn = page.getByTestId("confirm-merge-btn");
      await expect(confirmBtn).toBeVisible({ timeout: 5_000 });
      await confirmBtn.click();

      // Wait for the "Resolve with Agent" button and click it
      // Intercept the POST /git/resolve response to obtain the run_id
      const resolveBtn = page.getByRole("button", {
        name: /Resolve with Agent|エージェントで解決/i,
      });
      await expect(resolveBtn).toBeVisible({ timeout: 15_000 });

      // Intercept the network response to obtain run_id
      const [resolveResponse] = await Promise.all([
        page.waitForResponse(
          (resp) => resp.url().includes("/git/resolve") && resp.request().method() === "POST",
          { timeout: 15_000 },
        ),
        resolveBtn.click(),
      ]);
      expect(resolveResponse.status(), "POST /git/resolve should return 202").toBe(202);
      const resolveData = await resolveResponse.json();
      const resolveRunId = resolveData.run_id as string;
      expect(resolveRunId).toBeTruthy();
      console.log(`[conflict-resolve] Resolve run started: run_id = ${resolveRunId}`);

      // Wait until the resolve run reaches completed (verified against the specific run_id)
      // FAKE_SLEEP=0 means it completes almost instantly, but there is a short lag before state updates.
      await expect
        .poll(
          async () => {
            const res = await page.request.get(
              `/api/projects/${state.projectId}/epics/${state.epicId}/run/state`,
            );
            if (!res.ok()) return "no_state";
            const data = await res.json();
            // Confirm completion for this specific run_id
            if (data.run_id !== resolveRunId) return "different_run";
            return data.status ?? "no_status";
          },
          { timeout: 120_000, intervals: [200, 500, 1000, 2000] },
        )
        .toBe("completed");
      console.log("[conflict-resolve] Resolve run completed");
    });

    // -----------------------------------------------------------------------
    test("6. Merge to default again → 200 + merge SHA succeeds", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/diff`);

      // Switch to epic ⇔ default mode
      const epicModeBtn = page.getByRole("button", { name: /⇔/ });
      await expect(epicModeBtn).toBeVisible({ timeout: 15_000 });
      await epicModeBtn.click();

      // Click the Merge to default button
      const mergeBtn = page.getByTestId("merge-to-default-btn");
      await expect(mergeBtn).toBeEnabled({ timeout: 10_000 });
      await mergeBtn.click();

      // Confirm merge
      const confirmBtn = page.getByTestId("confirm-merge-btn");
      await expect(confirmBtn).toBeVisible({ timeout: 5_000 });

      // Intercept POST /git/merge response to verify the merge SHA
      const [mergeResponse] = await Promise.all([
        page.waitForResponse(
          (resp) => resp.url().includes("/git/merge") && resp.request().method() === "POST",
          { timeout: 15_000 },
        ),
        confirmBtn.click(),
      ]);
      expect(mergeResponse.status(), "POST /git/merge should return 200").toBe(200);
      const mergeData = await mergeResponse.json();
      expect(mergeData.sha, "merge SHA should be returned").toBeTruthy();
      console.log(`[conflict-resolve] Merge succeeded: sha = ${mergeData.sha}`);

      // Success: confirm button disappears (conflict resolved, merge succeeded)
      await expect(confirmBtn).not.toBeVisible({ timeout: 30_000 });
      console.log("[conflict-resolve] Merge UI confirms success");

      // When all repos in a single epic are merged, the backend updates epic.status to merged (actual behaviour).
      await waitForEpicStatus(page, "merged");
      console.log(
        "[conflict-resolve] epic.status = merged (backend updated on single-repo merge completion)",
      );

      // Screenshot
      await page.screenshot({
        path: "test-results/conflict-resolve-merged.png",
        fullPage: true,
      });
    });

    // -----------------------------------------------------------------------
    test("7. resolved content is merged into the default branch (verified via git)", () => {
      // Directly verify via git that the resolved version (RESOLVED) written by the Fake resolve worker
      // is actually present on default(main) and contains no conflict markers whatsoever.
      // This is the strongest proof of "resolved → merge succeeded" (does not rely on self-PATCH of epic.status).
      const merged = execFileSync("git", ["show", "main:conflict.txt"], {
        cwd: CONFLICT_RESOLVE_SEED.repoDir,
        encoding: "utf8",
      });
      console.log(`[conflict-resolve] main:conflict.txt after merge =\n${merged}`);
      expect(merged).toBe("line1\nRESOLVED\nline3\n");
      expect(merged).not.toContain("<<<<<<<");
      expect(merged).not.toContain("=======");
      expect(merged).not.toContain(">>>>>>>");
    });
  });
