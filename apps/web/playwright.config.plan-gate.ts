/**
 * Playwright config for the plan-approval-gate E2E scenario (bug ⑤).
 *
 * Starts FastAPI (fake provider) with the plan-gate FAKE_SCRIPT and Next.js.
 * The approval gate is LEFT ON (no YUKAR_REQUIRE_PLAN_APPROVAL override) so the
 * scenario verifies that a pre-approval dispatch is rejected and a post-approval
 * dispatch runs.
 *
 * Prerequisite: ports 8000 and 3000 must be free before running.
 */
import fs from "node:fs";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { PLAN_GATE_FAKE_SCRIPT, PLAN_GATE_SEED } from "./e2e/plan-gate-seed";

const REPO_ROOT = path.resolve(__dirname, "../..");

// ---- Write settings.yaml synchronously at config-parse time ----
fs.mkdirSync(PLAN_GATE_SEED.configDir, { recursive: true });
fs.mkdirSync(PLAN_GATE_SEED.workspaceDir, { recursive: true });
const SETTINGS_YAML = `${[
  `workspace_root: "${PLAN_GATE_SEED.workspaceDir}"`,
  "llm:",
  "  provider: fake",
  "embedding:",
  "  provider: fake",
].join("\n")}\n`;
fs.writeFileSync(path.join(PLAN_GATE_SEED.configDir, "settings.yaml"), SETTINGS_YAML, "utf8");

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/plan-gate.spec.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"], ["html", { outputFolder: "playwright-report/plan-gate", open: "never" }]],

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

  globalSetup: "./e2e/plan-gate-global-setup.ts",
  globalTeardown: "./e2e/plan-gate-global-teardown.ts",

  webServer: [
    // ---- FastAPI (port 8000) — plan-gate FAKE_SCRIPT, gate ON ----
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
        YUKAR_CONFIG_DIR: PLAN_GATE_SEED.configDir,
        YUKAR_FAKE_SCRIPT: PLAN_GATE_FAKE_SCRIPT,
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
