/**
 * Seed constants and fake script dedicated to the Evaluator reject → Worker retry → accept cycle.
 *
 * Verification scenario:
 *   1st dispatch: Evaluator calls submit_verdict(accepted:false, feedback:"Quality issues found.")
 *   → T1 reverts to todo → Manager issues a 2nd dispatch
 *   2nd dispatch: Evaluator calls submit_verdict(accepted:true)
 *   → T1 becomes done → complete_epic → run is completed
 *
 * Manager script (per tool-call):
 *   (0) task_update(T1)           — register task
 *   (1) dispatch([{T1}])          — 1st delegation → Evaluator rejects → T1 reverts to todo
 *   (2) dispatch([{T1,feedback}]) — 2nd delegation (with feedback) → Evaluator accepts
 *   (3) complete_epic({})         — T1=done so ok:true
 *   (4) text "Retry accepted."
 *
 * Evaluator script (per_call format):
 *   per_call[0]: read_diff → submit_verdict(accepted:false, feedback:"Quality issues found.")
 *   per_call[1]: read_diff → submit_verdict(accepted:true)
 *
 * Worker script:
 *   Reuses the worker script from seed.ts (replayed from the top on each dispatch,
 *   so the same work for both attempts is fine).
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-evaluator-reject");

export const EVALUATOR_REJECT_SEED = {
  /** Temporary root */
  base,
  /** YUKAR_CONFIG_DIR — contains settings.yaml */
  configDir: path.join(base, "config"),
  /** workspace_root in settings.yaml */
  workspaceDir: path.join(base, "workspace"),
  /** Local git repo that acts as the managed repo */
  repoDir: path.join(base, "repo", "myrepo"),
} as const;

export const EVALUATOR_REJECT_FAKE_SCRIPT = JSON.stringify({
  manager: [
    // (0) register task
    {
      type: "tool_use",
      tool_name: "task_update",
      tool_input: {
        task_id: "T1",
        title: "Write hello.py",
        status: "todo",
        repo: "myrepo",
        contract: "Create hello.py that prints hello. Verify: file exists and prints 'hello'.",
      },
    },
    // (1) 1st dispatch — Evaluator rejects
    {
      type: "tool_use",
      tool_name: "dispatch",
      tool_input: { items: [{ task_id: "T1", repo: "myrepo" }] },
    },
    // (2) 2nd dispatch (with feedback) — Evaluator accepts
    {
      type: "tool_use",
      tool_name: "dispatch",
      tool_input: {
        items: [
          {
            task_id: "T1",
            repo: "myrepo",
            feedback: "Quality issues found. Please fix and retry.",
          },
        ],
      },
    },
    // (3) T1=done so complete_epic returns ok:true
    {
      type: "tool_use",
      tool_name: "complete_epic",
      tool_input: {},
    },
    { type: "text", text: "Retry accepted." },
  ],
  // Worker script is replayed from the top on each dispatch
  worker: [
    {
      type: "tool_use",
      tool_name: "fs_write",
      tool_input: { path: "hello.py", content: "def greet():\n    print('hello')\n" },
    },
    {
      type: "tool_use",
      tool_name: "repo_grep",
      tool_input: { pattern: "hello" },
    },
    {
      type: "tool_use",
      tool_name: "fs_write",
      tool_input: { path: "util.py", content: "VALUE = 42\n" },
    },
    {
      type: "text",
      text: "Implemented hello.py and util.py. Leaving changes uncommitted for host to commit on accept.",
    },
  ],
  // per_call format: the i-th evaluation consumes per_call[i]
  evaluator: {
    per_call: [
      // 1st evaluation: reject
      [
        {
          type: "tool_use",
          tool_name: "read_diff",
          tool_input: {},
        },
        {
          type: "tool_use",
          tool_name: "submit_verdict",
          tool_input: {
            accepted: false,
            feedback: "Quality issues found. Please fix and retry.",
          },
        },
        { type: "text", text: "Rejected." },
      ],
      // 2nd evaluation: accept
      [
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
        { type: "text", text: "Accepted after retry." },
      ],
    ],
  },
});
