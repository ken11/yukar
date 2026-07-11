/**
 * Seed constants and fake script dedicated to the budget-exceeded scenario.
 *
 * Injects a large usage into the Manager's first turn to record token consumption.
 * The fake provider identifies itself as the default model_id from settings (sonnet-4-6),
 * so the price table is applied and the actual cost_usd is calculated from the injected tokens
 * (approximately $42).
 * After the run completes, a low positive limit (limit_usd=1) is set so that the actual cost
 * naturally exceeds the limit and over_budget=true is verified (not a limit=0 tautology).
 * Also verifies that the Usage page shows the over-budget indicator and that a subsequent
 * POST /run returns 409.
 *
 * Usage injection is done via the "usage" key of FakeModel's ToolUseTurn.
 * Bedrock camelCase format: inputTokens / outputTokens / totalTokens
 *
 * Cost is calculated using the default model_id from settings (sonnet-4-6: input $3.0/1M, output $15.0/1M):
 * 9*3.0 + 1*15.0 = $42 USD (approx. ¥6,510 at a rate of 155).
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-budget");

export const BUDGET_SEED = {
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
 * Fake LLM script for budget exceeded scenario.
 *
 * Injects a large usage into the Manager's first turn to record token consumption.
 * The turn is a single task_update tool call (usage rides on the ToolUseTurn) followed
 * by a report text — the text ends the turn and the run parks in "waiting" (P3).
 * After the run parks, sets a low positive limit (limit_usd=1) so the actual cost ($42) naturally exceeds it.
 */
export const BUDGET_FAKE_SCRIPT = JSON.stringify({
  manager: [
    {
      // Token consumption is recorded via usage injection (cost is calculated
      // automatically from the model_id in settings).
      type: "tool_use",
      tool_name: "task_update",
      tool_input: {
        task_id: "T1",
        title: "Budget probe task",
        status: "todo",
        repo: "myrepo",
        contract: "No-op planning task used to carry the injected usage.",
      },
      usage: {
        inputTokens: 9000000,
        outputTokens: 1000000,
        totalTokens: 10000000,
      },
    },
    { type: "text", text: "Budget scenario complete." },
  ],
  worker: [],
  evaluator: [],
});
