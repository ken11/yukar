/**
 * Seed constants and fake script for the plan-approval-gate scenario (bug ⑤).
 *
 * This scenario proves the host-enforced approval gate: the Manager attempts to
 * `dispatch` BEFORE the user has approved the plan, and the host REJECTS it, so
 * no Worker runs and the task stays `todo`. Only after the user approves (reply
 * to `ask_user`) does the retried `dispatch` actually run the Worker.
 *
 * Manager script layout (FakeModel cursor advances across turns):
 *   script[0]: task_update(T1)     — creates the plan (plan_approved=False)
 *   script[1]: dispatch(T1)        — PRE-approval → host rejects it (NO worker,
 *                                    T1 stays "todo") — the gate in action
 *   script[2]: ask_user(...)       — present the plan → run enters awaiting_input
 *   script[3]: text (end_turn)     — stops the Strands loop so the orchestrator
 *                                    for-loop detects _awaiting_user and blocks
 *   --- turns executed after the user's reply (turn 1) ---
 *   script[4]: dispatch(T1)        — POST-approval → host allows → Worker+Evaluator
 *   script[5]: complete_epic
 *   script[6]: text "Epic complete."
 *
 * Because the gate blocks script[1], T1 is still "todo" while awaiting_input;
 * after approval, script[4] runs the Worker and T1 reaches "done".
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-plan-gate");

export const PLAN_GATE_SEED = {
  /** Temporary root */
  base,
  /** YUKAR_CONFIG_DIR — contains settings.yaml */
  configDir: path.join(base, "config"),
  /** workspace_root in settings.yaml */
  workspaceDir: path.join(base, "workspace"),
  /** Local git repo that acts as the managed repo */
  repoDir: path.join(base, "repo", "myrepo"),
} as const;

export const PLAN_GATE_QUESTION = "この計画で進めてよいですか？";

export const PLAN_GATE_FAKE_SCRIPT = JSON.stringify({
  manager: [
    // Turn 0: plan a task…
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
    // …then PREMATURELY try to dispatch before asking the user. The host gate
    // rejects this: no Worker runs and T1 stays "todo".
    {
      type: "tool_use",
      tool_name: "dispatch",
      tool_input: { items: [{ task_id: "T1", repo: "myrepo" }] },
    },
    // Present the plan to the user and wait.
    {
      type: "tool_use",
      tool_name: "ask_user",
      tool_input: { question: PLAN_GATE_QUESTION },
    },
    // end_turn — stops the Strands loop; orchestrator blocks on awaiting_input.
    { type: "text", text: "ユーザーの承認をお待ちしています。" },
    // ---- After the user's reply (turn 1): dispatch is now allowed ----
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
    { type: "text", text: "Implemented hello.py." },
  ],
  evaluator: [
    { type: "tool_use", tool_name: "read_diff", tool_input: {} },
    {
      type: "tool_use",
      tool_name: "submit_verdict",
      tool_input: { accepted: true, feedback: "" },
    },
    { type: "text", text: "Accepted." },
  ],
});
