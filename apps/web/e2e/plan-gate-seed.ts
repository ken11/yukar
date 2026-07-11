/**
 * Seed constants and fake script for the plan-approval-gate scenario (P2:
 * snapshot-bound approval).
 *
 * This scenario proves the host-enforced approval gate in its P2 form:
 * approval is an EXPLICIT user operation (POST /plan/approval via the
 * approve-plan-btn) bound to a hash of the task-plan snapshot — a chat reply
 * alone never grants it, and any plan change produces a new snapshot whose
 * hash no longer matches the recorded approval.
 *
 * Manager script layout (FakeModel cursor advances across turns):
 *   --- Turn 0 (run start) ---
 *   script[0]: task_update(T1)     — creates the plan (hash H1, unapproved)
 *   script[1]: dispatch(T1)        — PRE-approval → host REJECTS (no Worker,
 *                                    T1 stays "todo") — the gate in action
 *   script[2]: ask_user(Q1)        — present the plan → run parks awaiting_input
 *   script[3]: text (end_turn)
 *   --- Turn 1 (woken by the user's FIRST approval — recorded for hash H1) ---
 *   script[4]: task_update(T1 v2)  — re-titles T1 → the plan snapshot changes
 *                                    to hash H2; the recorded approval (H1) no
 *                                    longer matches
 *   script[5]: dispatch(T1)        — REJECTED again: approval is stale (H1≠H2)
 *   script[6]: ask_user(Q2)        — present the revised plan → park again
 *   script[7]: text (end_turn)
 *   --- Turn 2 (woken by the user's SECOND approval — recorded for H2) ---
 *   script[8]: dispatch(T1)        — approval matches → Worker+Evaluator run
 *   script[9]: complete_epic
 *   script[10]: text "Epic complete."
 *
 * The spec drives both approvals through the approve-plan-btn (which records
 * the approval AND posts the i18n "plan approved" user message that wakes the
 * parked agent) and proves T1 stays "todo" across BOTH rejected dispatches.
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
export const PLAN_GATE_REVISED_QUESTION = "計画を更新しました。改めて承認をお願いできますか？";

/** Titles before/after the turn-1 re-plan (the hash-changing task_update). */
export const PLAN_GATE_TITLE_V1 = "Write hello.py";
export const PLAN_GATE_TITLE_V2 = "Write hello.py (documented)";

export const PLAN_GATE_FAKE_SCRIPT = JSON.stringify({
  manager: [
    // Turn 0: plan a task…
    {
      type: "tool_use",
      tool_name: "task_update",
      tool_input: {
        task_id: "T1",
        title: PLAN_GATE_TITLE_V1,
        status: "todo",
        repo: "myrepo",
        contract: "Create hello.py that prints hello. Verify: file exists and prints 'hello'.",
      },
    },
    // …then PREMATURELY try to dispatch before the user approved. The host
    // gate rejects this: no Worker runs and T1 stays "todo".
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
    // ---- Turn 1 (after the user's FIRST approval, recorded for hash H1) ----
    // Change the plan: the snapshot hash becomes H2, so the H1 approval is
    // stale and the next dispatch must be rejected again.
    {
      type: "tool_use",
      tool_name: "task_update",
      tool_input: {
        task_id: "T1",
        title: PLAN_GATE_TITLE_V2,
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
      tool_name: "ask_user",
      tool_input: { question: PLAN_GATE_REVISED_QUESTION },
    },
    { type: "text", text: "更新した計画への再承認をお待ちしています。" },
    // ---- Turn 2 (after the user's SECOND approval, recorded for H2) ----
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
