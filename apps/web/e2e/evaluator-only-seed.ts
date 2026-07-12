/**
 * Seed constants and fake script for the evaluator-only dispatch scenario (P6).
 *
 * Verification scenario (two phases across one live parked run):
 *   Phase 1 (turn 0): the Manager registers T1 and dispatches it with
 *     agents=["worker"] — the Worker drafts hello.py in the worktree, the
 *     report makes T1 done, and NOTHING is committed (no Evaluator ran).
 *     The Manager reports the draft in body text and the run parks in
 *     "waiting".  The spec asserts: 0 commits ahead of main, no evaluator
 *     thread, hello.py present but uncommitted in the trial worktree.
 *   Phase 2 (after the user reply wakes the parked run): the Manager
 *     registers T2 ("certify the drafted changes") and dispatches it with
 *     agents=["evaluator"] — no Worker and no hermetic reset, so the drafted
 *     files ARE the evaluation subject.  The Evaluator accepts → the host
 *     commits (the one-and-only deterministic side-effect gate).  The spec
 *     asserts on the git side: exactly 1 commit ahead of main containing
 *     hello.py with the drafted content.
 *
 * Manager script (per model call):
 *   (0) task_update(T1)                          — draft task
 *   (1) dispatch([{T1, agents:["worker"]}])      — worker-only draft
 *   (2) text draft report                        — end_turn → park in "waiting"
 *   --- user reply wakes the run ---
 *   (3) task_update(T2)                          — certification task
 *   (4) dispatch([{T2, agents:["evaluator"]}])   — evaluator-only certification
 *   (5) text certified report                    — end_turn → park in "waiting"
 *
 * Worker script:  fs_write(hello.py) → report text (draft left uncommitted)
 * Evaluator:      read_diff → submit_verdict(accepted:true) → text
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-evaluator-only");

export const EVALUATOR_ONLY_SEED = {
  /** Temporary root */
  base,
  /** YUKAR_CONFIG_DIR — contains settings.yaml */
  configDir: path.join(base, "config"),
  /** workspace_root in settings.yaml */
  workspaceDir: path.join(base, "workspace"),
  /** Local git repo that acts as the managed repo */
  repoDir: path.join(base, "repo", "myrepo"),
} as const;

/** Content the Worker drafts — asserted against the host commit in phase 2. */
export const EVALUATOR_ONLY_HELLO_CONTENT = "def greet():\n    print('hello')\n";

/** Manager body text ending phase 1 (draft ready, nothing committed yet). */
export const EVALUATOR_ONLY_DRAFT_TEXT =
  "Draft ready: hello.py is written in the worktree but NOT committed (worker-only). " +
  "Reply when you want me to certify it.";

/** Manager body text ending phase 2 (certified → host committed). */
export const EVALUATOR_ONLY_CERTIFIED_TEXT =
  "Certified: the evaluator accepted the drafted changes and the host committed hello.py.";

export const EVALUATOR_ONLY_FAKE_SCRIPT = JSON.stringify({
  manager: [
    // ---- Phase 1: worker-only draft ----
    {
      type: "tool_use",
      tool_name: "task_update",
      tool_input: {
        task_id: "T1",
        title: "Draft hello.py",
        status: "todo",
        repo: "myrepo",
        contract:
          "Draft hello.py that prints hello. The draft stays uncommitted; " +
          "certification happens later as a separate evaluator-only pass.",
      },
    },
    {
      type: "tool_use",
      tool_name: "dispatch",
      tool_input: { items: [{ task_id: "T1", repo: "myrepo", agents: ["worker"] }] },
    },
    { type: "text", text: EVALUATOR_ONLY_DRAFT_TEXT },
    // ---- Phase 2 (runs after the user reply wakes the parked run) ----
    {
      type: "tool_use",
      tool_name: "task_update",
      tool_input: {
        task_id: "T2",
        title: "Certify the drafted changes",
        status: "todo",
        repo: "myrepo",
        contract:
          "Evaluate the current worktree contents: hello.py must exist and print 'hello'. " +
          "Acceptance commits the draft.",
      },
    },
    {
      type: "tool_use",
      tool_name: "dispatch",
      tool_input: { items: [{ task_id: "T2", repo: "myrepo", agents: ["evaluator"] }] },
    },
    { type: "text", text: EVALUATOR_ONLY_CERTIFIED_TEXT },
  ],
  // Worker (phase 1 only): draft hello.py, leave it uncommitted.
  worker: [
    {
      type: "tool_use",
      tool_name: "fs_write",
      tool_input: { path: "hello.py", content: EVALUATOR_ONLY_HELLO_CONTENT },
    },
    {
      type: "text",
      text: "Drafted hello.py in the worktree. Left uncommitted — certification is a separate pass.",
    },
  ],
  // Evaluator (phase 2 only): certify the staged worktree contents.
  evaluator: [
    {
      type: "tool_use",
      tool_name: "read_diff",
      tool_input: {},
    },
    {
      type: "tool_use",
      tool_name: "submit_verdict",
      tool_input: { accepted: true, feedback: "" },
    },
    { type: "text", text: "Accepted: hello.py matches the contract." },
  ],
});
