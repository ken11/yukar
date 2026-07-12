/**
 * Seed constants and fake script dedicated to the pause/resume/stop scenario.
 *
 * YUKAR_FAKE_SLEEP=6.0 inserts a 6.0s delay between each chunk.
 * Because 6.0s > the supervisor's 5s cancel wait, stopping from the
 * running state causes the task to receive CancelledError and
 * state.status to stay "waiting" (the user-stop path).
 *
 * Turn-end semantics note: EVERY ended turn parks the run in "waiting"
 * (a text/end_turn response yields to the user).  A long "running" window
 * therefore has to come from WITHIN a single manager turn: consecutive
 * tool_use turns make Strands recurse inside the same orchestrator turn, so
 * the run stays "running" until the final text turn would end it.
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

/**
 * Fake LLM script for pause/resume/stop scenario.
 *
 * Turn layout (used together with YUKAR_FAKE_SLEEP=6.0):
 *   - 15 consecutive task_update tool_use turns: each tool result makes
 *     Strands recurse within the SAME orchestrator turn, so the run keeps
 *     "running" (no end_turn, no park) while the tests pause / resume / stop.
 *   - dispatch is never called, so no worker/evaluator ever starts.
 *   - The trailing text turn is never reached in practice (test 6 stops the
 *     run first); it exists only to terminate the script cleanly if it is.
 *
 * Why FAKE_SLEEP=6.0 matters:
 *   supervisor.stop() waits 5s then cancels the asyncio.Task.
 *   If the manager is mid asyncio.sleep(6.0), it receives CancelledError and
 *   the orchestrator leaves state.status = "waiting" (the user-stop path).
 */
const STEPS = [
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
  manager: [
    ...STEPS.map((step, i) => ({
      type: "tool_use",
      tool_name: "task_update",
      tool_input: {
        task_id: "T1",
        title: "Implement feature A",
        status: "todo",
        repo: "myrepo",
        contract: `Implement feature A. Verify: tests pass. (${step} — step ${i + 1} of ${STEPS.length})`,
      },
    })),
    { type: "text", text: "Planning pass finished." },
  ],
  worker: [],
  evaluator: [],
});
