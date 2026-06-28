/**
 * Playwright config for Docs edit/save/reload persistence E2E test.
 *
 * No run needed. Validates the Docs PUT API and the CodeMirror editor.
 *
 * Prerequisite: ports 8000 and 3000 must be free.
 */
import fs from "node:fs";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { DOCS_FAKE_SCRIPT, DOCS_SEED } from "./e2e/docs-seed";

// Repo root is two levels up from apps/web
const REPO_ROOT = path.resolve(__dirname, "../..");

// ---- Write settings.yaml synchronously at config-parse time ----
fs.mkdirSync(DOCS_SEED.configDir, { recursive: true });
fs.mkdirSync(DOCS_SEED.workspaceDir, { recursive: true });
const SETTINGS_YAML = `${[
  `workspace_root: "${DOCS_SEED.workspaceDir}"`,
  "llm:",
  "  provider: fake",
  "embedding:",
  "  provider: fake",
].join("\n")}\n`;
fs.writeFileSync(path.join(DOCS_SEED.configDir, "settings.yaml"), SETTINGS_YAML, "utf8");

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/docs.spec.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"], ["html", { outputFolder: "playwright-report/docs", open: "never" }]],

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

  globalSetup: "./e2e/docs-global-setup.ts",
  globalTeardown: "./e2e/docs-global-teardown.ts",

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
        YUKAR_CONFIG_DIR: DOCS_SEED.configDir,
        YUKAR_FAKE_SCRIPT: DOCS_FAKE_SCRIPT,
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
