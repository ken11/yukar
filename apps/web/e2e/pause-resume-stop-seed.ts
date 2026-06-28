/**
 * Seed constants and fake script dedicated to the pause/resume/stop scenario.
 *
 * YUKAR_FAKE_SLEEP=6.0 inserts a 6.0s delay between each chunk.
 * Because 6.0s > the supervisor's 5s cancel wait, stopping from the
 * running state causes the task to receive CancelledError and
 * state.status to become "idle" (the canonical idle path).
 *
 * 29 text turns are queued for the manager (dispatch/complete_epic are not called).
 * T1 remains "todo", bypassing the deadlock guard so that after the script is
 * exhausted "Script exhausted." turns continue until the turn limit.
 * The tests execute pause / resume / stop before that point.
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
 *   - Turn 0: task_update sets T1 to "todo".
 *     runnable_exists becomes True, bypassing the deadlock guard
 *     (which would break immediately when turn>0 and runnable/in_flight are both zero).
 *     dispatch/complete_epic are not called, so the run keeps consuming manager turns.
 *   - Turns 1–29: 29 text turns (6.0s sleep × ~3 chunks each = ~18s/turn) → ~540s running window.
 *
 * Why FAKE_SLEEP=6.0 matters:
 *   supervisor.stop() waits 5s then cancels the asyncio.Task.
 *   If the manager is mid asyncio.sleep(6.0), it receives CancelledError and
 *   the orchestrator sets state.status = "idle" (the canonical idle path).
 *
 * worker / evaluator are empty (they are never called because dispatch is not invoked).
 */
export const PAUSE_RESUME_FAKE_SCRIPT = JSON.stringify({
  manager: [
    // Turn 0: task_update registers T1 as "todo".
    // runnable_exists becomes True and the deadlock guard is bypassed.
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
    // Turns 1–29: text turns (6.0s × ~3 sleeps/turn = ~18s/turn, total ~540s running window).
    // Turns are not consumed while paused; tests 3-5 complete while paused.
    // Test 6 stops from the running state → CancelledError → "idle".
    { type: "text", text: "Analyzing requirements (step 1 of 29)." },
    { type: "text", text: "Reviewing codebase structure (step 2 of 29)." },
    { type: "text", text: "Identifying dependencies (step 3 of 29)." },
    { type: "text", text: "Planning implementation approach (step 4 of 29)." },
    { type: "text", text: "Estimating effort for each task (step 5 of 29)." },
    { type: "text", text: "Checking for potential conflicts (step 6 of 29)." },
    { type: "text", text: "Validating acceptance criteria (step 7 of 29)." },
    { type: "text", text: "Preparing task breakdown (step 8 of 29)." },
    { type: "text", text: "Reviewing risk factors (step 9 of 29)." },
    { type: "text", text: "Assessing technical debt (step 10 of 29)." },
    { type: "text", text: "Checking test coverage requirements (step 11 of 29)." },
    { type: "text", text: "Reviewing API contracts (step 12 of 29)." },
    { type: "text", text: "Checking database schema compatibility (step 13 of 29)." },
    { type: "text", text: "Reviewing security requirements (step 14 of 29)." },
    { type: "text", text: "Assessing performance requirements (step 15 of 29)." },
    { type: "text", text: "Reviewing documentation needs (step 16 of 29)." },
    { type: "text", text: "Checking integration points (step 17 of 29)." },
    { type: "text", text: "Reviewing error handling strategy (step 18 of 29)." },
    { type: "text", text: "Assessing monitoring requirements (step 19 of 29)." },
    { type: "text", text: "Checking deployment requirements (step 20 of 29)." },
    { type: "text", text: "Reviewing rollback strategy (step 21 of 29)." },
    { type: "text", text: "Assessing data migration needs (step 22 of 29)." },
    { type: "text", text: "Checking backwards compatibility (step 23 of 29)." },
    { type: "text", text: "Reviewing feature flags (step 24 of 29)." },
    { type: "text", text: "Assessing A/B testing requirements (step 25 of 29)." },
    { type: "text", text: "Checking localization requirements (step 26 of 29)." },
    { type: "text", text: "Reviewing accessibility requirements (step 27 of 29)." },
    { type: "text", text: "Assessing caching strategy (step 28 of 29)." },
    { type: "text", text: "Plan complete. Awaiting dispatch decision. (step 29 of 29)." },
  ],
  worker: [],
  evaluator: [],
});
