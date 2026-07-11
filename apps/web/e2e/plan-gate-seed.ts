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
 *   script[2]: text Q1 (end_turn)  — present the plan in body text → the turn
 *                                    ends and the run parks in "waiting"
 *   --- Turn 1 (woken by the user's FIRST approval — recorded for hash H1) ---
 *   script[3]: task_update(T1 v2)  — re-titles T1 → the plan snapshot changes
 *                                    to hash H2; the recorded approval (H1) no
 *                                    longer matches
 *   script[4]: dispatch(T1)        — REJECTED again: approval is stale (H1≠H2)
 *   script[5]: text Q2 (end_turn)  — present the revised plan → park again
 *   --- Turn 2 (woken by the user's SECOND approval — recorded for H2) ---
 *   script[6]: dispatch(T1)        — approval matches → Worker+Evaluator run
 *   script[7]: text report (end_turn) — park in "waiting" with T1 done
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
    // Present the plan to the user in body text — end_turn stops the Strands
    // loop and the orchestrator parks the run in "waiting".
    { type: "text", text: PLAN_GATE_QUESTION },
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
    // Present the revised plan in body text → park again.
    { type: "text", text: PLAN_GATE_REVISED_QUESTION },
    // ---- Turn 2 (after the user's SECOND approval, recorded for H2) ----
    {
      type: "tool_use",
      tool_name: "dispatch",
      tool_input: { items: [{ task_id: "T1", repo: "myrepo" }] },
    },
    { type: "text", text: "T1 is done and accepted." },
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
