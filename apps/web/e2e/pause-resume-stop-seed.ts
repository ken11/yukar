/**
 * Seed constants and fake script dedicated to the pause/resume/stop scenario.
 *
 * YUKAR_FAKE_SLEEP=6.0 inserts a 6.0s delay between each chunk.
 * Because 6.0s > the supervisor's 5s cancel wait, stopping from the
 * running state causes the task to receive CancelledError and
 * state.status to become "idle" (the canonical idle path).
 *
 * Turn-end semantics note: a manager turn that ends without an effector tool
 * (dispatch / task_update) gets ONE stall notice; a second silent turn PARKS
 * the run in awaiting_input.  To keep a long "running" window this script
 * alternates task_update + text — every turn is productive, so the host keeps
 * sending the one-shot notice and the run never parks.
 *
 * worker / evaluator are empty (they are never called because dispatch is not invoked).
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-pause-resume");

export const PAUSE_RESUME_SEED = {
  /** Temporary root */
  base,
  /** YUKAR_CONFIG_DIR — contains settings.yaml */
  configDir: path.join(base, "config"),
  /** workspace_root in settings.yaml */
  workspaceDir: path.join(base, "workspace"),
  /** Local git repo that acts as the managed repo */
  repoDir: path.join(base, "repo", "myrepo"),
} as const;

/** One productive manager turn: refresh T1 (effector) then narrate. */
function turn(step: number, total: number, narration: string) {
  return [
    {
      type: "tool_use",
      tool_name: "task_update",
      tool_input: {
        task_id: "T1",
        title: "Implement feature A",
        status: "todo",
        repo: "myrepo",
        contract: "Implement feature A. Verify: tests pass.",
      },
    },
    { type: "text", text: `${narration} (step ${step} of ${total}).` },
  ];
}

/**
 * Fake LLM script for pause/resume/stop scenario.
 *
 * Turn layout (used together with YUKAR_FAKE_SLEEP=6.0):
 *   - Every turn: task_update keeps T1 "todo" (runnable_exists stays True,
 *     bypassing the deadlock guard) AND marks the turn productive so the
 *     turn-end semantics keep the run flowing (notice → next turn) instead
 *     of parking it in awaiting_input.
 *   - dispatch/complete_epic are never called, so the run keeps consuming
 *     manager turns until the tests pause / resume / stop it.
 *   - 15 turns × (task_update + text) × 6.0s sleeps ≈ a long running window;
 *     turns are not consumed while paused.
 *
 * Why FAKE_SLEEP=6.0 matters:
 *   supervisor.stop() waits 5s then cancels the asyncio.Task.
 *   If the manager is mid asyncio.sleep(6.0), it receives CancelledError and
 *   the orchestrator sets state.status = "idle" (the canonical idle path).
 */
const NARRATIONS = [
  "Analyzing requirements",
  "Reviewing codebase structure",
  "Identifying dependencies",
  "Planning implementation approach",
  "Estimating effort for each task",
  "Checking for potential conflicts",
  "Validating acceptance criteria",
  "Preparing task breakdown",
  "Reviewing risk factors",
  "Assessing technical debt",
  "Checking test coverage requirements",
  "Reviewing API contracts",
  "Checking database schema compatibility",
  "Reviewing security requirements",
  "Assessing performance requirements",
];

export const PAUSE_RESUME_FAKE_SCRIPT = JSON.stringify({
  manager: NARRATIONS.flatMap((narration, i) => turn(i + 1, NARRATIONS.length, narration)),
  worker: [],
  evaluator: [],
});
