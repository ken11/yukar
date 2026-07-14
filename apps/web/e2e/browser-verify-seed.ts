/**
 * Seed constants and fake script for the browser verification scenario.
 *
 * Three independent groups share one backend instance:
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
 * C. Manager flow — repo "portal": the user's epic explicitly asks for a
 *    browser test of a written scenario, and the scripted MANAGER verifies it
 *    ITSELF with the repo-dispatching browser bundle (browser_open/browser_read
 *    take `repo=<name>`).  Nothing is dispatched, so no trial worktree exists —
 *    the host serves the repo's BASE CHECKOUT (the turn-0 fallback).
 *
 * The manager script uses the per_call form: manager run #0 is group B's
 * delegation script, manager run #1 is group C's direct-browser script (the
 * groups run in declaration order — workers=1, fullyParallel=false).
 *
 * Group B manager script (per model call):
 *   (0) task_update(T1)                       — register the verification task
 *   (1) dispatch([{T1, agents:["worker"]}])   — worker-only, no evaluator
 *   (2) text summary                          — end_turn → run parks in "waiting"
 *
 * Group B worker script:
 *   browser_open {} → browser_read {} → text report
 *
 * Group C manager script:
 *   browser_open {repo} → browser_read {repo} → per-step text report
 *
 * Evaluator script: intentionally ABSENT — no group ever starts one.
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
  /** Fixture repo for the manager flow (group C) — name "portal" */
  repoDirManager: path.join(base, "repo", "portal"),
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
export const MANAGER_BASE_PORT = 43230;

/** Final Manager summary — unique marker asserted by the spec. */
export const BROWSER_VERIFY_SUMMARY_TEXT =
  "Browser verification summary: the worker opened the dev server and " +
  "confirmed the 'Hello Browser Verify' heading in a real headless browser.";

/** Worker's final report — unique marker asserted by the spec. */
export const BROWSER_VERIFY_WORKER_REPORT =
  "Verified in the browser: the page titled 'Browser Verify Fixture' " +
  "shows the 'Hello Browser Verify' heading.";

/** The scenario the user writes into the epic — the explicit browser-test
 * request the Manager treats as its requirements list (group C). */
export const MANAGER_SCENARIO_DESCRIPTION =
  "Browser-test this scenario: open the top page and confirm it shows the " +
  `'${FIXTURE_HEADING}' heading.`;

/** Manager's final per-step report — unique marker asserted by the spec. */
export const MANAGER_BROWSER_REPORT =
  "Scenario verified directly in the browser: step 1 (open the top page) met — " +
  `the page titled '${FIXTURE_TITLE}' shows the '${FIXTURE_HEADING}' heading.`;

/** Manager run #0 (group B): delegate a worker-only verification task. */
const MANAGER_SCRIPT_DELEGATION = [
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
];

/** Manager run #1 (group C): verify the user's scenario itself with the
 * repo-dispatching browser bundle.  No dispatch → no worktree → the host
 * serves the repo's base checkout. */
const MANAGER_SCRIPT_DIRECT_BROWSER = [
  { type: "tool_use", tool_name: "browser_open", tool_input: { repo: "portal" } },
  { type: "tool_use", tool_name: "browser_read", tool_input: { repo: "portal" } },
  { type: "text", text: MANAGER_BROWSER_REPORT },
];

export const BROWSER_VERIFY_FAKE_SCRIPT = JSON.stringify({
  // per_call: manager run #0 = group B, manager run #1 = group C (the groups
  // run in declaration order — workers=1, fullyParallel=false).
  manager: { per_call: [MANAGER_SCRIPT_DELEGATION, MANAGER_SCRIPT_DIRECT_BROWSER] },
  // Worker: really launches the dev server + headless Chromium via the host.
  worker: [
    { type: "tool_use", tool_name: "browser_open", tool_input: {} },
    { type: "tool_use", tool_name: "browser_read", tool_input: {} },
    { type: "text", text: BROWSER_VERIFY_WORKER_REPORT },
  ],
  // No evaluator script on purpose — no group ever starts one.
});
