/**
 * Seed constants and fake script dedicated to the hitl-reply scenario.
 *
 * The Manager asks a question in BODY TEXT on turn 0 — the text turn ends the
 * turn, so the run parks in "waiting" (your turn).  When the user sends a
 * reply from the composer, the parked run wakes and proceeds through
 * task_update → dispatch → worker → evaluator(accepted) → report text, then
 * parks in "waiting" again with every task done.
 *
 * Verification points:
 *   - Before reply: question bubble is shown and run/state.status === "waiting"
 *   - After reply:  subsequent agent bubbles appear and the run parks in
 *     "waiting" with all tasks done (a conversation run never "completes")
 *
 * FakeModel Strands event_loop behaviour:
 *   - After a tool_use turn executes, Strands calls recurse_event_loop to
 *     request the next turn; a text turn (end_turn) stops the Strands loop and
 *     returns control to the orchestrator, which parks the run in "waiting"
 *   - On the next model call (after the user reply wakes the parked run) the
 *     FakeModel continues the script from where it left off
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-hitl-reply");

export const HITL_REPLY_SEED = {
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
 * Fake LLM script for hitl-reply scenario.
 *
 * Manager script layout:
 *   script[0]: text question — end_turn stops the Strands loop, the
 *              orchestrator parks the run in "waiting" until the reply
 *   script[1]: task_update(T1) — executed after the user reply (turn 1)
 *   script[2]: dispatch(T1)
 *   script[3]: text report — turn 1 ends, run parks in "waiting" again
 *
 * Worker:    fs_write(hello.py) → repo_grep("hello") → fs_write(util.py) → text
 * Evaluator: read_diff → submit_verdict(accepted:true) → text
 */
export const HITL_REPLY_FAKE_SCRIPT = JSON.stringify({
  manager: [
    // Turn 0: the question is plain body text → park in "waiting".
    {
      type: "text",
      text: "この実装計画で進めてよいですか？",
    },
    // ---- Turns executed after the user reply ----
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
    {
      type: "tool_use",
      tool_name: "dispatch",
      tool_input: { items: [{ task_id: "T1", repo: "myrepo" }] },
    },
    { type: "text", text: "T1 is done and accepted. Please review the branch." },
  ],
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
      text: "Implemented hello.py and util.py; confirmed 'hello' is present via repo_grep.",
    },
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
