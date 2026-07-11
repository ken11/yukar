/**
 * Seed constants and fake script dedicated to the Worker failure scenario.
 *
 * The manager runs normally through task_update → dispatch,
 * then the worker's first turn is a RaiseTurn (MaxTokensReachedException) that causes it to fail.
 * Afterwards the manager reports in body text and its turn end parks the run
 * in "waiting" (P3: a conversation run never completes).
 *
 * Verification points:
 *   - The ThreadTreePanel WorkerNode shows status="failed"
 *   - The WorkerNode warning icon (status "失敗" label) is visible
 *   - run_state.status parks at "waiting" while the epic stays open
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-worker-failure");

export const WORKER_FAILURE_SEED = {
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
 * Fake LLM script for worker failure scenario.
 *
 * Manager:
 *   (0) task_update(T1)   — register task
 *   (1) dispatch(T1)      — delegate to Worker
 *   (2) text report       — Manager continues after the Worker failure; the
 *       text turn ends the turn and the run parks in "waiting"
 *
 * Worker:
 *   (0) raise MaxTokensReachedException — Worker fails immediately
 *
 * Evaluator:
 *   (empty) — Evaluator is not invoked because Worker failed
 */
export const WORKER_FAILURE_FAKE_SCRIPT = JSON.stringify({
  manager: [
    {
      type: "tool_use",
      tool_name: "task_update",
      tool_input: {
        task_id: "T1",
        title: "Write failure.py",
        status: "todo",
        repo: "myrepo",
        contract: "Create failure.py. Verify: file exists.",
      },
    },
    {
      type: "tool_use",
      tool_name: "dispatch",
      tool_input: { items: [{ task_id: "T1", repo: "myrepo" }] },
    },
    { type: "text", text: "Run finished with worker failure." },
  ],
  worker: [
    {
      type: "raise",
      exc_name: "MaxTokensReachedException",
      message: "Fake max tokens exceeded",
    },
  ],
  evaluator: [],
});

/**
 * Fake LLM script for context overflow variant.
 *
 * The pattern where the Worker fails with ContextWindowOverflowException.
 */
export const WORKER_CONTEXT_OVERFLOW_FAKE_SCRIPT = JSON.stringify({
  manager: [
    {
      type: "tool_use",
      tool_name: "task_update",
      tool_input: {
        task_id: "T2",
        title: "Write overflow.py",
        status: "todo",
        repo: "myrepo",
        contract: "Create overflow.py. Verify: file exists.",
      },
    },
    {
      type: "tool_use",
      tool_name: "dispatch",
      tool_input: { items: [{ task_id: "T2", repo: "myrepo" }] },
    },
    { type: "text", text: "Run finished with context overflow failure." },
  ],
  worker: [
    {
      type: "raise",
      exc_name: "ContextWindowOverflowException",
      message: "Fake context window overflow",
    },
  ],
  evaluator: [],
});
