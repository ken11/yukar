/**
 * Playwright config for Arbiter merge (bulk merge of multiple Epics) E2E test.
 *
 * Scenario:
 *   Run two Epics to completion using Fake runs, then bulk-merge them from the Epics board.
 *   MergeProgressPanel shows progress via SSE; verify both Epics become "merged".
 *
 * Launch strategy:
 *   - FastAPI (8000): started with ARBITER_MERGE_FAKE_SCRIPT (per_call format worker)
 *   - Next.js (3000): started with pnpm dev
 *   - YUKAR_CONFIG_DIR: ARBITER_MERGE_SEED.configDir (isolated temp dir)
 *   - YUKAR_FAKE_SLEEP: "0" (no delay)
 */
import fs from "node:fs";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { ARBITER_MERGE_FAKE_SCRIPT, ARBITER_MERGE_SEED } from "./e2e/arbiter-merge-seed";

// Repo root is two levels up from apps/web
const REPO_ROOT = path.resolve(__dirname, "../..");

// ---- Write settings.yaml synchronously at config-parse time ----
fs.mkdirSync(ARBITER_MERGE_SEED.configDir, { recursive: true });
fs.mkdirSync(ARBITER_MERGE_SEED.workspaceDir, { recursive: true });
const SETTINGS_YAML = `${[
  `workspace_root: "${ARBITER_MERGE_SEED.workspaceDir}"`,
  "llm:",
  "  provider: fake",
  "embedding:",
  "  provider: fake",
].join("\n")}\n`;
fs.writeFileSync(path.join(ARBITER_MERGE_SEED.configDir, "settings.yaml"), SETTINGS_YAML, "utf8");

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/arbiter-merge.spec.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [
    ["list"],
    ["html", { outputFolder: "playwright-report/arbiter-merge", open: "never" }],
  ],

  /* Allow generous timeout: 2 Epic × run + arbiter merge takes time */
  timeout: 240_000,
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

  globalSetup: "./e2e/arbiter-merge-global-setup.ts",
  globalTeardown: "./e2e/arbiter-merge-global-teardown.ts",

  webServer: [
    // ---- FastAPI (port 8000) — started with arbiter merge FAKE_SCRIPT ----
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
        YUKAR_CONFIG_DIR: ARBITER_MERGE_SEED.configDir,
        YUKAR_FAKE_SCRIPT: ARBITER_MERGE_FAKE_SCRIPT,
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
