/**
 * Seed constants and fake script dedicated to the hitl-reply scenario.
 *
 * The Manager calls ask_user first, and the run enters awaiting_input.
 * When the user sends a reply from the composer, the run resumes and proceeds through
 * task_update → dispatch → complete_epic → worker → evaluator(accepted) until it
 * reaches completed.
 *
 * Verification points:
 *   - Before reply: question bubble is shown and run/state.status === "awaiting_input"
 *   - After reply:  subsequent agent bubbles appear and run reaches completed
 *
 * FakeModel Strands event_loop behaviour:
 *   - After a tool_use turn executes, Strands calls recurse_event_loop to request the next turn
 *   - Therefore, inserting a tool call immediately after ask_user causes all turns to execute
 *     before the run can enter awaiting_input
 *   - Solution: insert a text turn (end_turn) after ask_user to stop the Strands loop and
 *     return to the orchestrator's for-loop. The orchestrator detects _awaiting_user and
 *     blocks. On the next orchestrator turn (turn=1) after the user reply, FakeModel executes
 *     script[2] onward (task_update → dispatch → ...).
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
 *   script[0]: ask_user tool — run enters awaiting_input
 *   script[1]: text ("回答をお待ちしています。") — end_turn stops Strands loop,
 *              orchestrator detects _awaiting_user and blocks until reply
 *   script[2]: task_update(T1) — executed after user reply (turn=1)
 *   script[3]: dispatch(T1)
 *   script[4]: complete_epic
 *   script[5]: text "Epic complete."
 *
 * Strands event_loop mechanics:
 *   - ask_user executes → Strands recurse_event_loop → FakeModel emits script[1]=text →
 *     stop_reason=end_turn → EventLoopStopEvent → orchestrator for-loop iterates →
 *     _awaiting_user=True → _wait_for_user_input blocks
 *   - user reply arrives → turn=1 → stream_async(user_answer) → FakeModel emits
 *     script[2]=task_update → script[3]=dispatch → script[4]=complete_epic → ...
 *
 * Worker:    fs_write(hello.py) → repo_grep("hello") → fs_write(util.py) → text
 * Evaluator: read_diff → submit_verdict(accepted:true) → text
 */
export const HITL_REPLY_FAKE_SCRIPT = JSON.stringify({
  manager: [
    {
      type: "tool_use",
      tool_name: "ask_user",
      tool_input: {
        question: "この実装計画で進めてよいですか？",
      },
    },
    // Text turn (end_turn) stops the Strands loop and returns control to the
    // orchestrator's for-loop.
    // The orchestrator detects _awaiting_user=True and waits for the next turn.
    {
      type: "text",
      text: "ユーザーの回答をお待ちしています。",
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
