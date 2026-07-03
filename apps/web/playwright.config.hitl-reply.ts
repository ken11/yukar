/**
 * Playwright config for hitl-reply (resume run via HITL reply) E2E test.
 *
 * Separated from the existing playwright.config.ts; starts FastAPI and Next.js
 * with a dedicated FAKE_SCRIPT covering ask_user → reply → completed.
 *
 * Prerequisite: ports 8000 and 3000 must be free before running.
 * (Stop any running pnpm dev first if necessary.)
 *
 * Launch strategy:
 *   - FastAPI (8000): started with HITL_REPLY_FAKE_SCRIPT
 *   - Next.js (3000): started with pnpm dev --hostname 127.0.0.1
 *   - YUKAR_CONFIG_DIR: HITL_REPLY_SEED.configDir (isolated temp dir)
 */
import fs from "node:fs";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { HITL_REPLY_FAKE_SCRIPT, HITL_REPLY_SEED } from "./e2e/hitl-reply-seed";

// Repo root is two levels up from apps/web
const REPO_ROOT = path.resolve(__dirname, "../..");

// ---- Write settings.yaml synchronously at config-parse time ----
fs.mkdirSync(HITL_REPLY_SEED.configDir, { recursive: true });
fs.mkdirSync(HITL_REPLY_SEED.workspaceDir, { recursive: true });
const SETTINGS_YAML = `${[
  `workspace_root: "${HITL_REPLY_SEED.workspaceDir}"`,
  "llm:",
  "  provider: fake",
  "embedding:",
  "  provider: fake",
].join("\n")}\n`;
fs.writeFileSync(path.join(HITL_REPLY_SEED.configDir, "settings.yaml"), SETTINGS_YAML, "utf8");

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/hitl-reply.spec.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"], ["html", { outputFolder: "playwright-report/hitl-reply", open: "never" }]],

  /* Generous timeouts: awaiting_input → reply → completed needs headroom */
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

  globalSetup: "./e2e/hitl-reply-global-setup.ts",
  globalTeardown: "./e2e/hitl-reply-global-teardown.ts",

  webServer: [
    // ---- FastAPI (port 8000) — started with HITL_REPLY_FAKE_SCRIPT ----
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
        YUKAR_CONFIG_DIR: HITL_REPLY_SEED.configDir,
        YUKAR_FAKE_SCRIPT: HITL_REPLY_FAKE_SCRIPT,
        // Pre-dates the plan-approval gate; scripted Manager dispatches without
        // a simulated user approval, so disable the gate for this scenario.
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
