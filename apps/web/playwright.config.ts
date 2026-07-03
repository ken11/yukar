import fs from "node:fs";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { FAKE_SCRIPT, SEED } from "./e2e/seed";

// Repo root is two levels up from apps/web
const REPO_ROOT = path.resolve(__dirname, "../..");

// ---- Write settings.yaml synchronously at config-parse time ----
// Playwright starts webServers BEFORE globalSetup runs (plugin tasks come first
// in the task queue). We therefore ensure the isolated config dir + settings
// exist as soon as the config module is evaluated, so uvicorn finds them.
fs.mkdirSync(SEED.configDir, { recursive: true });
fs.mkdirSync(SEED.workspaceDir, { recursive: true });
const SETTINGS_YAML = `${[
  `workspace_root: "${SEED.workspaceDir}"`,
  "llm:",
  "  provider: fake",
  "embedding:",
  "  provider: fake",
].join("\n")}\n`;
fs.writeFileSync(path.join(SEED.configDir, "settings.yaml"), SETTINGS_YAML, "utf8");

export default defineConfig({
  testDir: "./e2e",
  /* Exclude specs that require dedicated fake-script configs */
  testIgnore: [
    "**/ask-user.spec.ts",
    "**/plan-gate.spec.ts",
    "**/streaming.spec.ts",
    "**/worker-failure.spec.ts",
    "**/budget.spec.ts",
    "**/pause-resume-stop.spec.ts",
    "**/hitl-reply.spec.ts",
    "**/evaluator-reject.spec.ts",
    "**/arbiter-merge.spec.ts",
    "**/conflict-resolve.spec.ts",
    "**/docs.spec.ts",
    "**/lifecycle-buttons.spec.ts",
    "**/notifications.spec.ts",
    "**/reindex.spec.ts",
    "**/repos-crud.spec.ts",
  ],
  /* Run tests in files in parallel */
  fullyParallel: false,
  /* Fail the build on CI if you accidentally left test.only in the source code. */
  forbidOnly: !!process.env.CI,
  /* No retries — fake LLM is deterministic */
  retries: 0,
  /* Single worker — servers are shared */
  workers: 1,
  reporter: [["list"], ["html", { outputFolder: "playwright-report", open: "never" }]],

  /* Generous timeouts: fake run should complete in <30s but leave room for Next.js startup */
  timeout: 120_000,
  expect: { timeout: 60_000 },

  use: {
    baseURL: "http://127.0.0.1:3000",
    /* Collect trace on first retry */
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",

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
      // YUKAR_CONFIG_DIR points to the isolated config dir we wrote above.
      // YUKAR_FAKE_SCRIPT / YUKAR_FAKE_SLEEP are picked up by FakeModel.from_env().
      env: {
        ...process.env,
        YUKAR_CONFIG_DIR: SEED.configDir,
        YUKAR_FAKE_SCRIPT: FAKE_SCRIPT,
        // Pre-dates the plan-approval gate; scripted Manager dispatches without
        // a simulated user approval, so disable the gate for this scenario.
        YUKAR_REQUIRE_PLAN_APPROVAL: "0",
        YUKAR_FAKE_SLEEP: "0",
      },
    },
    // ---- Next.js dev server (port 3000) ----
    {
      command: "pnpm dev",
      cwd: path.join(REPO_ROOT, "apps/web"),
      url: "http://127.0.0.1:3000",
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
