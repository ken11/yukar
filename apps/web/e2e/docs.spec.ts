/**
 * Docs edit/save/reload persistence E2E test.
 *
 * Purpose:
 *   Edit content on the project Docs page → Save → verify via page.reload()
 *   that the edits are persisted, using a real browser.
 *
 * Verification flow:
 *   1. Create a project
 *   2. Pre-create a project doc via API (PUT /docs/project.md)
 *   3. Navigate to /projects/{p}/docs and confirm the doc is displayed
 *   4. Append a unique string in the CodeMirror editor
 *   5. Click the Save button → confirm "Saved" feedback
 *   6. Assert that edited content survives page.reload()
 *   7. Confirm server-side content via GET /api/projects/{p}/docs/project.md
 */

import { expect, test } from "@playwright/test";
import { DOCS_SEED } from "./docs-seed";

const UNIQUE_MARKER = "e2e-docs-test-uniquemarker-XYZ789";
const DOC_FILENAME = "project.md";

test.describe
  .serial("Docs edit/save/reload persistence", () => {
    const state = {
      projectId: "",
    };

    // ---- 1. Create project ----

    test("1. create project", async ({ page }) => {
      await page.goto("/projects");

      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("project-name-input").fill("docs-project");
      await page.getByTestId("repo-path-input-0").fill(DOCS_SEED.repoDir);
      await page.getByTestId("form-dialog-submit").click();

      const row = page.locator('[data-testid^="project-row-"]').first();
      await expect(row).toBeVisible({ timeout: 15_000 });

      const testId = await row.getAttribute("data-testid");
      state.projectId = testId?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();
    });

    // ---- 2. Pre-create project doc via API ----

    test("2. pre-create project doc via API", async ({ page }) => {
      expect(state.projectId).toBeTruthy();

      const res = await page.request.put(`/api/projects/${state.projectId}/docs/${DOC_FILENAME}`, {
        data: { content: "# Project Notes\n\nInitial content.\n" },
      });
      expect(res.ok()).toBeTruthy();
    });

    // ---- 3. Navigate to Docs page ----

    test("3. docs page shows the doc", async ({ page }) => {
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}/docs`);

      // Wait until CodeMirror has loaded
      await expect(page.locator(".cm-content").first()).toBeVisible({ timeout: 30_000 });

      // Filename should appear in the nav (use first() to narrow multiple matches)
      await expect(page.getByText(DOC_FILENAME).first()).toBeVisible({ timeout: 10_000 });
    });

    // ---- 4. Edit content in CodeMirror ----

    test("4. edit content in CodeMirror and save", async ({ page }) => {
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}/docs`);

      // Wait until CodeMirror contenteditable is visible
      const cmContent = page.locator(".cm-content").first();
      await expect(cmContent).toBeVisible({ timeout: 30_000 });

      // Append a unique marker at the end of the document
      // Focus CodeMirror and type at the end
      await cmContent.click();
      // Move to end with Ctrl+End, then type
      await page.keyboard.press("Control+End");
      await page.keyboard.type(`\n\n${UNIQUE_MARKER}`);

      // Click the Save button (has aria-label="ドキュメントを保存")
      const saveBtn = page.getByRole("button", { name: "ドキュメントを保存" });
      await expect(saveBtn).toBeVisible({ timeout: 10_000 });
      await saveBtn.click();

      // "Saved" feedback should appear (appears briefly then disappears)
      await expect(saveBtn).toHaveText(/saved/i, { timeout: 10_000 });
    });

    // ---- 5. Confirm persistence after reload ----

    test("5. reload and verify persistence", async ({ page }) => {
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}/docs`);
      const cmContent = page.locator(".cm-content").first();
      await expect(cmContent).toBeVisible({ timeout: 30_000 });

      // Reload
      await page.reload();
      await expect(cmContent).toBeVisible({ timeout: 30_000 });

      // Marker should still be present in the editor
      const editorText = await cmContent.textContent();
      expect(editorText).toContain(UNIQUE_MARKER);
    });

    // ---- 6. Confirm server-side content via API ----

    test("6. server-side content persisted (API check)", async ({ page }) => {
      expect(state.projectId).toBeTruthy();

      const res = await page.request.get(`/api/projects/${state.projectId}/docs/${DOC_FILENAME}`);
      expect(res.ok()).toBeTruthy();

      const body = await res.json();
      expect(body.content).toContain(UNIQUE_MARKER);
    });
  });
