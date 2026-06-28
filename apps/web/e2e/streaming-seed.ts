/**
 * Seed constants and fake script dedicated to the MessageTurn streaming scenario.
 *
 * The manager's first turn is a MessageTurn (mixed text + tool_use),
 * and we verify in a real browser that both a text block and a tool-call row
 * are rendered inside a single assistant bubble.
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-streaming");

export const STREAMING_SEED = {
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
 * Fake LLM script for MessageTurn streaming scenario.
 *
 * The Manager's first turn is a MessageTurn:
 *   blocks = [
 *     { type: "text",     text: "まず計画を整理します" },
 *     { type: "tool_use", tool_name: "task_update", tool_input: {...} },
 *   ]
 * → Grouped into 1 bubble at the same msg_index, with text and ToolCallRow coexisting.
 *
 * Afterwards, dispatch → complete_epic advances the run to completed.
 * Worker / Evaluator follow the FAKE_SCRIPT in seed.ts.
 */
export const STREAMING_FAKE_SCRIPT = JSON.stringify({
  manager: [
    // Core verification: MessageTurn (text + tool_use coexisting in 1 bubble)
    {
      type: "message",
      blocks: [
        { type: "text", text: "まず計画を整理します" },
        {
          type: "tool_use",
          tool_name: "task_update",
          tool_input: {
            task_id: "T1",
            title: "Write hello.py",
            status: "todo",
            repo: "myrepo",
            contract: "Create hello.py that prints hello. Verify: file exists and prints 'hello'.",
          },
        },
      ],
    },
    {
      type: "tool_use",
      tool_name: "dispatch",
      tool_input: { items: [{ task_id: "T1", repo: "myrepo" }] },
    },
    {
      type: "tool_use",
      tool_name: "complete_epic",
      tool_input: {},
    },
    { type: "text", text: "Epic complete." },
  ],
  worker: [
    {
      type: "tool_use",
      tool_name: "fs_write",
      tool_input: { path: "hello.py", content: "def greet():\n    print('hello')\n" },
    },
    {
      type: "tool_use",
      tool_name: "repo_grep",
      tool_input: { pattern: "hello" },
    },
    {
      type: "tool_use",
      tool_name: "fs_write",
      tool_input: { path: "util.py", content: "VALUE = 42\n" },
    },
    {
      type: "text",
      text: "Implemented hello.py (greet) and util.py; confirmed 'hello' is present via repo_grep.",
    },
  ],
  evaluator: [
    {
      type: "tool_use",
      tool_name: "read_diff",
      tool_input: {},
    },
    {
      type: "tool_use",
      tool_name: "submit_verdict",
      tool_input: { accepted: true, feedback: "" },
    },
    { type: "text", text: "Accepted." },
  ],
});
