/**
 * Seed constants and fake script dedicated to the question/reload scenario
 * (historically the "ask_user" scenario; under P3 the ask_user tool is gone —
 * a question is plain assistant body text and the turn end parks the run).
 *
 * The manager's first turn plans a task and asks a question in BODY TEXT,
 * which ends the turn and parks the run in "waiting" (your turn).  The E2E
 * test verifies that the question bubble survives a page reload: it is an
 * ordinary conversation message restored from the thread history (no
 * pending_question carrier, no SSE replay dependency).
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
 * Fake LLM script for the question/reload scenario.
 *
 * Turn 0: the manager registers T1 then asks the question in body text — the
 * text turn ends the turn, so the run parks in "waiting" and the question
 * renders as a normal assistant bubble.
 *
 * Turn 1 (after the user replies with a QUESTION, not an approval): the
 * manager answers in plain text without calling any tool.  Every ended turn
 * parks the run in "waiting" — the host must NOT inject a dispatch command.
 * ask-user.spec test 5 verifies this.
 *
 * Worker / Evaluator are empty (no dispatch happens in this scenario).
 */
export const ASK_USER_ANSWER_TEXT = "T1 は挨拶ファイルを1つ追加するだけの計画です。";

export const ASK_USER_FAKE_SCRIPT = JSON.stringify({
  manager: [
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
    // Turn 0 ends with the question in body text → park in "waiting".
    { type: "text", text: "この計画で進めてよいですか？" },
    // Turn 1: tool-less conversational answer → the run parks in "waiting" again.
    { type: "text", text: ASK_USER_ANSWER_TEXT },
  ],
  worker: [],
  evaluator: [],
});
