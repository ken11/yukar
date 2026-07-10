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
 * Turn 0: the manager calls ask_user — the run enters awaiting_input and the
 * question bubble is shown.
 *
 * Turn 1 (after the user replies with a QUESTION, not an approval): the
 * manager answers in plain text without calling any tool.  Under turn-end
 * semantics this parks the run in question-less awaiting_input — the host
 * must NOT inject a dispatch command.  ask-user.spec test 5 verifies this.
 *
 * Worker / Evaluator are empty (no dispatch happens in this scenario).
 */
export const ASK_USER_ANSWER_TEXT = "T1 は挨拶ファイルを1つ追加するだけの計画です。";

export const ASK_USER_FAKE_SCRIPT = JSON.stringify({
  manager: [
    // Plan first: T1 must EXIST (todo) for the conversational park to be
    // reachable — with zero tasks the deadlock guard ends the run after the
    // tool-less reply instead of parking it.
    {
      type: "tool_use",
      tool_name: "task_update",
      tool_input: {
        task_id: "T1",
        title: "Add greeting file",
        status: "todo",
        repo: "myrepo",
        contract: "Add a greeting file. Verify: file exists.",
      },
    },
    {
      type: "tool_use",
      tool_name: "ask_user",
      tool_input: {
        question: "この計画で進めてよいですか？",
      },
    },
    { type: "text", text: "Awaiting your approval." },
    // Turn 1: tool-less conversational answer → the run parks (awaiting_input
    // with no pending question).
    { type: "text", text: ASK_USER_ANSWER_TEXT },
  ],
  worker: [],
  evaluator: [],
});
