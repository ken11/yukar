/**
 * Playwright config for the full-scenario E2E (gate ON).
 *
 * Drives the complete basic HITL flow (plan → revise → approve → dispatch →
 * evaluate → self-check → in_review) and then the two features built on top of
 * it: the same-trial new session (continue-on-branch after merge) and the
 * read-only Reviewer.  The plan-approval gate is LEFT ON.
 *
 * Prerequisite: ports 8000 and 3000 must be free before running.
 */
import fs from "node:fs";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { FULL_SCENARIO_FAKE_SCRIPT, FULL_SCENARIO_SEED } from "./e2e/full-scenario-seed";

const REPO_ROOT = path.resolve(__dirname, "../..");

fs.mkdirSync(FULL_SCENARIO_SEED.configDir, { recursive: true });
fs.mkdirSync(FULL_SCENARIO_SEED.workspaceDir, { recursive: true });
const SETTINGS_YAML = `${[
  `workspace_root: "${FULL_SCENARIO_SEED.workspaceDir}"`,
  "llm:",
  "  provider: fake",
  "embedding:",
  "  provider: fake",
].join("\n")}\n`;
fs.writeFileSync(path.join(FULL_SCENARIO_SEED.configDir, "settings.yaml"), SETTINGS_YAML, "utf8");

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/full-scenario.spec.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [
    ["list"],
    ["html", { outputFolder: "playwright-report/full-scenario", open: "never" }],
  ],

  timeout: 180_000,
  expect: { timeout: 60_000 },

  use: {
    baseURL: "http://127.0.0.1:3000",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },

  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],

  globalSetup: "./e2e/full-scenario-global-setup.ts",
  globalTeardown: "./e2e/full-scenario-global-teardown.ts",

  webServer: [
    // ---- FastAPI (port 8000) — full-scenario FAKE_SCRIPT, gate ON ----
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
        YUKAR_CONFIG_DIR: FULL_SCENARIO_SEED.configDir,
        YUKAR_FAKE_SCRIPT: FULL_SCENARIO_FAKE_SCRIPT,
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
