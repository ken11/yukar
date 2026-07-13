/**
 * Seed constants and fake script for the browser verification scenario.
 *
 * Two independent groups share one backend instance:
 *
 * A. Settings UI roundtrip — repo "webapp": configure one dev-server service
 *    on the Repos page, save, reload to confirm persistence, then remove.
 *    No run is started, so the fake script is never consumed by this group.
 *
 * B. Agent flow — repo "site": the dev-server config is PUT via REST, then a
 *    scripted Manager dispatches one worker-only task whose Worker really
 *    calls browser_open / browser_read.  Under provider=fake only the LLM is
 *    scripted — the tools execute for real: the host launches the declared
 *    `python3 -m http.server` inside the trial worktree and opens a headless
 *    Chromium page on it.
 *
 * Manager script (per model call):
 *   (0) task_update(T1)                       — register the verification task
 *   (1) dispatch([{T1, agents:["worker"]}])   — worker-only, no evaluator
 *   (2) text summary                          — end_turn → run parks in "waiting"
 *
 * Worker script:
 *   browser_open {} → browser_read {} → text report
 *
 * Evaluator script: intentionally ABSENT — agents=["worker"] never starts one.
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-browser-verify");

export const BROWSER_VERIFY_SEED = {
  /** Temporary root */
  base,
  /** YUKAR_CONFIG_DIR — contains settings.yaml */
  configDir: path.join(base, "config"),
  /** workspace_root in settings.yaml */
  workspaceDir: path.join(base, "workspace"),
  /** Fixture repo for the settings UI roundtrip (group A) — name "webapp" */
  repoDirSettings: path.join(base, "repo", "webapp"),
  /** Fixture repo for the agent flow (group B) — name "site" */
  repoDirAgent: path.join(base, "repo", "site"),
} as const;

/** Heading committed in the fixture index.html — the real page content the
 * headless browser must see inside the trial worktree. */
export const FIXTURE_HEADING = "Hello Browser Verify";

/** <title> committed in the fixture index.html. */
export const FIXTURE_TITLE = "Browser Verify Fixture";

/** index.html committed into both fixture repos. */
export const FIXTURE_INDEX_HTML = [
  "<!doctype html>",
  "<html>",
  "<head>",
  '  <meta charset="utf-8" />',
  `  <title>${FIXTURE_TITLE}</title>`,
  "</head>",
  "<body>",
  `  <h1>${FIXTURE_HEADING}</h1>`,
  "  <p>Served from the trial worktree.</p>",
  "</body>",
  "</html>",
  "",
].join("\n");

/** Command line typed into the settings UI (group A). */
export const SERVICE_COMMAND_LINE = "python3 -m http.server {port} --bind 127.0.0.1";

/** Exec tokens for the REST-seeded config (group B) — same command. */
export const SERVICE_COMMAND_TOKENS = [
  "python3",
  "-m",
  "http.server",
  "{port}",
  "--bind",
  "127.0.0.1",
];

/** Base ports in the 432xx range to avoid collisions (the host scans upward
 * from base_port anyway, so a stale listener never blocks the run). */
export const SETTINGS_BASE_PORT = 43210;
export const AGENT_BASE_PORT = 43220;

/** Final Manager summary — unique marker asserted by the spec. */
export const BROWSER_VERIFY_SUMMARY_TEXT =
  "Browser verification summary: the worker opened the dev server and " +
  "confirmed the 'Hello Browser Verify' heading in a real headless browser.";

/** Worker's final report — unique marker asserted by the spec. */
export const BROWSER_VERIFY_WORKER_REPORT =
  "Verified in the browser: the page titled 'Browser Verify Fixture' " +
  "shows the 'Hello Browser Verify' heading.";

export const BROWSER_VERIFY_FAKE_SCRIPT = JSON.stringify({
  manager: [
    // (0) register the verification task
    {
      type: "tool_use",
      tool_name: "task_update",
      tool_input: {
        task_id: "T1",
        title: "Verify the site in a browser",
        status: "todo",
        repo: "site",
        contract:
          "Open the repo's dev server in the browser and report what the " +
          "page shows. The report itself is the deliverable; no file " +
          "changes are expected.",
      },
    },
    // (1) worker-only delegation — no Evaluator, no host commit
    {
      type: "tool_use",
      tool_name: "dispatch",
      tool_input: { items: [{ task_id: "T1", repo: "site", agents: ["worker"] }] },
    },
    // (2) summarise the Worker's report in body text → run parks in "waiting"
    { type: "text", text: BROWSER_VERIFY_SUMMARY_TEXT },
  ],
  // Worker: really launches the dev server + headless Chromium via the host.
  worker: [
    { type: "tool_use", tool_name: "browser_open", tool_input: {} },
    { type: "tool_use", tool_name: "browser_read", tool_input: {} },
    { type: "text", text: BROWSER_VERIFY_WORKER_REPORT },
  ],
  // No evaluator script on purpose — agents=["worker"] must never invoke it.
});
