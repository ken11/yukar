/**
 * Focused smoke for the 6 reported fixes, driven by provider=fake.
 * Web(:3000) → API(:8000); Manager/Worker/Evaluator replay YUKAR_FAKE_SCRIPT (see seed.ts).
 *
 * Demonstrates (screenshots under playwright-report/):
 *   #1  Completing the epic (1-bit lifecycle) → controls flip to Reopen
 *   #2  Per-utterance bubble splitting — Manager thread renders one bubble per utterance
 *   #3  Hand-off persistence — Worker thread shows the Manager→Worker hand-off (user message)
 *   #4  commit-after-eval — host commit makes hello.py / util.py appear in the epic⇔default diff
 *   #5  repo_grep ran inside the Worker (script step) — implied by the successful run + host commit
 *
 * #6 (worker max-token failure) cannot be triggered by the deterministic fake model
 * (it never raises MaxTokensReachedException); it is covered by backend+frontend unit tests.
 */

import { expect, test } from "@playwright/test";
import { SEED } from "./seed";
import { waitForWorkDone } from "./wait-helpers";

const SHOTS = "playwright-report";

test.describe
  .serial("6-issue fake smoke", () => {
    const state = { projectId: "", epicId: "", workerThreadId: "" };

    test("setup: create project + epic, run until work is done", async ({ page }) => {
      // --- project ---
      await page.goto("/projects");
      await page.getByTestId("new-project-btn").click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.getByTestId("project-name-input").fill("smoke-project");
      await page.getByTestId("repo-path-input-0").fill(SEED.repoDirs.smoke);
      await page.getByTestId("form-dialog-submit").click();

      // Select THIS spec's project by name — the project list is shared across
      // specs in the main config, so `.first()` would return a different spec's
      // project (e.g. scenario's e2e-project) once more than one exists, building
      // this epic on the wrong repo.
      const row = page
        .locator('[data-testid^="project-row-"]')
        .filter({ hasText: "smoke-project" });
      await expect(row).toBeVisible({ timeout: 15_000 });
      state.projectId = (await row.getAttribute("data-testid"))?.replace("project-row-", "") ?? "";
      expect(state.projectId).toBeTruthy();

      // --- epic (creation navigates straight to the epic detail page) ---
      await page.goto(`/projects/${state.projectId}`);
      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.getByTestId("epic-title-input").fill("Smoke epic");
      await page.getByTestId("epic-description-input").fill("Create hello.py and util.py.");
      await page.getByTestId("epic-ac-input").fill("hello.py exists and prints 'hello'");
      await page.getByTestId("form-dialog-submit").click();

      await page.waitForURL(/\/projects\/[^/]+\/epics\/[^/]+/, { timeout: 15_000 });
      state.epicId = page.url().match(/\/epics\/([^/?#]+)/)?.[1] ?? "";
      expect(state.epicId).toBeTruthy();

      // --- run (start button lives on the epic detail header) ---
      await page.getByTestId("start-run-btn").click();
      await expect(page).toHaveURL(/\/threads\/manager/, { timeout: 15_000 });

      // Standard work-done wait (deterministic fake run): the conversation run
      // parks in "waiting" and every task is done. Finishing work never
      // transitions the epic (1-bit lifecycle) nor "completes" the run
      // (a conversation has no end).
      await waitForWorkDone(page, state.projectId, state.epicId);

      // Discover the worker thread id for the hand-off assertion.
      const tRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}/threads`,
      );
      const threads: Array<{ id: string; role: string }> = await tRes.json();
      state.workerThreadId = threads.find((t) => t.role === "worker")?.id ?? "";
      expect(state.workerThreadId, "a worker thread should exist after the run").toBeTruthy();
    });

    test("#2: Manager thread renders one bubble per utterance", async ({ page }) => {
      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/threads/manager`);
      const bubbles = page.getByTestId("agent-message");
      await expect(bubbles.first()).toBeVisible({ timeout: 20_000 });
      await page.screenshot({ path: `${SHOTS}/smoke-2-manager-bubbles.png`, fullPage: true });
      const count = await bubbles.count();
      expect(
        count,
        "Manager turn (task_update → dispatch → report text) must render as separate bubbles, not one crammed bubble",
      ).toBeGreaterThanOrEqual(3);
    });

    test("#3 + activity log: Worker thread retains hand-off AND tool-use activity", async ({
      page,
    }) => {
      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicId}/threads/${state.workerThreadId}`,
      );
      // Hand-off prompt persisted as a user message (issue③).
      const handoff = page.getByTestId("user-message").first();
      await expect(handoff).toBeVisible({ timeout: 20_000 });
      await expect(handoff).toContainText(/Task Contract|Task:|T1|contract/i);
      // The Worker's full conversation (fs_write / repo_grep / fs_write / text) is now
      // persisted (no FileSessionManager) → multiple agent bubbles remain on reload,
      // not just the final summary.
      const agentBubbles = page.getByTestId("agent-message");
      await expect(agentBubbles.first()).toBeVisible({ timeout: 10_000 });
      await page.screenshot({ path: `${SHOTS}/smoke-3-worker-activity-log.png`, fullPage: true });
      expect(
        await agentBubbles.count(),
        "Worker thread must retain its tool-use activity (multiple bubbles), not only the final reply",
      ).toBeGreaterThanOrEqual(2);
    });

    test("#4: host committed hello.py + util.py (epic⇔default diff)", async ({ page }) => {
      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/diff`);
      // Epic⇔default mode button (label is "{epicId} ⇔ default" / "… ⇔ デフォルト").
      await page.getByRole("button", { name: /⇔/ }).click();
      const panel = page.getByTestId("changed-files-panel");
      await expect(panel.locator("text=hello.py")).toBeVisible({ timeout: 20_000 });
      await page.screenshot({ path: `${SHOTS}/smoke-4-host-commit-diff.png`, fullPage: true });
      await expect(panel.locator("text=util.py")).toBeVisible({ timeout: 10_000 });
    });

    test("board: your-turn badge — static on load AND live via the project SSE", async ({
      page,
    }) => {
      // ---- Static: run_summary embedded in GET /epics ----
      // The setup run parked in "waiting" (work done), so a fresh board load
      // shows the your-turn badge on this epic's row without any SSE event.
      await page.goto(`/projects/${state.projectId}/epics`);
      await expect(
        page.getByTestId(`your-turn-${state.epicId}`),
        "Parked run (waiting + run_id) surfaces as a your-turn badge on load",
      ).toBeVisible({ timeout: 15_000 });

      // ---- Live: project SSE your-turn signal patches the open board ----
      // Create a second epic via the API, load the board (row present, no
      // badge — never ran), then start its run via the API and watch the badge
      // appear WITHOUT any reload when the run parks in "waiting"
      // (your_turn via the project-scope SSE).
      const createRes = await page.request.post(`/api/projects/${state.projectId}/epics`, {
        data: {
          title: "Board badge epic",
          description: "Create hello.py and util.py.",
          acceptance_criteria: "hello.py exists and prints 'hello'",
        },
      });
      expect(createRes.status(), "second epic created (201)").toBe(201);
      const epic2: { id: string } = await createRes.json();

      await page.goto(`/projects/${state.projectId}/epics`);
      await expect(page.getByTestId(`epic-card-${epic2.id}`)).toBeVisible({ timeout: 15_000 });
      const liveBadge = page.getByTestId(`your-turn-${epic2.id}`);
      await expect(liveBadge, "a never-run epic carries no your-turn badge").toHaveCount(0);

      const runRes = await page.request.post(
        `/api/projects/${state.projectId}/epics/${epic2.id}/run`,
      );
      expect(runRes.ok(), `run start should succeed: ${runRes.status()}`).toBeTruthy();

      // No reload, no navigation: the deterministic fake run finishes its work
      // and parks — the badge must appear on the still-open board page purely
      // via the project SSE cache patch.
      await expect(
        liveBadge,
        "your-turn badge appears live when the run parks in waiting",
      ).toBeVisible({ timeout: 90_000 });
      await page.screenshot({ path: `${SHOTS}/smoke-p4-board-your-turn.png`, fullPage: true });
    });

    test("#1: completing the epic flips the controls to Reopen", async ({ page }) => {
      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/threads/manager`);
      // Complete is inline in the desktop sidebar; it renders once the controls
      // settle into an idle branch (readiness wait).
      const completeBtn = page.getByTestId("complete-epic-btn");
      await expect(completeBtn).toBeVisible({ timeout: 20_000 });
      await completeBtn.click();

      // The user's single "finish" action: epic.status flips to completed …
      await expect
        .poll(
          async () => {
            const res = await page.request.get(
              `/api/projects/${state.projectId}/epics/${state.epicId}`,
            );
            return (await res.json()).status;
          },
          { timeout: 15_000, intervals: [500, 1000] },
        )
        .toBe("completed");

      // … and the header controls flip to the read-only branch (Reopen).
      await expect(page.getByTestId("reopen-btn")).toBeVisible({ timeout: 15_000 });
      await page.screenshot({ path: `${SHOTS}/smoke-1-after-complete.png`, fullPage: true });
    });
  });
