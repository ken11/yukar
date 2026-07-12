/**
 * Playwright config for the question-reload E2E test (historically the
 * ask_user scenario; the agent now asks in its final message and parks in
 * "waiting").
 *
 * Separated from the existing playwright.config.ts; starts FastAPI and Next.js
 * with a dedicated FAKE_SCRIPT whose turn ends on a question message.
 *
 * Prerequisite: ports 8000 and 3000 must be free before running.
 * (Stop any running pnpm dev first if necessary.)
 *
 * Launch strategy:
 *   - FastAPI (8000): started with the ask-user scenario FAKE_SCRIPT
 *   - Next.js (3000): started with pnpm dev
 *   - YUKAR_CONFIG_DIR: ASK_USER_SEED.configDir (isolated temp dir)
 */
import fs from "node:fs";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { ASK_USER_FAKE_SCRIPT, ASK_USER_SEED } from "./e2e/ask-user-seed";

// Repo root is two levels up from apps/web
const REPO_ROOT = path.resolve(__dirname, "../..");

// ---- Write settings.yaml synchronously at config-parse time ----
fs.mkdirSync(ASK_USER_SEED.configDir, { recursive: true });
fs.mkdirSync(ASK_USER_SEED.workspaceDir, { recursive: true });
const SETTINGS_YAML = `${[
  `workspace_root: "${ASK_USER_SEED.workspaceDir}"`,
  "llm:",
  "  provider: fake",
  "embedding:",
  "  provider: fake",
].join("\n")}\n`;
fs.writeFileSync(path.join(ASK_USER_SEED.configDir, "settings.yaml"), SETTINGS_YAML, "utf8");

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/ask-user.spec.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"], ["html", { outputFolder: "playwright-report/ask-user", open: "never" }]],

  /* Generous timeouts: allow headroom for the REST run/state + thread-history restore and SSE backfill after reload */
  timeout: 120_000,
  expect: { timeout: 30_000 },

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

  globalSetup: "./e2e/ask-user-global-setup.ts",
  globalTeardown: "./e2e/ask-user-global-teardown.ts",

  webServer: [
    // ---- FastAPI (port 8000) — started with the ask-user scenario FAKE_SCRIPT ----
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
        YUKAR_CONFIG_DIR: ASK_USER_SEED.configDir,
        YUKAR_FAKE_SCRIPT: ASK_USER_FAKE_SCRIPT,
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
