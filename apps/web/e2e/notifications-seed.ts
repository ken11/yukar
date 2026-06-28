/**
 * Seed constants and fake script for the notifications scenario.
 *
 * Verifies that the notification badge increments on run_started / run_completed SSE events.
 * A full run completion is required, so FAKE_SCRIPT is equivalent to the one in seed.ts.
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-notifications");

export const NOTIF_SEED = {
  /** Temporary root */
  base,
  /** YUKAR_CONFIG_DIR — contains settings.yaml */
  configDir: path.join(base, "config"),
  /** workspace_root in settings.yaml */
  workspaceDir: path.join(base, "workspace"),
  /** Local git repo that acts as the managed repo */
  repoDir: path.join(base, "repo", "myrepo"),
} as const;

/**
 * Fake LLM script — scenario where the run completes and run_completed arrives via SSE.
 * Equivalent to FAKE_SCRIPT in seed.ts (task_update → dispatch → complete_epic → text).
 */
export const NOTIF_FAKE_SCRIPT = JSON.stringify({
  manager: [
    {
      type: "tool_use",
      tool_name: "task_update",
      tool_input: {
        task_id: "T1",
        title: "Write hello.py",
        status: "todo",
        repo: "myrepo",
        contract: "Create hello.py. Verify: file exists.",
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
  worker: [
    {
      type: "tool_use",
      tool_name: "fs_write",
      tool_input: { path: "hello.py", content: "print('hello')\n" },
    },
    { type: "text", text: "Done." },
  ],
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
