/**
 * Seed constants and Fake script dedicated to the conflict-resolve scenario.
 *
 * Scenario:
 *   1. Epic run: Worker writes conflict.txt with the "EPIC" version. Evaluator accepts.
 *      Host commits.
 *   2. Inside the spec, commit a "MAIN" version of conflict.txt to the default branch (main).
 *      → epic branch and main diverge on the same line of the same file → conflict on merge.
 *   3. UI: epic ⇔ default diff → Merge to default → 409 conflict.
 *   4. UI: Resolve with Agent → POST /git/resolve → resolve run starts.
 *   5. Resolve worker: writes conflict.txt with the "RESOLVED" version, then git_add → git_commit.
 *      (MERGE_HEAD is removed and merge_in_progress becomes false)
 *   6. Resolve run completed → UI: Merge to default again → succeeds → epic = merged.
 *
 * Worker script uses the per_call format:
 *   per_call[0] = epic run worker (writes conflict.txt with EPIC version)
 *   per_call[1] = resolve run worker (writes RESOLVED version, git_add+commit to complete merge)
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-conflict-resolve");

export const CONFLICT_RESOLVE_SEED = {
  /** Temporary root */
  base,
  /** YUKAR_CONFIG_DIR — contains settings.yaml */
  configDir: path.join(base, "config"),
  /** workspace_root in settings.yaml */
  workspaceDir: path.join(base, "workspace"),
  /** Local git repo that acts as the managed repo */
  repoDir: path.join(base, "repo", "myrepo"),
} as const;

export const CONFLICT_RESOLVE_FAKE_SCRIPT = JSON.stringify({
  manager: [
    {
      type: "tool_use",
      tool_name: "task_update",
      tool_input: {
        task_id: "T1",
        title: "Write conflict.txt",
        status: "todo",
        repo: "myrepo",
        contract: "Create conflict.txt with EPIC content.",
      },
    },
    {
      type: "tool_use",
      tool_name: "dispatch",
      tool_input: { items: [{ task_id: "T1", repo: "myrepo" }] },
    },
    {
      type: "tool_use",
      tool_name: "complete_epic",
      tool_input: {},
    },
    { type: "text", text: "Epic complete." },
  ],

  // per_call format:
  //   call 0 = epic run worker
  //   call 1 = resolve run worker
  worker: {
    per_call: [
      // ---- call 0: epic run ----
      // Write conflict.txt with the EPIC version (host commits on evaluator accept)
      [
        {
          type: "tool_use",
          tool_name: "fs_write",
          tool_input: {
            path: "conflict.txt",
            content: "line1\nEPIC\nline3\n",
          },
        },
        {
          type: "text",
          text: "Wrote conflict.txt with EPIC content. Leaving uncommitted for host.",
        },
      ],

      // ---- call 1: resolve run ----
      // Write the resolved version (no conflict markers), then git_add → git_commit to complete the merge.
      // MERGE_HEAD is removed and merge_in_progress becomes false, allowing resolve_runner to succeed.
      [
        {
          type: "tool_use",
          tool_name: "fs_write",
          tool_input: {
            path: "conflict.txt",
            content: "line1\nRESOLVED\nline3\n",
          },
        },
        {
          type: "tool_use",
          tool_name: "git_add",
          tool_input: { paths: "conflict.txt" },
        },
        {
          type: "tool_use",
          tool_name: "git_commit",
          tool_input: { message: "Resolve merge conflict in conflict.txt" },
        },
        {
          type: "text",
          text: "Resolved conflict.txt and committed the merge. No conflict markers remain.",
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
