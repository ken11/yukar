/**
 * Playwright config for MessageTurn streaming E2E test.
 *
 * Separated from the existing playwright.config.ts; starts FastAPI and Next.js
 * with a dedicated FAKE_SCRIPT that includes MessageTurn.
 *
 * Prerequisite: ports 8000 and 3000 must be free before running.
 * (Stop any running pnpm dev first if necessary.)
 *
 * Launch strategy:
 *   - FastAPI (8000): started with STREAMING_FAKE_SCRIPT
 *   - Next.js (3000): started with pnpm dev
 *   - YUKAR_CONFIG_DIR: STREAMING_SEED.configDir (isolated temp dir)
 */
import fs from "node:fs";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { STREAMING_FAKE_SCRIPT, STREAMING_SEED } from "./e2e/streaming-seed";

// Repo root is two levels up from apps/web
const REPO_ROOT = path.resolve(__dirname, "../..");

// ---- Write settings.yaml synchronously at config-parse time ----
fs.mkdirSync(STREAMING_SEED.configDir, { recursive: true });
fs.mkdirSync(STREAMING_SEED.workspaceDir, { recursive: true });
const SETTINGS_YAML = `${[
  `workspace_root: "${STREAMING_SEED.workspaceDir}"`,
  "llm:",
  "  provider: fake",
  "embedding:",
  "  provider: fake",
].join("\n")}\n`;
fs.writeFileSync(path.join(STREAMING_SEED.configDir, "settings.yaml"), SETTINGS_YAML, "utf8");

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/streaming.spec.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"], ["html", { outputFolder: "playwright-report/streaming", open: "never" }]],

  /* Generous timeouts: fake run should complete fast but leave room for Next.js startup */
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

  globalSetup: "./e2e/streaming-global-setup.ts",
  globalTeardown: "./e2e/streaming-global-teardown.ts",

  webServer: [
    // ---- FastAPI (port 8000) — started with MessageTurn FAKE_SCRIPT ----
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
        YUKAR_CONFIG_DIR: STREAMING_SEED.configDir,
        YUKAR_FAKE_SCRIPT: STREAMING_FAKE_SCRIPT,
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
