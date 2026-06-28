/**
 * Seed constants and fake script dedicated to the ask_user scenario.
 *
 * The manager's first turn calls ask_user, which puts the run into awaiting_input.
 * The E2E test verifies that the question bubble is restored after a page reload
 * (restored from RunState.pending_question via GET /run/state — no SSE replay dependency).
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-ask-user");

export const ASK_USER_SEED = {
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
 * Fake LLM script for ask_user scenario.
 *
 * The manager calls ask_user on its first turn.
 * This puts the run into awaiting_input and displays the question bubble.
 *
 * Worker / Evaluator are empty (the run halts in awaiting_input, so they are not needed).
 */
export const ASK_USER_FAKE_SCRIPT = JSON.stringify({
  manager: [
    {
      type: "tool_use",
      tool_name: "ask_user",
      tool_input: {
        question: "この計画で進めてよいですか？",
      },
    },
  ],
  worker: [],
  evaluator: [],
});
