/**
 * Playwright config for the browser verification E2E test.
 *
 * Three groups against one backend instance:
 *   A. Repos-page dev-server editor roundtrip (configure → save → reload →
 *      remove) — pure settings UI, no run.
 *   B. Agent flow — a scripted worker-only run whose Worker really calls
 *      browser_open / browser_read: the host launches the declared
 *      `python3 -m http.server` inside the trial worktree and drives a
 *      headless Chromium page on it (tools are real even under provider=fake).
 *   C. Manager flow — the scripted Manager verifies the user's written
 *      scenario ITSELF via the repo-dispatching browser bundle; nothing is
 *      dispatched, so the host serves the repo's base checkout.
 *
 * Launch strategy:
 *   - FastAPI (8000): started with BROWSER_VERIFY_FAKE_SCRIPT
 *   - Next.js (3000): started with pnpm dev
 *   - YUKAR_CONFIG_DIR: BROWSER_VERIFY_SEED.configDir (isolated temp dir)
 */
import fs from "node:fs";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { BROWSER_VERIFY_FAKE_SCRIPT, BROWSER_VERIFY_SEED } from "./e2e/browser-verify-seed";

// Repo root is two levels up from apps/web
const REPO_ROOT = path.resolve(__dirname, "../..");

// ---- Write settings.yaml synchronously at config-parse time ----
fs.mkdirSync(BROWSER_VERIFY_SEED.configDir, { recursive: true });
fs.mkdirSync(BROWSER_VERIFY_SEED.workspaceDir, { recursive: true });
const SETTINGS_YAML = `${[
  `workspace_root: "${BROWSER_VERIFY_SEED.workspaceDir}"`,
  "llm:",
  "  provider: fake",
  "embedding:",
  "  provider: fake",
].join("\n")}\n`;
fs.writeFileSync(path.join(BROWSER_VERIFY_SEED.configDir, "settings.yaml"), SETTINGS_YAML, "utf8");

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/browser-verify.spec.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [
    ["list"],
    ["html", { outputFolder: "playwright-report/browser-verify", open: "never" }],
  ],

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

  globalSetup: "./e2e/browser-verify-global-setup.ts",
  globalTeardown: "./e2e/browser-verify-global-teardown.ts",

  webServer: [
    // ---- FastAPI (port 8000) — started with browser-verify FAKE_SCRIPT ----
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
        YUKAR_CONFIG_DIR: BROWSER_VERIFY_SEED.configDir,
        YUKAR_FAKE_SCRIPT: BROWSER_VERIFY_FAKE_SCRIPT,
        // The scripted Manager dispatches without a simulated user approval;
        // the approval gate itself is covered by the plan-gate scenario.
        YUKAR_REQUIRE_PLAN_APPROVAL: "0",
        YUKAR_FAKE_SLEEP: "0",
        // The login-capture browser cannot open a headed window in CI — the
        // test drives the same flow headless (design §12 test hook).
        YUKAR_LOGIN_BROWSER_HEADLESS: "1",
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
