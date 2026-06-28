/**
 * Playwright config for notifications (run lifecycle SSE unread badge) E2E test.
 */
import fs from "node:fs";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { NOTIF_FAKE_SCRIPT, NOTIF_SEED } from "./e2e/notifications-seed";

const REPO_ROOT = path.resolve(__dirname, "../..");

fs.mkdirSync(NOTIF_SEED.configDir, { recursive: true });
fs.mkdirSync(NOTIF_SEED.workspaceDir, { recursive: true });
const SETTINGS_YAML = `${[
  `workspace_root: "${NOTIF_SEED.workspaceDir}"`,
  "llm:",
  "  provider: fake",
  "embedding:",
  "  provider: fake",
].join("\n")}\n`;
fs.writeFileSync(path.join(NOTIF_SEED.configDir, "settings.yaml"), SETTINGS_YAML, "utf8");

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/notifications.spec.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [
    ["list"],
    ["html", { outputFolder: "playwright-report/notifications", open: "never" }],
  ],

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

  globalSetup: "./e2e/notifications-global-setup.ts",
  globalTeardown: "./e2e/notifications-global-teardown.ts",

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
        YUKAR_CONFIG_DIR: NOTIF_SEED.configDir,
        YUKAR_FAKE_SCRIPT: NOTIF_FAKE_SCRIPT,
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
