/**
 * Playwright config for budget exceeded E2E test.
 *
 * Injects a large usage value in the Manager's first turn to exceed the budget limit,
 * then verifies non-zero cost display, over-budget indicator, and POST /run 409 on the Usage page.
 *
 * Launch strategy:
 *   - FastAPI (8000): started with BUDGET_FAKE_SCRIPT
 *   - Next.js (3000): started with pnpm dev
 *   - YUKAR_CONFIG_DIR: BUDGET_SEED.configDir (isolated temp dir)
 */
import fs from "node:fs";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { BUDGET_FAKE_SCRIPT, BUDGET_SEED } from "./e2e/budget-seed";

// Repo root is two levels up from apps/web
const REPO_ROOT = path.resolve(__dirname, "../..");

// ---- Write settings.yaml synchronously at config-parse time ----
fs.mkdirSync(BUDGET_SEED.configDir, { recursive: true });
fs.mkdirSync(BUDGET_SEED.workspaceDir, { recursive: true });
const SETTINGS_YAML = `${[
  `workspace_root: "${BUDGET_SEED.workspaceDir}"`,
  "llm:",
  "  provider: fake",
  "embedding:",
  "  provider: fake",
].join("\n")}\n`;
fs.writeFileSync(path.join(BUDGET_SEED.configDir, "settings.yaml"), SETTINGS_YAML, "utf8");

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/budget.spec.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"], ["html", { outputFolder: "playwright-report/budget", open: "never" }]],

  /* Generous timeouts */
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

  globalSetup: "./e2e/budget-global-setup.ts",
  globalTeardown: "./e2e/budget-global-teardown.ts",

  webServer: [
    // ---- FastAPI (port 8000) — started with budget FAKE_SCRIPT ----
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
        YUKAR_CONFIG_DIR: BUDGET_SEED.configDir,
        YUKAR_FAKE_SCRIPT: BUDGET_FAKE_SCRIPT,
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
