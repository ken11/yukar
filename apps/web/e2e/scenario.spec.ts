/**
 * Full-stack e2e: Web(:3000) → API(:8000) with provider=fake.
 * Manager/Worker/Evaluator run deterministically via YUKAR_FAKE_SCRIPT.
 *
 * Scenario:
 *   1. Create project (myrepo)
 *   2. Create epic
 *   3. Start Run → wait for "completed"
 *   4. Tasks page: T1 visible with contract
 *   5. Diff page: hello.py visible; merge → success
 *   6. Thread read-only: worker/evaluator have lock banner, manager has composer
 *   7. Settings: save worker agent-config, create skill, add MCP server → persist on reload
 */

import { expect, test } from "@playwright/test";
import { SEED } from "./seed";

/**
 * All tests run sequentially in one describe block.
 * The `state` object is shared across tests within the describe scope.
 */
test.describe
  .serial("e2e scenario", () => {
    // IDs discovered in early tests and reused in later tests
    const state = {
      projectId: "",
      epicId: "",
    };

    // ---- 1. Project creation ----

    test("1. create project and see it in list", async ({ page }) => {
      await page.goto("/projects");

      // Open New Project modal
      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("project-name-input").fill("e2e-project");
      await page.getByTestId("repo-path-input-0").fill(SEED.repoDirs.scenario);

      await page.getByTestId("form-dialog-submit").click();

      // Select THIS spec's project by name — the project list is shared across
      // specs in the main config, so `.first()` would return a different spec's
      // project once more than one exists.
      const row = page.locator('[data-testid^="project-row-"]').filter({ hasText: "e2e-project" });
      await expect(row).toBeVisible({ timeout: 15_000 });

      // Extract the project ID from data-testid
      const testId = await row.getAttribute("data-testid");
      state.projectId = testId?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();
    });

    // ---- 2. Epic creation ----

    test("2. create epic", async ({ page }) => {
      expect(state.projectId, "projectId from test 1").toBeTruthy();

      await page.goto(`/projects/${state.projectId}`);

      // new-epic-btn may appear multiple times, so use .first()
      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();

      await page.getByTestId("epic-title-input").fill("Write hello epic");
      await page
        .getByTestId("epic-description-input")
        .fill("Create a hello.py file that prints hello.");
      await page.getByTestId("epic-ac-input").fill("hello.py exists and prints 'hello'");

      await page.getByTestId("form-dialog-submit").click();

      // After epic creation, navigate directly to the epic detail page and extract epicId from the URL.
      await page.waitForURL(/\/projects\/[^/]+\/epics\/[^/]+/, { timeout: 15_000 });
      state.epicId = page.url().match(/\/epics\/([^/?#]+)/)?.[1] ?? "";
      expect(state.epicId).toBeTruthy();
    });

    // ---- 3. Start Run, wait for completed ----

    test("3. start run and wait for completed status", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      // Navigate directly to the epic detail page and click the Start Run button
      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}`);

      // Click Start Run button
      const startBtn = page.getByTestId("start-run-btn");
      await expect(startBtn).toBeVisible({ timeout: 10_000 });
      await startBtn.click();

      // We're now redirected to the manager thread page
      await expect(page).toHaveURL(/\/threads\/manager/, { timeout: 15_000 });

      // Wait for the epic to reach "completed" status via API polling.
      // The fake run should complete fast (YUKAR_FAKE_SLEEP=0) but we give it 90s.
      await expect
        .poll(
          async () => {
            const res = await page.request.get(
              `/api/projects/${state.projectId}/epics/${state.epicId}`,
            );
            return (await res.json()).status;
          },
          { timeout: 90_000, intervals: [500, 1000, 1000] },
        )
        .toBe("completed");
    });

    // ---- 4. Tasks page: T1 with contract ----

    test("4. tasks page shows T1 with contract", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/tasks`);

      // Wait for task row T1
      const taskRow = page.getByTestId("task-row-T1");
      await expect(taskRow).toBeVisible({ timeout: 15_000 });

      // Check contract text is present
      await expect(taskRow).toContainText("contract:", { timeout: 5_000 });
      await expect(taskRow).toContainText("hello.py");
    });

    // ---- 5. Diff page: hello.py visible, merge ----

    test("5. diff page shows hello.py and merge succeeds", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/diff`);

      // Switch to "epic ⇔ default" mode to see committed changes.
      // Button label = "{epicId} ⇔ default" / "… ⇔ デフォルト" (locale-dependent).
      // Use /⇔/ to match both locales.
      const epicModeBtn = page.getByRole("button", { name: /⇔/ });
      await expect(epicModeBtn).toBeVisible({ timeout: 15_000 });
      await epicModeBtn.click();

      // hello.py should appear in the Changed Files panel (scoped to the left panel)
      const changedFilesPanel = page.getByTestId("changed-files-panel");
      await expect(changedFilesPanel.locator("text=hello.py")).toBeVisible({ timeout: 15_000 });

      // Click "Merge to default" button
      const mergeBtn = page.getByTestId("merge-to-default-btn");
      await expect(mergeBtn).toBeEnabled({ timeout: 10_000 });
      await mergeBtn.click();

      // Confirm merge
      const confirmBtn = page.getByTestId("confirm-merge-btn");
      await expect(confirmBtn).toBeVisible({ timeout: 5_000 });
      await confirmBtn.click();

      // After successful merge the confirm button disappears
      await expect(confirmBtn).not.toBeVisible({ timeout: 15_000 });
    });

    // ---- 6. Thread read-only vs composer ----

    test("6a. manager thread has composer", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/threads/manager`);

      // Manager thread should show the composer textarea
      const composer = page.getByTestId("thread-composer");
      await expect(composer).toBeVisible({ timeout: 15_000 });

      // Should NOT show read-only banner
      const readonlyBanner = page.getByTestId("thread-readonly-banner");
      await expect(readonlyBanner).not.toBeVisible();
    });

    test("6b. worker thread is read-only", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();
      expect(state.epicId, "epicId").toBeTruthy();

      // Fetch the thread list via API to find the worker thread ID and navigate directly
      const tRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}/threads`,
      );
      const threads: Array<{ id: string; role: string }> = await tRes.json();
      const workerThreadId = threads.find((t) => t.role === "worker")?.id ?? "";
      expect(workerThreadId, "a worker thread should exist").toBeTruthy();

      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicId}/threads/${workerThreadId}`,
      );

      // Should show read-only banner
      const readonlyBanner = page.getByTestId("thread-readonly-banner");
      await expect(readonlyBanner).toBeVisible({ timeout: 10_000 });

      // Should NOT show composer
      const composer = page.getByTestId("thread-composer");
      await expect(composer).not.toBeVisible();
    });

    // ---- 8. Agent Profiles create & persist ----

    test("8. agent profiles: create frontend-worker and persist on reload", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();

      await page.goto(`/projects/${state.projectId}/settings`);

      // Agent Profiles section should be visible
      const section = page.getByTestId("agent-profiles-section");
      await expect(section).toBeVisible({ timeout: 15_000 });

      // Click "New Profile"
      await page.getByTestId("new-profile-btn").click();

      // Fill name
      const nameInput = page.getByTestId("profile-name-input");
      await expect(nameInput).toBeVisible();
      await nameInput.fill("frontend-worker");

      // base_role is already "worker" by default — confirm the worker button is visually active
      // (no need to click since emptyDraft starts with base_role="worker")

      // Fill description
      const descInput = page.getByTestId("profile-description-input");
      await expect(descInput).toBeVisible();
      await descInput.fill("Frontend-focused worker profile");

      // Fill instructions
      const instructionsTextarea = page.getByTestId("profile-instructions-textarea");
      await expect(instructionsTextarea).toBeVisible();
      await instructionsTextarea.fill("Always use TypeScript. Prefer functional components.");

      // Fill commands allow
      const commandsAllowTextarea = page.getByTestId("profile-commands-allow-textarea");
      await expect(commandsAllowTextarea).toBeVisible();
      await commandsAllowTextarea.fill("pnpm test");

      // Save
      const saveBtn = page.getByTestId("save-profile-btn");
      await expect(saveBtn).toBeVisible();
      await saveBtn.click();

      // Button shows "Saved" / "保存しました" briefly (locale-independent match)
      await expect(saveBtn).toContainText(/Saved|保存しました/, { timeout: 10_000 });

      // Reload and verify persistence
      await page.reload();

      const sectionAfter = page.getByTestId("agent-profiles-section");
      await expect(sectionAfter).toBeVisible({ timeout: 15_000 });

      // Profile list item should exist
      const profileItem = page.getByTestId("profile-list-item-frontend-worker");
      await expect(profileItem).toBeVisible({ timeout: 10_000 });

      // Click the profile to select it and load its data into the editor
      await profileItem.click();

      // Verify instructions are preserved
      const instructionsAfter = page.getByTestId("profile-instructions-textarea");
      await expect(instructionsAfter).toHaveValue(
        "Always use TypeScript. Prefer functional components.",
        { timeout: 10_000 },
      );

      // Verify commands allow is preserved
      const commandsAllowAfter = page.getByTestId("profile-commands-allow-textarea");
      await expect(commandsAllowAfter).toHaveValue("pnpm test", { timeout: 10_000 });
    });

    // ---- 9. Repos run_command allow/deny persist ----

    test("9. repos section: set myrepo allow/deny and persist on reload", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();

      // Repos has moved to the /repos tab (not under settings)
      await page.goto(`/projects/${state.projectId}/repos`);

      // myrepo row should exist (testid = repo-row-{name})
      const repoRow = page.getByTestId("repo-row-myrepo");
      await expect(repoRow).toBeVisible({ timeout: 15_000 });

      // Fill allow textarea
      const allowTextarea = page.getByTestId("repo-allow-textarea-myrepo");
      await expect(allowTextarea).toBeVisible();
      await allowTextarea.fill("pnpm test\npytest");

      // Fill deny textarea
      const denyTextarea = page.getByTestId("repo-deny-textarea-myrepo");
      await expect(denyTextarea).toBeVisible();
      await denyTextarea.fill("rm -rf");

      // Save
      const saveBtn = page.getByTestId("save-repo-commands-btn-myrepo");
      await expect(saveBtn).toBeVisible();
      await saveBtn.click();

      // Button shows "Saved" / "保存しました" briefly (locale-independent match)
      await expect(saveBtn).toContainText(/Saved|保存しました/, { timeout: 10_000 });

      // Reload and verify persistence
      await page.reload();

      const repoRowAfter = page.getByTestId("repo-row-myrepo");
      await expect(repoRowAfter).toBeVisible({ timeout: 15_000 });

      const allowTextareaAfter = page.getByTestId("repo-allow-textarea-myrepo");
      await expect(allowTextareaAfter).toHaveValue("pnpm test\npytest", { timeout: 10_000 });

      const denyTextareaAfter = page.getByTestId("repo-deny-textarea-myrepo");
      await expect(denyTextareaAfter).toHaveValue("rm -rf", { timeout: 10_000 });
    });

    // ---- 7. Settings persistence ----

    test("7. project settings: agent-config, skill, mcp persist on reload", async ({ page }) => {
      expect(state.projectId, "projectId").toBeTruthy();

      await page.goto(`/projects/${state.projectId}/settings`);

      // 7a. Agent config: select Worker tab, fill instructions, save
      const workerTab = page.getByRole("button", { name: /Worker/ }).first();
      await expect(workerTab).toBeVisible({ timeout: 10_000 });
      await workerTab.click();

      const workerTextarea = page.getByTestId("agent-config-textarea-worker");
      await expect(workerTextarea).toBeVisible();
      await workerTextarea.fill("Always use type hints in Python.");

      await page.getByTestId("save-agent-config-btn-worker").click();
      // Button text changes to "Saved" / "保存しました" briefly then back (locale-independent)
      await expect(page.getByTestId("save-agent-config-btn-worker")).toContainText(
        /Saved|保存しました/,
        {
          timeout: 10_000,
        },
      );

      // 7b. Skills: create a new skill
      await page.getByRole("button", { name: "New Skill" }).click();

      const skillNameInput = page.getByTestId("new-skill-name-input");
      await expect(skillNameInput).toBeVisible();
      await skillNameInput.fill("e2e-test-skill");

      await page.getByTestId("save-skill-btn").click();
      // After save, skill appears in sidebar list
      await expect(page.getByTestId("skill-list-item-e2e-test-skill")).toBeVisible({
        timeout: 10_000,
      });

      // 7c. MCP: add a server and save
      await page.getByTestId("add-mcp-server-btn").click();

      const mcpNameInput = page.getByTestId("mcp-server-name-input");
      await expect(mcpNameInput).toBeVisible();
      await mcpNameInput.fill("test-mcp");

      await page.getByTestId("save-mcp-btn").click();
      await expect(page.getByTestId("save-mcp-btn")).toContainText(/Saved|保存しました/, {
        timeout: 10_000,
      });

      // 7d. Reload and verify all three sections persisted
      await page.reload();

      // Worker config
      const workerTabAfter = page.getByRole("button", { name: /Worker/ }).first();
      await expect(workerTabAfter).toBeVisible({ timeout: 10_000 });
      await workerTabAfter.click();
      const workerTextareaAfter = page.getByTestId("agent-config-textarea-worker");
      await expect(workerTextareaAfter).toHaveValue("Always use type hints in Python.", {
        timeout: 10_000,
      });

      // Skill
      await expect(page.getByTestId("skill-list-item-e2e-test-skill")).toBeVisible({
        timeout: 10_000,
      });

      // MCP server name in list
      await expect(page.getByTestId("mcp-server-list-item-test-mcp")).toBeVisible({
        timeout: 10_000,
      });
    });
  });
