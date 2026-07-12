/**
 * Playwright config for pause/resume/stop E2E test.
 *
 * Dedicated config to verify Run pause / resume / stop in the real UI.
 *
 * YUKAR_FAKE_SLEEP=6.0 inserts a 6.0s delay between each chunk.
 * Because 6.0s > supervisor's 5s cancel timeout, stopping from "running" state
 * causes the asyncio.Task to be cancelled → CancelledError → state.status = "waiting".
 * The script alternates task_update(T1 todo) + text every turn (no dispatch):
 * each turn stays productive, keeping the run alive for a long running window.
 *
 * Launch strategy:
 *   - FastAPI (8000): started with PAUSE_RESUME_FAKE_SCRIPT
 *   - Next.js (3000): started with pnpm dev
 *   - YUKAR_CONFIG_DIR: PAUSE_RESUME_SEED.configDir (isolated temp dir)
 */
import fs from "node:fs";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { PAUSE_RESUME_FAKE_SCRIPT, PAUSE_RESUME_SEED } from "./e2e/pause-resume-stop-seed";

// Repo root is two levels up from apps/web
const REPO_ROOT = path.resolve(__dirname, "../..");

// ---- Write settings.yaml synchronously at config-parse time ----
fs.mkdirSync(PAUSE_RESUME_SEED.configDir, { recursive: true });
fs.mkdirSync(PAUSE_RESUME_SEED.workspaceDir, { recursive: true });
const SETTINGS_YAML = `${[
  `workspace_root: "${PAUSE_RESUME_SEED.workspaceDir}"`,
  "llm:",
  "  provider: fake",
  "embedding:",
  "  provider: fake",
].join("\n")}\n`;
fs.writeFileSync(path.join(PAUSE_RESUME_SEED.configDir, "settings.yaml"), SETTINGS_YAML, "utf8");

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/pause-resume-stop.spec.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [
    ["list"],
    ["html", { outputFolder: "playwright-report/pause-resume-stop", open: "never" }],
  ],

  /* Generous timeouts: pause/resume/stop each need polling headroom */
  timeout: 120_000,
  expect: { timeout: 60_000 },

  use: {
    baseURL: "http://127.0.0.1:3000",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  globalSetup: "./e2e/pause-resume-stop-global-setup.ts",
  globalTeardown: "./e2e/pause-resume-stop-global-teardown.ts",

  webServer: [
    // ---- FastAPI (port 8000) — started with pause/resume FAKE_SCRIPT ----
    {
      command: [
        "uv",
        "run",
        "--directory",
        "apps/api",
        "uvicorn",
        "yukar.app:create_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
      ].join(" "),
      cwd: REPO_ROOT,
      url: "http://127.0.0.1:8000/api/health",
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        ...process.env,
        YUKAR_CONFIG_DIR: PAUSE_RESUME_SEED.configDir,
        YUKAR_FAKE_SCRIPT: PAUSE_RESUME_FAKE_SCRIPT,
        // Pre-dates the plan-approval gate; scripted Manager dispatches without
        // a simulated user approval, so disable the gate for this scenario.
        YUKAR_REQUIRE_PLAN_APPROVAL: "0",
        // 6.0s sleep per chunk (> supervisor's 5s cancel timeout).
        // When supervisor.stop() fires from "running" state, the manager task is
        // inside asyncio.sleep(6.0).  The supervisor waits 5s then cancels the
        // asyncio.Task → CancelledError → orchestrator sets state.status = "idle".
        // 30 turns × ~3 sleeps/turn × 6s = ~540s of running headroom.
        YUKAR_FAKE_SLEEP: "6.0",
      },
    },
    // ---- Next.js dev server (port 3000) ----
    {
      command: "pnpm dev --hostname 127.0.0.1",
      cwd: path.join(REPO_ROOT, "apps/web"),
      url: "http://127.0.0.1:3000",
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
