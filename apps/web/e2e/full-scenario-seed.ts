/**
 * Seed + fake script for the full-scenario E2E (gate ON).
 *
 * One rich fake script drives the whole basic HITL flow the product is built
 * around, and is reused (each agent replays its role script from the top) for:
 *   - the same-trial continuation scenario, and
 *   - the reviewer scenario.
 *
 * Basic scenario (what the Manager script encodes):
 *   1. User requests an Epic → run starts.
 *   2. Manager plans (task_update) and asks the user to confirm (ask_user) →
 *      run parks at awaiting_input.  [Manager script turns 0]
 *   3. User asks for a revision (a plain chat reply — it does NOT approve) →
 *      Manager re-plans and re-asks.  [turn 1]
 *   4. User approves via the EXPLICIT approve-plan operation (approve-plan-btn
 *      → POST /plan/approval, snapshot-hash bound, P2) which also auto-posts
 *      the "plan approved" message that wakes the agent → Manager dispatches
 *      the Worker (the approval gate lets it through only now), the Evaluator
 *      accepts, the Manager self-checks the branch diff, then completes →
 *      run/state becomes completed (the epic stays open — only the user flips
 *      its status).  [turn 2]
 *
 * Continuation nuance: the continuation session replays this script from the
 * top, and its turn-1 re-plan reproduces a plan snapshot IDENTICAL to the one
 * approved in the first session — the recorded approval hash still matches, so
 * no re-approval is needed and a plain reply wakes the agent into the gated
 * dispatch (the spec asserts this snapshot-identity property explicitly).
 *
 * The Manager script is a FLAT list; the FakeModel cursor advances across turns.
 * A `text` turn ends the Strands loop, so the orchestrator's turn-loop observes
 * awaiting_input (after each ask_user) and blocks for the user's reply.
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-full-scenario");

export const FULL_SCENARIO_SEED = {
  base,
  configDir: path.join(base, "config"),
  workspaceDir: path.join(base, "workspace"),
  // One isolated repo per describe block (both basename "myrepo" so the fake
  // script's repo:"myrepo" resolves; separate dirs so one epic's merge cannot
  // contaminate another's epic⇔default diff).
  repoDirs: {
    sameTrial: path.join(base, "repo-same-trial", "myrepo"),
    reviewer: path.join(base, "repo-reviewer", "myrepo"),
  },
} as const;

// ask_user questions — the spec waits for these exact strings to know which
// awaiting_input state the run is parked at.
export const Q_PLAN = "初期計画です。この内容で進めてよろしいですか？";
export const Q_REVISED = "ご指摘を反映しました。この計画で進めてよろしいですか？";
export const Q_REVIEW =
  "レビューが完了しました。ブランチは Epic の意図を満たしています。マージしてよろしいですか？";

const _MANAGER_CONTRACT =
  "Create hello.py that prints hello. Verify: file exists and prints 'hello'.";

export const FULL_SCENARIO_FAKE_SCRIPT = JSON.stringify({
  manager: [
    // --- Turn 0: initial plan → ask the user to confirm ---
    {
      type: "tool_use",
      tool_name: "task_update",
      tool_input: {
        task_id: "T1",
        title: "Write hello.py",
        status: "todo",
        repo: "myrepo",
        contract: _MANAGER_CONTRACT,
      },
    },
    { type: "tool_use", tool_name: "ask_user", tool_input: { question: Q_PLAN } },
    { type: "text", text: "初期計画を提示しました。ご確認ください。" },
    // --- Turn 1 (after the user's revision request): re-plan → re-ask ---
    {
      type: "tool_use",
      tool_name: "task_update",
      tool_input: {
        task_id: "T1",
        title: "Write hello.py (documented)",
        status: "todo",
        repo: "myrepo",
        contract: _MANAGER_CONTRACT,
      },
    },
    { type: "tool_use", tool_name: "ask_user", tool_input: { question: Q_REVISED } },
    { type: "text", text: "ご指摘を反映した計画を提示しました。" },
    // --- Turn 2 (after the user's approval): dispatch → self-check → complete ---
    {
      type: "tool_use",
      tool_name: "dispatch",
      tool_input: { items: [{ task_id: "T1", repo: "myrepo" }] },
    },
    { type: "tool_use", tool_name: "read_branch_diff", tool_input: {} },
    { type: "tool_use", tool_name: "complete_epic", tool_input: {} },
    { type: "text", text: "実装が完了しました。レビューをお願いします。" },
  ],
  worker: [
    {
      type: "tool_use",
      tool_name: "fs_write",
      tool_input: { path: "hello.py", content: "def greet():\n    print('hello')\n" },
    },
    { type: "text", text: "Implemented hello.py (greet)." },
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
  // Reviewer (read-only): reads the branch diff (git) AND a file from the active
  // manager trial's worktree (fs_read — proves the worktree tools), then reports
  // to the user via ask_user (parks at awaiting_input).
  reviewer: [
    { type: "tool_use", tool_name: "read_branch_diff", tool_input: {} },
    {
      type: "tool_use",
      tool_name: "fs_read",
      tool_input: { path: "hello.py", repo: "myrepo" },
    },
    { type: "tool_use", tool_name: "ask_user", tool_input: { question: Q_REVIEW } },
    { type: "text", text: "レビュー結果を報告しました。" },
    // After the user's acknowledgement the reviewer wraps up (turn ends with no
    // ask_user → the run completes; the epic's status is untouched either way).
    { type: "text", text: "承知しました。ご確認ありがとうございました。" },
  ],
});
