/**
 * Repos add/delete (CRUD) E2E test — Repos page "Add repository" + delete
 *
 * Purpose:
 *   Verify in a real browser that a repository can be added to and removed
 *   from an existing project directly on the Repos page — the capability that
 *   was previously missing (repos were locked in at project-creation time).
 *
 * Verification flow:
 *   1. Create a project registering repo "alpha".
 *   2. On the Repos page, add repo "beta" via the inline "Add repository" form.
 *   3. Confirm the new row appears and the API reports both repos.
 *   4. Adding a non-git path surfaces a validation error (422 → inline message).
 *   5. Delete "beta" via the per-row delete button + confirm dialog.
 *   6. Confirm the row is gone, "alpha" remains, and the API reports one repo.
 */

import { expect, test } from "@playwright/test";
import { REPOS_CRUD_SEED } from "./repos-crud-seed";

async function repoNames(
  request: { get: (url: string) => Promise<{ ok: () => boolean; json: () => Promise<unknown> }> },
  projectId: string,
): Promise<string[]> {
  const res = await request.get(`/api/projects/${projectId}/repos`);
  if (!res.ok()) return [];
  const body = (await res.json()) as Array<{ name: string }>;
  return body.map((r) => r.name).sort();
}

test.describe
  .serial("Repos — add and delete a repository on an existing project", () => {
    const state = { projectId: "" };

    // ---- 1. Create project with repo "alpha" ----

    test("1. create project registering repo alpha", async ({ page }) => {
      await page.goto("/projects");

      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("project-name-input").fill("repos-crud-project");
      await page.getByTestId("repo-path-input-0").fill(REPOS_CRUD_SEED.repoDirA);
      await page.getByTestId("form-dialog-submit").click();

      const row = page.locator('[data-testid^="project-row-"]').first();
      await expect(row).toBeVisible({ timeout: 15_000 });

      const testId = await row.getAttribute("data-testid");
      state.projectId = testId?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();
    });

    // ---- 2 & 3. Add repo "beta" via the inline form ----

    test("2. add repo beta via the Repos page form", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      await page.goto(`/projects/${state.projectId}/repos`);

      // alpha is already registered.
      await expect(page.getByTestId("repo-row-alpha")).toBeVisible({ timeout: 15_000 });

      // Open the inline add form and register beta (name left blank → derived from path).
      await page.getByTestId("add-repo-btn").click();
      await expect(page.getByTestId("add-repo-form")).toBeVisible();
      await page.getByTestId("add-repo-path-input").fill(REPOS_CRUD_SEED.repoDirB);
      await page.getByTestId("add-repo-submit").click();

      // The new row appears (list refetches after the mutation).
      await expect(page.getByTestId("repo-row-beta")).toBeVisible({ timeout: 15_000 });

      // API confirms both repos are registered.
      await expect
        .poll(() => repoNames(page.request, state.projectId), { timeout: 15_000 })
        .toEqual(["alpha", "beta"]);
    });

    // ---- 4. A non-git path is rejected with an inline error ----

    test("3. adding a non-git path shows a validation error", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      await page.goto(`/projects/${state.projectId}/repos`);

      await page.getByTestId("add-repo-btn").click();
      await page.getByTestId("add-repo-path-input").fill(REPOS_CRUD_SEED.workspaceDir);
      await page.getByTestId("add-repo-submit").click();

      // The 422 surfaces as an inline error and no phantom row is created.
      await expect(page.getByTestId("add-repo-error")).toBeVisible({ timeout: 10_000 });
      const names = await repoNames(page.request, state.projectId);
      expect(names).toEqual(["alpha", "beta"]);
    });

    // ---- 5 & 6. Delete repo "beta" via the per-row delete button ----

    test("4. delete repo beta via the delete button and confirm dialog", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      await page.goto(`/projects/${state.projectId}/repos`);

      await expect(page.getByTestId("repo-row-beta")).toBeVisible({ timeout: 15_000 });

      // Open the confirm dialog and confirm the removal.
      await page.getByTestId("delete-repo-btn-beta").click();
      await page.getByTestId("delete-repo-confirm-btn").click();

      // The row disappears; alpha remains.
      await expect(page.getByTestId("repo-row-beta")).toHaveCount(0, { timeout: 15_000 });
      await expect(page.getByTestId("repo-row-alpha")).toBeVisible();

      // API confirms only alpha is left.
      await expect
        .poll(() => repoNames(page.request, state.projectId), { timeout: 15_000 })
        .toEqual(["alpha"]);
    });
  });
