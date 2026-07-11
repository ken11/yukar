/**
 * Multi-trial E2E smoke — real-device verification of the "multiple Manager trials" feature.
 *
 * Original bug being reproduced:
 *   "New thread is read-only with no composer" (no navigation / composer absent)
 *
 * Verification items:
 *   1. setup: create project + epic → fake run until work done (run parks in waiting)
 *   2. Create new trial → navigate to new URL (should not stay on "manager")
 *      (the previous trial is still "active" — conversations never resolve —
 *      so the modal's archive_active=true archives it atomically)
 *   3. New trial has composer visible and no readonly banner
 *   4. API: old trial=archived / new trial=active / epic.active_thread_id points to new trial
 *   5. List pane: both trials are listed (active + archived sections)
 *   6. Archiving the ACTIVE trial while a live run is parked in waiting is
 *      allowed (the parked run is shelved, not a 409) — P3 shelving semantics
 */

import { expect, test } from "@playwright/test";
import { SEED } from "./seed";
import { waitForWorkDone } from "./wait-helpers";

const SHOTS = "playwright-report";

test.describe
  .serial("multi-trial fake smoke", () => {
    const state = {
      projectId: "",
      epicId: "",
      firstManagerId: "",
      newTrialId: "",
    };

    // -------------------------------------------------------------------------
    // 1. setup — same pattern as smoke.spec.ts
    // -------------------------------------------------------------------------
    test("setup: create project + epic, run until work is done", async ({ page }) => {
      // --- project ---
      await page.goto("/projects");
      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.getByTestId("project-name-input").fill("multi-trial-project");
      await page.getByTestId("repo-path-input-0").fill(SEED.repoDirs.multiTrial);
      await page.getByTestId("form-dialog-submit").click();

      // Select THIS spec's project by name — the project list is shared across
      // specs in the main config, so `.first()` would return a different spec's
      // project once more than one exists.
      const row = page
        .locator('[data-testid^="project-row-"]')
        .filter({ hasText: "multi-trial-project" });
      await expect(row).toBeVisible({ timeout: 15_000 });
      state.projectId = (await row.getAttribute("data-testid"))?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();

      // --- epic ---
      await page.goto(`/projects/${state.projectId}`);
      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.getByTestId("epic-title-input").fill("Multi-trial epic");
      await page.getByTestId("epic-description-input").fill("Create hello.py and util.py.");
      await page.getByTestId("epic-ac-input").fill("hello.py exists and prints 'hello'");
      await page.getByTestId("form-dialog-submit").click();

      await page.waitForURL(/\/projects\/[^/]+\/epics\/[^/]+/, { timeout: 15_000 });
      state.epicId = page.url().match(/\/epics\/([^/?#]+)/)?.[1] ?? "";
      expect(state.epicId).toBeTruthy();

      // --- run ---
      await page.getByTestId("start-run-btn").click();
      await expect(page).toHaveURL(/\/threads\/manager/, { timeout: 15_000 });

      // Standard work-done wait: the run parks in "waiting" and all tasks are done.
      await waitForWorkDone(page, state.projectId, state.epicId);

      // Record the manager thread id immediately after the run
      const tRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}/threads`,
      );
      const threads: Array<{ id: string; role: string; status: string }> = await tRes.json();
      state.firstManagerId = threads.find((t) => t.role === "manager")?.id ?? "manager";
      expect(state.firstManagerId).toBeTruthy();
    });

    // -------------------------------------------------------------------------
    // 2. Create new trial → URL navigation
    // -------------------------------------------------------------------------
    test("After creating a new trial, the URL navigates to the new thread id", async ({ page }) => {
      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicId}/threads/${state.firstManagerId}`,
      );
      // New trial button — target the button inside the left pane (nav[aria-label="Threads"])
      // The header also has a button with the same name, so narrow down to avoid strict mode errors
      const threadsNav = page.locator('nav[aria-label="Threads"]');
      await expect(threadsNav).toBeVisible({ timeout: 10_000 });
      await threadsNav.getByTestId("new-thread-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible({ timeout: 10_000 });

      // Enter title
      await page.getByTestId("trial-title-input").fill("Second attempt");

      // submit — capture the POST response before checking URL navigation.
      // Passing a pattern that matches the current URL (/threads/manager) to waitForURL
      // would resolve immediately before router.push, so intercept the API response first.
      const [createResponse] = await Promise.all([
        page.waitForResponse(
          (resp) => resp.url().includes("/threads") && resp.request().method() === "POST",
          { timeout: 15_000 },
        ),
        page.getByTestId("form-dialog-submit").click(),
      ]);
      expect(createResponse.status(), "New trial creation API should return 201").toBe(201);
      const newThread = await createResponse.json();
      state.newTrialId = newThread.id;

      // Wait for URL navigation after router.push (new id has th- prefix)
      await page.waitForURL(new RegExp(`/threads/${state.newTrialId}`), { timeout: 15_000 });
      const newId = page.url().match(/\/threads\/([^/?#]+)/)?.[1] ?? "";
      expect(newId, "URL should have navigated to the new thread").toBeTruthy();
      expect(newId, "Should not remain on 'manager'").not.toBe("manager");
      expect(newId, "Should differ from the old trial id").not.toBe(state.firstManagerId);
      expect(newId, "Should match the id in the API response").toBe(state.newTrialId);

      // --- Composer should appear within the same in-app navigation (no page.goto) ---
      // Regression of the original bug: right after router.push, the layout RSC was stale
      // and epic.active_thread_id still pointed to the old trial, causing the composer to disappear.
      // Fixed path:
      //   NewThreadModal onSuccess invalidates queryKeys.epics.detail
      //   → EpicShell useQuery re-fetches the latest epic
      //   → liveActiveThreadId (new trial id) is passed as activeThreadId to useRunActivity
      //   → SET_MANAGER_THREAD_ID dispatch updates managerThreadId
      //   → computeIsActiveTrial returns true and the composer is shown.
      // INIT / applyTreeInit are fixes for tree display nodes and are unrelated to the composer.
      const composerAfterNav = page.getByTestId("thread-composer");
      await expect(
        composerAfterNav,
        "Composer should be visible immediately after in-app navigation (no page.goto)",
      ).toBeVisible({ timeout: 20_000 });

      // Also verify that no readonly banner is present
      const readonlyBannerAfterNav = page.getByTestId("thread-readonly-banner");
      await expect(
        readonlyBannerAfterNav,
        "No readonly banner should appear immediately after in-app navigation",
      ).not.toBeVisible();
    });

    // -------------------------------------------------------------------------
    // 3. New trial is writable (composer visible / no readonly banner)
    // -------------------------------------------------------------------------
    test("New trial shows composer and has no readonly banner", async ({ page }) => {
      expect(state.newTrialId, "New trial id should have been captured").toBeTruthy();

      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicId}/threads/${state.newTrialId}`,
      );

      // Composer should be visible (most important: direct negation of the original bug)
      const composer = page.getByTestId("thread-composer");
      await expect(composer, "Composer should be visible on the new trial").toBeVisible({
        timeout: 15_000,
      });

      // No readonly banner present
      const readonlyBanner = page.getByTestId("thread-readonly-banner");
      await expect(readonlyBanner, "New trial should not show a readonly banner").not.toBeVisible();

      // No archived banner either (it is the active trial)
      const archivedBanner = page.getByTestId("thread-archived-banner");
      await expect(archivedBanner, "New trial should not be archived").not.toBeVisible();

      await page.screenshot({
        path: `${SHOTS}/multi-trial-3-new-trial-composer.png`,
        fullPage: true,
      });
    });

    // -------------------------------------------------------------------------
    // 3b. Old trial (archived by the new-trial creation) becomes read-only
    //
    // Once the active trial (newTrialId) is established, opening firstManagerId directly
    // should show the readonly banner rather than the composer.
    // -------------------------------------------------------------------------
    test("Opening the old trial (archived) shows the readonly banner and no composer", async ({
      page,
    }) => {
      expect(state.firstManagerId, "Old trial id should have been captured").toBeTruthy();
      expect(state.newTrialId, "New trial id should have been captured").toBeTruthy();

      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicId}/threads/${state.firstManagerId}`,
      );

      // The archived banner should appear (the old trial was archived by the
      // new-trial creation — conversations never "resolve" under P3, so the
      // read-only presentation for a superseded trial is the archived state).
      const archivedBanner = page.getByTestId("thread-archived-banner");
      await expect(archivedBanner, "Old trial should show the archived banner").toBeVisible({
        timeout: 15_000,
      });

      // Composer should not be present
      const composer = page.getByTestId("thread-composer");
      await expect(composer, "Old trial should not show the composer").not.toBeVisible();

      await page.screenshot({
        path: `${SHOTS}/multi-trial-3b-old-trial-readonly.png`,
        fullPage: true,
      });
    });

    // -------------------------------------------------------------------------
    // 4. API verification of the archive model
    // -------------------------------------------------------------------------
    test("API: old trial is inactive / new trial active / branches differ / active_thread_id updated", async ({
      page,
    }) => {
      expect(state.newTrialId).toBeTruthy();

      // Thread list
      const tRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}/threads`,
      );
      expect(tRes.ok()).toBeTruthy();
      const threads: Array<{
        id: string;
        role: string;
        status: string;
        branch?: string | null;
      }> = await tRes.json();

      // Two manager threads
      const managerThreads = threads.filter((t) => t.role === "manager");
      expect(
        managerThreads.length,
        "There should be at least 2 threads with the manager role",
      ).toBeGreaterThanOrEqual(2);

      // Old trial is archived. Under P3 a conversation never "resolves" — the
      // run parks in waiting and the trial stays active until the new-trial
      // creation (archive_active=true) archives it atomically.
      const old = managerThreads.find((t) => t.id === state.firstManagerId);
      expect(old, "Old trial should exist in the list").toBeDefined();
      expect(old?.status, "Old trial should be archived by the new-trial creation").toBe(
        "archived",
      );

      // New trial = active
      const newT = managerThreads.find((t) => t.id === state.newTrialId);
      expect(newT, "New trial should exist in the list").toBeDefined();
      expect(newT?.status, "New trial should be active").toBe("active");

      // Branches differ
      if (old?.branch && newT?.branch) {
        expect(old.branch, "Old and new trial branches should differ").not.toBe(newT.branch);
      }

      // epic.active_thread_id should point to the new trial
      const eRes = await page.request.get(`/api/projects/${state.projectId}/epics/${state.epicId}`);
      expect(eRes.ok()).toBeTruthy();
      const epic: { active_thread_id?: string | null } = await eRes.json();
      expect(epic.active_thread_id, "epic.active_thread_id should point to the new trial id").toBe(
        state.newTrialId,
      );
    });

    // -------------------------------------------------------------------------
    // 5. List pane: both manager threads are displayed
    //
    // The old trial was archived by the new-trial creation, so it appears in
    // the archived section while the new trial sits in the active section.
    // -------------------------------------------------------------------------
    test("Both old and new trials appear in the thread list pane", async ({ page }) => {
      expect(state.newTrialId).toBeTruthy();

      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicId}/threads/${state.newTrialId}`,
      );

      // Verify the left pane is present (always shown at md width)
      const nav = page.locator('nav[aria-label="Threads"]');
      await expect(nav).toBeVisible({ timeout: 10_000 });

      // A link to the old trial (Trial 1) should exist in the list
      // The agent tree panel may also contain a link with the same href, so use first()
      const oldTrialLink = nav.locator(`a[href*="/threads/${state.firstManagerId}"]`).first();
      await expect(oldTrialLink, "Link to old Trial 1 should be visible in the list").toBeVisible({
        timeout: 10_000,
      });

      // A link to the new trial should exist in the list (currently active)
      const newTrialLink = nav.locator(`a[href*="/threads/${state.newTrialId}"]`).first();
      await expect(newTrialLink, "Link to the new trial should be visible in the list").toBeVisible(
        {
          timeout: 10_000,
        },
      );

      await page.screenshot({
        path: `${SHOTS}/multi-trial-5-thread-list-pane.png`,
        fullPage: true,
      });
    });

    // -------------------------------------------------------------------------
    // 6. Archiving the ACTIVE trial is allowed while no turn is executing
    //
    // P3 shelving semantics: only an EXECUTING turn (running/paused) holds the
    // run slot and returns 409. A run parked in "waiting" never blocks trial
    // mutations (it is shelved). The old trial is already archived (test 2),
    // so we archive the active trial itself while viewing it and assert the
    // page flips to its archived read-only presentation.
    // -------------------------------------------------------------------------
    test("Archiving the active trial while viewing it succeeds and turns read-only", async ({
      page,
    }) => {
      expect(state.newTrialId, "New trial id should have been captured").toBeTruthy();

      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicId}/threads/${state.newTrialId}`,
      );
      const nav = page.locator('nav[aria-label="Threads"]');
      await expect(nav).toBeVisible({ timeout: 10_000 });

      // Hover over the active trial row to reveal the archive button and click it
      // aria-label is locale-dependent (ja: "アーカイブ" / en: "Archive") so use a regex
      const activeTrialRow = nav.locator(`a[href*="/threads/${state.newTrialId}"]`).first();
      await activeTrialRow.hover();
      const archiveBtnInRow = nav.getByRole("button", { name: /archive|アーカイブ/i }).first();
      await expect(archiveBtnInRow).toBeVisible({ timeout: 5_000 });

      const [archiveResp] = await Promise.all([
        page.waitForResponse(
          (resp) =>
            resp.url().includes("/threads/") &&
            resp.url().includes("/archive") &&
            resp.request().method() === "POST",
          { timeout: 10_000 },
        ),
        archiveBtnInRow.click(),
      ]);
      expect(
        archiveResp.status(),
        "Archive API should return 200 (no run is executing — a parked run never blocks)",
      ).toBe(200);

      // The archived trial page becomes read-only: archived banner, no composer.
      await expect(
        page.getByTestId("thread-archived-banner"),
        "Archived banner should appear on the archived trial",
      ).toBeVisible({ timeout: 15_000 });
      await expect(
        page.getByTestId("thread-composer"),
        "Composer should disappear on the archived trial",
      ).not.toBeVisible();

      // The epic no longer has an active trial.
      const eRes = await page.request.get(`/api/projects/${state.projectId}/epics/${state.epicId}`);
      const epic: { active_thread_id?: string | null } = await eRes.json();
      expect(
        epic.active_thread_id,
        "active_thread_id is cleared by archiving the active trial",
      ).toBeNull();

      await page.screenshot({
        path: `${SHOTS}/multi-trial-6-archive-active.png`,
        fullPage: true,
      });
    });
  });
