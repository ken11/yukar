/**
 * Playwright config for the worker-only dispatch E2E test (P6).
 *
 * Dedicated config to verify in a real browser that dispatch(agents=["worker"])
 * runs only the Worker: no Evaluator thread, no host commit, the report text
 * is the deliverable and the run parks in "waiting" with the task done.
 *
 * Launch strategy:
 *   - FastAPI (8000): started with WORKER_ONLY_FAKE_SCRIPT (no evaluator script)
 *   - Next.js (3000): started with pnpm dev
 *   - YUKAR_CONFIG_DIR: WORKER_ONLY_SEED.configDir (isolated temp dir)
 */
import fs from "node:fs";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { WORKER_ONLY_FAKE_SCRIPT, WORKER_ONLY_SEED } from "./e2e/worker-only-seed";

// Repo root is two levels up from apps/web
const REPO_ROOT = path.resolve(__dirname, "../..");

// ---- Write settings.yaml synchronously at config-parse time ----
fs.mkdirSync(WORKER_ONLY_SEED.configDir, { recursive: true });
fs.mkdirSync(WORKER_ONLY_SEED.workspaceDir, { recursive: true });
const SETTINGS_YAML = `${[
  `workspace_root: "${WORKER_ONLY_SEED.workspaceDir}"`,
  "llm:",
  "  provider: fake",
  "embedding:",
  "  provider: fake",
].join("\n")}\n`;
fs.writeFileSync(path.join(WORKER_ONLY_SEED.configDir, "settings.yaml"), SETTINGS_YAML, "utf8");

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/worker-only.spec.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"], ["html", { outputFolder: "playwright-report/worker-only", open: "never" }]],

  timeout: 180_000,
  expect: { timeout: 90_000 },

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

  globalSetup: "./e2e/worker-only-global-setup.ts",
  globalTeardown: "./e2e/worker-only-global-teardown.ts",

  webServer: [
    // ---- FastAPI (port 8000) — started with worker-only FAKE_SCRIPT ----
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
        YUKAR_CONFIG_DIR: WORKER_ONLY_SEED.configDir,
        YUKAR_FAKE_SCRIPT: WORKER_ONLY_FAKE_SCRIPT,
        // The scripted Manager dispatches without a simulated user approval;
        // the approval gate itself is covered by the plan-gate scenario.
        YUKAR_REQUIRE_PLAN_APPROVAL: "0",
        YUKAR_FAKE_SLEEP: "0",
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
