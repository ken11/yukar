/**
 * Seed constants and Fake script dedicated to the Arbiter merge (bulk Epic merge) scenario.
 *
 * Scenario:
 *   - Create 2 Epics in 1 project and run each until its work is done with a
 *     Fake run (the run parks in "waiting" with all tasks done — P3).
 *   - Each Epic's Worker writes to a separate file (epic1.py / epic2.py) to
 *     ensure no merge conflict can occur.
 *     (Same file would also be conflict-free because worktrees are separate,
 *      but separate files are chosen to make the test intent explicit.)
 *   - After the work is done, select both from the Epics board → Merge Selected →
 *     MergeProgressPanel shows SSE progress and both carry the merge fact.
 *
 * Fake script (both Epics use the same YUKAR_FAKE_SCRIPT):
 *   Manager: task_update(T1) → dispatch(T1) → report text (turn ends → waiting)
 *   Worker (per_call):
 *     per_call[0] = 1st Epic run → writes epic1.py
 *     per_call[1] = 2nd Epic run → writes epic2.py
 *   Evaluator: read_diff → submit_verdict(accepted:true) → text
 *
 * Arbiter:
 *   No conflict (separate files) → Arbiter does not call LLM →
 *   arbiter key not needed.
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-arbiter-merge");

export const ARBITER_MERGE_SEED = {
  /** Temporary root */
  base,
  /** YUKAR_CONFIG_DIR — contains settings.yaml */
  configDir: path.join(base, "config"),
  /** workspace_root in settings.yaml */
  workspaceDir: path.join(base, "workspace"),
  /** Local git repo that acts as the managed repo */
  repoDir: path.join(base, "repo", "myrepo"),
} as const;

export const ARBITER_MERGE_FAKE_SCRIPT = JSON.stringify({
  manager: [
    // (0) Register task
    {
      type: "tool_use",
      tool_name: "task_update",
      tool_input: {
        task_id: "T1",
        title: "Implement feature",
        status: "todo",
        repo: "myrepo",
        contract: "Implement the feature file. Verify: file exists.",
      },
    },
    // (1) dispatch → Worker executes → Evaluator accepts
    {
      type: "tool_use",
      tool_name: "dispatch",
      tool_input: { items: [{ task_id: "T1", repo: "myrepo" }] },
    },
    // (2) T1=done → report and end the turn (the run parks in "waiting")
    { type: "text", text: "Epic work is done." },
  ],
  // per_call format: 1st run writes epic1.py, 2nd run writes epic2.py
  // This ensures the 2 Epics each commit a different file
  worker: {
    per_call: [
      // 1st Epic Worker
      [
        {
          type: "tool_use",
          tool_name: "fs_write",
          tool_input: {
            path: "epic1.py",
            content: "# Epic 1 feature\nFEATURE_1 = True\n",
          },
        },
        {
          type: "tool_use",
          tool_name: "repo_grep",
          tool_input: { pattern: "FEATURE_1" },
        },
        {
          type: "text",
          text: "Implemented epic1.py. Leaving changes uncommitted for host to commit on accept.",
        },
      ],
      // 2nd Epic Worker
      [
        {
          type: "tool_use",
          tool_name: "fs_write",
          tool_input: {
            path: "epic2.py",
            content: "# Epic 2 feature\nFEATURE_2 = True\n",
          },
        },
        {
          type: "tool_use",
          tool_name: "repo_grep",
          tool_input: { pattern: "FEATURE_2" },
        },
        {
          type: "text",
          text: "Implemented epic2.py. Leaving changes uncommitted for host to commit on accept.",
        },
      ],
    ],
  },
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
    { type: "text", text: "Accepted." },
  ],
});
