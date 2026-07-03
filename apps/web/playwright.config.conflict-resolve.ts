/**
 * Playwright config for conflict-resolve E2E test.
 *
 * Scenario:
 *   Epic run → conflict.txt EPIC version committed.
 *   Inside the test, commit MAIN version to main → Merge to default → 409 conflict.
 *   Resolve with Agent → resolve run (per_call[1] worker) → RESOLVED version committed.
 *   Merge to default again → success → epic = merged.
 *
 * Launch strategy:
 *   - FastAPI (8000): started with CONFLICT_RESOLVE_FAKE_SCRIPT (per_call worker)
 *   - Next.js (3000): started with pnpm dev
 *   - YUKAR_CONFIG_DIR: CONFLICT_RESOLVE_SEED.configDir (isolated temp dir)
 *   - YUKAR_FAKE_SLEEP: "0" (no delay)
 */
import fs from "node:fs";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { CONFLICT_RESOLVE_FAKE_SCRIPT, CONFLICT_RESOLVE_SEED } from "./e2e/conflict-resolve-seed";

// Repo root is two levels up from apps/web
const REPO_ROOT = path.resolve(__dirname, "../..");

// ---- Write settings.yaml synchronously at config-parse time ----
// Playwright starts webServers BEFORE globalSetup runs.
// We therefore ensure the isolated config dir + settings exist
// as soon as the config module is evaluated, so uvicorn finds them.
fs.mkdirSync(CONFLICT_RESOLVE_SEED.configDir, { recursive: true });
fs.mkdirSync(CONFLICT_RESOLVE_SEED.workspaceDir, { recursive: true });
const SETTINGS_YAML = `${[
  `workspace_root: "${CONFLICT_RESOLVE_SEED.workspaceDir}"`,
  "llm:",
  "  provider: fake",
  "embedding:",
  "  provider: fake",
].join("\n")}\n`;
fs.writeFileSync(
  path.join(CONFLICT_RESOLVE_SEED.configDir, "settings.yaml"),
  SETTINGS_YAML,
  "utf8",
);

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/conflict-resolve.spec.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [
    ["list"],
    ["html", { outputFolder: "playwright-report/conflict-resolve", open: "never" }],
  ],

  /* Allow generous timeout: includes resolve run + re-merge */
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

  globalSetup: "./e2e/conflict-resolve-global-setup.ts",
  globalTeardown: "./e2e/conflict-resolve-global-teardown.ts",

  webServer: [
    // ---- FastAPI (port 8000) ----
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
        YUKAR_CONFIG_DIR: CONFLICT_RESOLVE_SEED.configDir,
        YUKAR_FAKE_SCRIPT: CONFLICT_RESOLVE_FAKE_SCRIPT,
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
