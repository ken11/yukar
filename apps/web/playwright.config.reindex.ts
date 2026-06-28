/**
 * Playwright config for reindex E2E test.
 *
 * Verifies that clicking the reindex button on the Repos page
 * transitions the index status badge to a terminal state.
 */
import fs from "node:fs";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { REINDEX_FAKE_SCRIPT, REINDEX_SEED } from "./e2e/reindex-seed";

const REPO_ROOT = path.resolve(__dirname, "../..");

fs.mkdirSync(REINDEX_SEED.configDir, { recursive: true });
fs.mkdirSync(REINDEX_SEED.workspaceDir, { recursive: true });
const SETTINGS_YAML = `${[
  `workspace_root: "${REINDEX_SEED.workspaceDir}"`,
  "llm:",
  "  provider: fake",
  "embedding:",
  "  provider: fake",
].join("\n")}\n`;
fs.writeFileSync(path.join(REINDEX_SEED.configDir, "settings.yaml"), SETTINGS_YAML, "utf8");

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/reindex.spec.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"], ["html", { outputFolder: "playwright-report/reindex", open: "never" }]],

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

  globalSetup: "./e2e/reindex-global-setup.ts",
  globalTeardown: "./e2e/reindex-global-teardown.ts",

  webServer: [
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
        YUKAR_CONFIG_DIR: REINDEX_SEED.configDir,
        YUKAR_FAKE_SCRIPT: REINDEX_FAKE_SCRIPT,
        YUKAR_FAKE_SLEEP: "0",
      },
    },
    {
      command: "pnpm dev --hostname 127.0.0.1",
      cwd: path.join(REPO_ROOT, "apps/web"),
      url: "http://127.0.0.1:3000",
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
