/**
 * Browser verification E2E test — dev-server settings UI + agent browser tools.
 *
 * Group A (settings UI roundtrip):
 *   Configure one dev-server service on the Repos page (name/command/port/
 *   readiness path), save, confirm via REST and a reload that the values
 *   persisted, then remove the config and confirm it is cleared.
 *
 * Group B (agent flow):
 *   Seed the dev-server config via REST (PUT dev-server), then run a scripted
 *   worker-only task whose Worker really calls browser_open / browser_read.
 *   Under provider=fake only the LLM is scripted — the tools execute for
 *   real: the host launches `python3 -m http.server` inside the trial
 *   worktree and opens a headless Chromium page on it.  Assertions:
 *     - run parks in the standard "waiting" state with T1 done
 *     - exactly one worker thread (resolved), zero evaluator threads
 *     - REST: browser_open / browser_read tool results have status "success"
 *       and carry the committed fixture heading (the browser saw the real page)
 *     - UI: the worker thread timeline shows both tool rows as succeeded
 *
 * Group C (manager flow):
 *   The epic explicitly asks for a browser test of a written scenario and the
 *   scripted MANAGER verifies it ITSELF through the repo-dispatching bundle
 *   (browser_open / browser_read with `repo=<name>`).  Nothing is dispatched,
 *   so no trial worktree exists — the host serves the repo's BASE CHECKOUT
 *   (the turn-0 fallback).  Assertions:
 *     - run parks in "waiting" with ZERO worker and evaluator threads
 *     - REST (manager timeline): browser_open / browser_read succeeded and
 *       carry the committed fixture heading
 *     - UI: the manager conversation shows the tool rows and the per-step report
 */

import { expect, test } from "@playwright/test";
import {
  AGENT_BASE_PORT,
  BROWSER_VERIFY_SEED,
  BROWSER_VERIFY_SUMMARY_TEXT,
  FIXTURE_HEADING,
  FIXTURE_TITLE,
  MANAGER_BASE_PORT,
  MANAGER_SCENARIO_DESCRIPTION,
  SERVICE_COMMAND_LINE,
  SERVICE_COMMAND_TOKENS,
  SETTINGS_BASE_PORT,
} from "./browser-verify-seed";
import { waitForRunWaiting, waitForWorkDone } from "./wait-helpers";

// ---- Wire types (subset of the REST payloads this spec reads) ----

interface WireDevService {
  name: string;
  command: string[];
  cwd: string;
  base_port: number;
  readiness?: { path: string | null; timeout_seconds: number } | null;
  env?: Record<string, string>;
}

interface WireDevServerConfig {
  services: WireDevService[];
  browser?: { allowed_origins: string[]; allow_common_cdns: boolean };
}

interface WireRepo {
  name: string;
  dev_server?: WireDevServerConfig | null;
}

interface WireContentPart {
  text?: string | null;
  toolUse?: { toolUseId: string; name: string; input?: Record<string, unknown> } | null;
  toolResult?: { toolUseId: string; status?: string | null; text?: string | null } | null;
}

interface WireMessage {
  message: { role: string; content: WireContentPart[] };
  message_id: number;
}

type ApiRequest = {
  get: (url: string) => Promise<{ ok: () => boolean; json: () => Promise<unknown> }>;
};

async function getDevServer(
  request: ApiRequest,
  projectId: string,
  repoName: string,
): Promise<WireDevServerConfig | null> {
  const res = await request.get(`/api/projects/${projectId}/repos`);
  if (!res.ok()) return null;
  const repos = (await res.json()) as WireRepo[];
  return repos.find((r) => r.name === repoName)?.dev_server ?? null;
}

/** Fold toolResults onto their toolUse and return the pair for one tool name. */
function findToolCall(
  messages: WireMessage[],
  toolName: string,
): { toolUseId: string; status?: string | null; resultText: string } | null {
  const resultById = new Map<string, { status?: string | null; text?: string | null }>();
  for (const msg of messages) {
    for (const part of msg.message.content) {
      if (part.toolResult) resultById.set(part.toolResult.toolUseId, part.toolResult);
    }
  }
  for (const msg of messages) {
    for (const part of msg.message.content) {
      if (part.toolUse?.name === toolName) {
        const result = resultById.get(part.toolUse.toolUseId);
        return {
          toolUseId: part.toolUse.toolUseId,
          status: result?.status,
          resultText: result?.text ?? "",
        };
      }
    }
  }
  return null;
}

/** Create a project via the UI dialog and return its id (rows are filtered by
 * name — the project list is shared by both groups in this file). */
async function createProject(
  page: import("@playwright/test").Page,
  name: string,
  repoPath: string,
): Promise<string> {
  await page.goto("/projects");
  await page.getByTestId("new-project-btn").click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByTestId("project-name-input").fill(name);
  await page.getByTestId("repo-path-input-0").fill(repoPath);
  await page.getByTestId("form-dialog-submit").click();

  const row = page.locator('[data-testid^="project-row-"]').filter({ hasText: name });
  await expect(row).toBeVisible({ timeout: 15_000 });
  const projectId = (await row.getAttribute("data-testid"))?.replace("project-row-", "") ?? "";
  expect(projectId).toBeTruthy();
  return projectId;
}

// ---------------------------------------------------------------------------
// Group A — dev-server settings UI roundtrip (repo "webapp")
// ---------------------------------------------------------------------------

test.describe
  .serial("Browser verify A — dev-server settings roundtrip on the Repos page", () => {
    const state = { projectId: "" };
    const repoName = "webapp";

    test("1. create project registering the fixture repo", async ({ page }) => {
      state.projectId = await createProject(
        page,
        "browser-verify-settings",
        BROWSER_VERIFY_SEED.repoDirSettings,
      );
    });

    test("2. configure and save one service; values persist across reload", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      await page.goto(`/projects/${state.projectId}/repos`);
      await expect(page.getByTestId(`repo-row-${repoName}`)).toBeVisible({ timeout: 15_000 });

      // No config yet — the section is a single "configure" affordance.
      await page.getByTestId(`configure-dev-server-btn-${repoName}`).click();

      // Fill exactly one service.
      await page.getByTestId(`dev-server-service-name-${repoName}-0`).fill("web");
      await page.getByTestId(`dev-server-service-command-${repoName}-0`).fill(SERVICE_COMMAND_LINE);
      await page
        .getByTestId(`dev-server-service-port-${repoName}-0`)
        .fill(String(SETTINGS_BASE_PORT));
      await page.getByTestId(`dev-server-service-readiness-path-${repoName}-0`).fill("/");

      await page.getByTestId(`save-dev-server-btn-${repoName}`).click();

      // REST confirms the persisted config.
      await expect
        .poll(async () => (await getDevServer(page.request, state.projectId, repoName)) !== null, {
          timeout: 15_000,
        })
        .toBe(true);
      const saved = await getDevServer(page.request, state.projectId, repoName);
      expect(saved).not.toBeNull();
      expect(saved?.services).toHaveLength(1);
      const svc = saved?.services[0];
      expect(svc?.name).toBe("web");
      expect(svc?.command).toEqual(SERVICE_COMMAND_TOKENS);
      expect(svc?.base_port).toBe(SETTINGS_BASE_PORT);
      expect(svc?.readiness?.path).toBe("/");

      // A fresh page load rebuilds the editor from the saved config.
      await page.reload();
      await expect(page.getByTestId(`dev-server-service-name-${repoName}-0`)).toHaveValue("web");
      await expect(page.getByTestId(`dev-server-service-command-${repoName}-0`)).toHaveValue(
        SERVICE_COMMAND_LINE,
      );
      await expect(page.getByTestId(`dev-server-service-port-${repoName}-0`)).toHaveValue(
        String(SETTINGS_BASE_PORT),
      );
      await expect(page.getByTestId(`dev-server-service-readiness-path-${repoName}-0`)).toHaveValue(
        "/",
      );
    });

    test("3. capture and discard a browser login session", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      await page.goto(`/projects/${state.projectId}/repos`);

      // Start: the host launches the dev server + a (headless in CI) login
      // browser; the UI flips to the in-progress affordances.
      await page.getByTestId(`browser-login-start-btn-${repoName}`).click();
      await expect(page.getByTestId(`browser-login-finish-btn-${repoName}`)).toBeVisible({
        timeout: 60_000,
      });

      // Finish: storage_state is saved and the capture closes.
      await page.getByTestId(`browser-login-finish-btn-${repoName}`).click();
      await expect(page.getByTestId(`browser-auth-captured-${repoName}`)).toBeVisible({
        timeout: 60_000,
      });
      const authRes = await page.request.get(
        `/api/projects/${state.projectId}/repos/${repoName}/browser-auth`,
      );
      expect(((await authRes.json()) as { exists: boolean }).exists).toBe(true);

      // Discard: back to the clean state.
      await page.getByTestId(`browser-auth-discard-btn-${repoName}`).click();
      await expect(page.getByTestId(`browser-auth-captured-${repoName}`)).not.toBeVisible({
        timeout: 30_000,
      });
      await expect
        .poll(async () => {
          const res = await page.request.get(
            `/api/projects/${state.projectId}/repos/${repoName}/browser-auth`,
          );
          return ((await res.json()) as { exists: boolean }).exists;
        })
        .toBe(false);
    });

    test("4. remove the config; the editor collapses and REST clears", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      await page.goto(`/projects/${state.projectId}/repos`);

      // The saved config renders the expanded editor with a remove button.
      await page.getByTestId(`remove-dev-server-btn-${repoName}`).click();

      // The section collapses back to the "configure" affordance…
      await expect(page.getByTestId(`configure-dev-server-btn-${repoName}`)).toBeVisible({
        timeout: 15_000,
      });

      // …and the API reports the config as cleared.
      await expect
        .poll(async () => await getDevServer(page.request, state.projectId, repoName), {
          timeout: 15_000,
        })
        .toBeNull();
    });
  });

// ---------------------------------------------------------------------------
// Group B — agent flow: worker really opens the dev server (repo "site")
// ---------------------------------------------------------------------------

test.describe
  .serial("Browser verify B — worker opens the trial's dev server for real", () => {
    const state = { projectId: "", epicId: "", workerThreadId: "", servedOrigin: "" };
    const repoName = "site";

    test("1. create project and seed the dev-server config via REST", async ({ page }) => {
      state.projectId = await createProject(
        page,
        "browser-verify-agent",
        BROWSER_VERIFY_SEED.repoDirAgent,
      );

      const putRes = await page.request.put(
        `/api/projects/${state.projectId}/repos/${repoName}/dev-server`,
        {
          data: {
            services: [
              {
                name: "web",
                command: SERVICE_COMMAND_TOKENS,
                cwd: ".",
                base_port: AGENT_BASE_PORT,
                readiness: { path: "/", timeout_seconds: 60 },
                env: {},
              },
            ],
            browser: { allowed_origins: [], allow_common_cdns: true },
          },
        },
      );
      expect(putRes.ok(), `PUT dev-server should succeed: ${putRes.status()}`).toBeTruthy();
      const repo = (await putRes.json()) as WireRepo;
      expect(repo.dev_server?.services[0]?.name).toBe("web");
    });

    test("2. create epic and start the run", async ({ page }) => {
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}`);
      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.getByTestId("epic-title-input").fill("Browser verify epic");
      await page
        .getByTestId("epic-description-input")
        .fill("Verify the site in a real headless browser.");
      await page.getByTestId("epic-ac-input").fill("The page shows the committed heading.");
      await page.getByTestId("form-dialog-submit").click();

      await page.waitForURL(/\/projects\/[^/]+\/epics\/[^/]+/, { timeout: 15_000 });
      state.epicId = page.url().match(/\/epics\/([^/?#]+)/)?.[1] ?? "";
      expect(state.epicId).toBeTruthy();

      await page.getByTestId("start-run-btn").click();
      await expect(page).toHaveURL(/\/threads\/manager/, { timeout: 15_000 });
    });

    test("3. work is done — run parks in waiting with T1 done", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      // Standard work-done wait. browser_open really launches the declared
      // http.server + headless Chromium on the API host, so allow extra time.
      await waitForWorkDone(page, state.projectId, state.epicId, { timeout: 150_000 });

      // Worker-only dispatch: one resolved worker thread on T1, no evaluator.
      const threadsRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}/threads`,
      );
      expect(threadsRes.status()).toBe(200);
      const threads = (await threadsRes.json()) as Array<{
        id: string;
        role: string;
        status: string;
        task: string | null;
      }>;
      const workerThreads = threads.filter((t) => t.role === "worker");
      expect(workerThreads).toHaveLength(1);
      expect(workerThreads[0].status).toBe("resolved");
      expect(workerThreads[0].task).toBe("T1");
      expect(threads.filter((t) => t.role === "evaluator")).toHaveLength(0);
      state.workerThreadId = workerThreads[0].id;
    });

    test("4. REST: browser_open/browser_read succeeded against the real page", async ({ page }) => {
      expect(state.workerThreadId).toBeTruthy();

      const msgRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}/threads/${state.workerThreadId}`,
      );
      expect(msgRes.status()).toBe(200);
      const messages = (await msgRes.json()) as WireMessage[];

      // browser_open: the host started the dev server, Chromium loaded the
      // committed index.html, and the snapshot carries the real heading.
      const open = findToolCall(messages, "browser_open");
      expect(open, "the worker timeline must contain a browser_open call").not.toBeNull();
      expect(open?.status).toBe("success");
      expect(open?.resultText).toContain(`title: ${FIXTURE_TITLE}`);
      expect(open?.resultText).toContain(FIXTURE_HEADING);

      // browser_read: a fresh snapshot of the same live page.
      const read = findToolCall(messages, "browser_read");
      expect(read, "the worker timeline must contain a browser_read call").not.toBeNull();
      expect(read?.status).toBe("success");
      expect(read?.resultText).toContain(FIXTURE_HEADING);

      // Stash the real served origin for the stop-hook assertion below.
      const urlMatch = open?.resultText.match(/url: (http:\/\/127\.0\.0\.1:\d+)/);
      expect(urlMatch, "browser_open result should carry the served url").not.toBeNull();
      state.servedOrigin = urlMatch?.[1] ?? "";
    });

    test("5. UI: worker timeline shows both tool rows as succeeded", async ({ page }) => {
      expect(state.workerThreadId).toBeTruthy();

      await page.goto(
        `/projects/${state.projectId}/epics/${state.epicId}/threads/${state.workerThreadId}`,
      );

      // ToolCallRow renders "<name> … ✓" when done and "▲ error" on failure.
      const openRow = page.locator("button").filter({ hasText: "browser_open" }).first();
      await expect(openRow).toBeVisible({ timeout: 30_000 });
      await expect(openRow).toContainText("✓");
      await expect(openRow).not.toContainText("error");

      const readRow = page.locator("button").filter({ hasText: "browser_read" }).first();
      await expect(readRow).toBeVisible({ timeout: 10_000 });
      await expect(readRow).toContainText("✓");
      await expect(readRow).not.toContainText("error");

      // The worker's final report (what it saw) is visible in the timeline.
      await expect(page.getByText(/Verified in the browser/)).toBeVisible({ timeout: 10_000 });

      await page.screenshot({
        path: "test-results/browser-verify-worker-timeline.png",
        fullPage: true,
      });
    });

    test("6. UI: manager summary is visible in the conversation", async ({ page }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/threads/manager`, {
        waitUntil: "domcontentloaded",
      });
      await expect(page.getByText(BROWSER_VERIFY_SUMMARY_TEXT)).toBeVisible({ timeout: 30_000 });
    });

    test("7. stop hook: stopping the run stops the trial's dev server", async ({ page }) => {
      // A run parked in "waiting" stays LIVE and keeps its dev server up (so the
      // next turn reuses it). The run-end stop hook (orchestrator finally →
      // stop_for_epic) fires on an actual stop — exercise that wiring end to end.
      expect(state.servedOrigin).toBeTruthy();

      // The server is still serving while the run is parked (waiting ≠ ended).
      const beforeStop = await page.request
        .get(`${state.servedOrigin}/`, { timeout: 3_000 })
        .then((r) => r.ok())
        .catch(() => false);
      expect(beforeStop, "dev server should still serve while the run is parked").toBe(true);

      const stopRes = await page.request.post(
        `/api/projects/${state.projectId}/epics/${state.epicId}/run/stop`,
      );
      expect(stopRes.ok() || stopRes.status() === 404).toBeTruthy();

      // After the stop, the finally's stop_for_epic must have torn the server down.
      await expect
        .poll(
          async () => {
            try {
              await page.request.get(`${state.servedOrigin}/`, { timeout: 2_000 });
              return "still-serving";
            } catch {
              return "stopped";
            }
          },
          { timeout: 30_000, intervals: [500, 1000, 2000] },
        )
        .toBe("stopped");
    });
  });

// ---------------------------------------------------------------------------
// Group C — manager flow: the Manager browser-verifies the scenario itself
// (repo "portal", served from the BASE CHECKOUT — nothing is dispatched)
// ---------------------------------------------------------------------------

test.describe
  .serial("Browser verify C — manager verifies the user's scenario directly", () => {
    const state = { projectId: "", epicId: "" };
    const repoName = "portal";

    test("1. create project and seed the dev-server config via REST", async ({ page }) => {
      state.projectId = await createProject(
        page,
        "browser-verify-manager",
        BROWSER_VERIFY_SEED.repoDirManager,
      );

      const putRes = await page.request.put(
        `/api/projects/${state.projectId}/repos/${repoName}/dev-server`,
        {
          data: {
            services: [
              {
                name: "web",
                command: SERVICE_COMMAND_TOKENS,
                cwd: ".",
                base_port: MANAGER_BASE_PORT,
                readiness: { path: "/", timeout_seconds: 60 },
                env: {},
              },
            ],
            browser: { allowed_origins: [], allow_common_cdns: true },
          },
        },
      );
      expect(putRes.ok(), `PUT dev-server should succeed: ${putRes.status()}`).toBeTruthy();
    });

    test("2. create epic carrying the scenario and start the run", async ({ page }) => {
      expect(state.projectId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}`);
      await page.getByTestId("new-epic-btn").first().click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.getByTestId("epic-title-input").fill("Manager browser-verifies a scenario");
      await page.getByTestId("epic-description-input").fill(MANAGER_SCENARIO_DESCRIPTION);
      await page.getByTestId("epic-ac-input").fill("Every scenario step is verified as met.");
      await page.getByTestId("form-dialog-submit").click();

      await page.waitForURL(/\/projects\/[^/]+\/epics\/[^/]+/, { timeout: 15_000 });
      state.epicId = page.url().match(/\/epics\/([^/?#]+)/)?.[1] ?? "";
      expect(state.epicId).toBeTruthy();

      await page.getByTestId("start-run-btn").click();
      await expect(page).toHaveURL(/\/threads\/manager/, { timeout: 15_000 });
    });

    test("3. manager verified directly — no worker/evaluator, tools saw the page", async ({
      page,
    }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      // browser_open really launches the declared http.server (on the repo's
      // base checkout — no dispatch, so no worktree) + headless Chromium.
      // waitForWorkDone does not apply: the Manager creates NO tasks here, so
      // the standard turn-end wait (run parked in "waiting") is the condition.
      await waitForRunWaiting(page, state.projectId, state.epicId, { timeout: 150_000 });

      // The Manager did the verification itself: no delegation happened.
      const threadsRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}/threads`,
      );
      expect(threadsRes.status()).toBe(200);
      const threads = (await threadsRes.json()) as Array<{ role: string }>;
      expect(threads.filter((t) => t.role === "worker")).toHaveLength(0);
      expect(threads.filter((t) => t.role === "evaluator")).toHaveLength(0);

      // Manager timeline via REST: both tools succeeded against the real page.
      const msgRes = await page.request.get(
        `/api/projects/${state.projectId}/epics/${state.epicId}/threads/manager`,
      );
      expect(msgRes.status()).toBe(200);
      const messages = (await msgRes.json()) as WireMessage[];

      const open = findToolCall(messages, "browser_open");
      expect(open, "the manager timeline must contain a browser_open call").not.toBeNull();
      expect(open?.status).toBe("success");
      expect(open?.resultText).toContain(`title: ${FIXTURE_TITLE}`);
      expect(open?.resultText).toContain(FIXTURE_HEADING);

      const read = findToolCall(messages, "browser_read");
      expect(read, "the manager timeline must contain a browser_read call").not.toBeNull();
      expect(read?.status).toBe("success");
      expect(read?.resultText).toContain(FIXTURE_HEADING);
    });

    test("4. UI: manager conversation shows the tool rows and the per-step report", async ({
      page,
    }) => {
      expect(state.projectId).toBeTruthy();
      expect(state.epicId).toBeTruthy();

      await page.goto(`/projects/${state.projectId}/epics/${state.epicId}/threads/manager`, {
        waitUntil: "domcontentloaded",
      });

      const openRow = page.locator("button").filter({ hasText: "browser_open" }).first();
      await expect(openRow).toBeVisible({ timeout: 30_000 });
      await expect(openRow).toContainText("✓");
      await expect(openRow).not.toContainText("error");

      const readRow = page.locator("button").filter({ hasText: "browser_read" }).first();
      await expect(readRow).toBeVisible({ timeout: 10_000 });
      await expect(readRow).toContainText("✓");
      await expect(readRow).not.toContainText("error");

      // The Manager's per-step scenario report is the user-facing deliverable.
      await expect(page.getByText(/Scenario verified directly in the browser/)).toBeVisible({
        timeout: 10_000,
      });

      await page.screenshot({
        path: "test-results/browser-verify-manager-timeline.png",
        fullPage: true,
      });
    });
  });
