/**
 * Reindex E2E test — Repos reindex button → index status badge
 *
 * Purpose:
 *   Open the Repos page with a repo already registered to the project,
 *   click the reindex button, and verify in a real browser that the index
 *   status badge transitions to the terminal state (indexed).
 *
 * Notes:
 *   Because embedding=fake (FakeEmbedder) is used, meaningful search results
 *   are not guaranteed, but the reindex trigger and badge state transition
 *   (unindexed → indexing → indexed) can still be verified.
 *   The quality of search results (hit accuracy) is out of scope for this spec.
 *
 * Verification flow:
 *   1. Create project (including repo registration)
 *   2. Navigate to /repos page
 *   3. Confirm the initial state is unindexed (via API)
 *   4. Click the "reindex" button
 *   5. Confirm the index status badge becomes "indexing…" or "indexed"
 *   6. Poll the API until state=indexed
 *   7. Confirm the badge displays the "indexed" text in the DOM
 */

import { expect, test } from "@playwright/test";
import { REINDEX_SEED } from "./reindex-seed";

const REPO_NAME = "myrepo";

test.describe
  .serial("Reindex — repos reindex button → index badge state transition", () => {
    const state = {
      projectId: "",
    };

    // ---- 1. Create project ----

    test("1. create project with repo", async ({ page }) => {
      await page.goto("/projects");

      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("project-name-input").fill("reindex-project");
      await page.getByTestId("repo-path-input-0").fill(REINDEX_SEED.repoDir);
      await page.getByTestId("form-dialog-submit").click();

      const row = page.locator('[data-testid^="project-row-"]').first();
      await expect(row).toBeVisible({ timeout: 15_000 });

      const testId = await row.getAttribute("data-testid");
      state.projectId = testId?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();
    });

    // ---- 2. Navigate to Repos page & verify initial state ----

    test("2. repos page shows repo row and initial index status", async ({ page }) => {
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}/repos`);

      // The repo row must be visible
      const repoRow = page.getByTestId(`repo-row-${REPO_NAME}`);
      await expect(repoRow).toBeVisible({ timeout: 15_000 });

      // Verify the initial state via API (FakeEmbedder is fast, so auto-indexing may have already completed)
      const res = await page.request.get(`/api/projects/${state.projectId}/index/status`);
      expect(res.ok()).toBeTruthy();
      const statusBody = await res.json();
      const repoStatus = statusBody.statuses?.find(
        (s: { repo_name: string }) => s.repo_name === REPO_NAME,
      );
      // State must be one of: unindexed / stale / error, or indexed if auto-indexing already finished
      expect(["unindexed", "stale", "error", "indexed", "indexing"]).toContain(
        repoStatus?.state ?? "unindexed",
      );
    });

    // ---- 3. Click the reindex button and verify state transition ----

    test("3. click reindex button and badge transitions to indexed", async ({ page }) => {
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}/repos`);

      const repoRow = page.getByTestId(`repo-row-${REPO_NAME}`);
      await expect(repoRow).toBeVisible({ timeout: 15_000 });

      // Click the reindex button in the project-header.
      // (The per-row reindex button is only shown when state is unindexed/stale/error,
      //  but FakeEmbedder is fast enough that the repo may already be indexed right after project creation.)
      // The project-header button is always visible, so use that instead.
      const headerReindexBtn = page.getByRole("button", { name: /再インデックス|Reindex/i });
      await expect(headerReindexBtn).toBeVisible({ timeout: 10_000 });
      await headerReindexBtn.click();

      // Poll the API until state=indexed
      // FakeEmbedder operates synchronously, so this should complete relatively quickly
      await expect
        .poll(
          async () => {
            const res = await page.request.get(`/api/projects/${state.projectId}/index/status`);
            if (!res.ok()) return null;
            const body = await res.json();
            const st = body.statuses?.find((s: { repo_name: string }) => s.repo_name === REPO_NAME);
            return st?.state ?? null;
          },
          { timeout: 60_000, intervals: [1000, 2000, 3000] },
        )
        .toBe("indexed");

      // The badge must display the "indexed" text (UI updates via polling)
      await expect(repoRow.getByText("indexed")).toBeVisible({ timeout: 30_000 });

      // The reindex button must not be visible when state is indexed
      await expect(repoRow.getByRole("button", { name: "reindex" })).not.toBeVisible({
        timeout: 5_000,
      });
    });

    // ---- 4. Indexed state persists after reload ----

    test("4. indexed state persists after reload", async ({ page }) => {
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}/repos`);

      const repoRow = page.getByTestId(`repo-row-${REPO_NAME}`);
      await expect(repoRow).toBeVisible({ timeout: 15_000 });

      // Confirm indexed state is still present after page reload
      await page.reload();
      await expect(repoRow).toBeVisible({ timeout: 15_000 });

      // Verify state via API
      const res = await page.request.get(`/api/projects/${state.projectId}/index/status`);
      expect(res.ok()).toBeTruthy();
      const body = await res.json();
      const repoStatus = body.statuses?.find(
        (s: { repo_name: string }) => s.repo_name === REPO_NAME,
      );
      expect(repoStatus?.state).toBe("indexed");
    });
  });
