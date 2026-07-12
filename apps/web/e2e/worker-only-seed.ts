/**
 * Seed constants and fake script for the worker-only dispatch scenario.
 *
 * Verification scenario:
 *   The Manager plans an investigation task whose deliverable is the report
 *   text itself, then dispatches it with agents=["worker"].  Only the Worker
 *   runs (read tools + a report text); no Evaluator is started, no host
 *   commit happens, and the task becomes done with the Worker's report
 *   returned to the Manager.  The Manager summarises the findings in body
 *   text and the run parks in "waiting".
 *
 * Manager script (per model call):
 *   (0) task_update(T1)                       — register the investigation task
 *   (1) dispatch([{T1, agents:["worker"]}])   — worker-only delegation
 *   (2) text summary                          — end_turn → run parks in "waiting"
 *
 * Worker script (read tools + report; no writes):
 *   fs_read(README.md) → repo_grep("myrepo") → text findings report
 *
 * Evaluator script: intentionally ABSENT — the scenario asserts that no
 * evaluator thread is ever registered (agents=["worker"] skips evaluation).
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-worker-only");

export const WORKER_ONLY_SEED = {
  /** Temporary root */
  base,
  /** YUKAR_CONFIG_DIR — contains settings.yaml */
  configDir: path.join(base, "config"),
  /** workspace_root in settings.yaml */
  workspaceDir: path.join(base, "workspace"),
  /** Local git repo that acts as the managed repo */
  repoDir: path.join(base, "repo", "myrepo"),
} as const;

/** Final Manager summary — unique marker asserted by the spec. */
export const WORKER_ONLY_SUMMARY_TEXT =
  "Investigation summary: README.md documents the myrepo project. " +
  "The deliverable is this report, so no evaluation ran and nothing was committed.";

export const WORKER_ONLY_FAKE_SCRIPT = JSON.stringify({
  manager: [
    // (0) register the investigation task
    {
      type: "tool_use",
      tool_name: "task_update",
      tool_input: {
        task_id: "T1",
        title: "Investigate repository structure",
        status: "todo",
        repo: "myrepo",
        contract:
          "Investigate the repository and report the findings in text. " +
          "The report itself is the deliverable; no file changes are expected.",
      },
    },
    // (1) worker-only delegation — no Evaluator, no host commit
    {
      type: "tool_use",
      tool_name: "dispatch",
      tool_input: { items: [{ task_id: "T1", repo: "myrepo", agents: ["worker"] }] },
    },
    // (2) summarise the Worker's report in body text → run parks in "waiting"
    { type: "text", text: WORKER_ONLY_SUMMARY_TEXT },
  ],
  // Worker: read-only investigation + report text (the deliverable)
  worker: [
    {
      type: "tool_use",
      tool_name: "fs_read",
      tool_input: { path: "README.md" },
    },
    {
      type: "tool_use",
      tool_name: "repo_grep",
      tool_input: { pattern: "myrepo" },
    },
    {
      type: "text",
      text: "Findings: README.md contains the '# myrepo' heading; the repository has a single documentation file and no source code.",
    },
  ],
  // No evaluator script on purpose — agents=["worker"] must never invoke it.
  // (If a regression starts an Evaluator anyway, it would reply "Script
  // exhausted." without a submit_verdict and the run would fail visibly.)
});
