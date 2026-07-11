/**
 * Shared seed paths for e2e tests.
 * All paths are deterministic fixed paths under os.tmpdir().
 * Used by globalSetup, globalTeardown, playwright.config.ts, and spec files.
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e");

export const SEED = {
  /** Temporary root */
  base,
  /** YUKAR_CONFIG_DIR — contains settings.yaml */
  configDir: path.join(base, "config"),
  /** workspace_root in settings.yaml */
  workspaceDir: path.join(base, "workspace"),
  /**
   * Per-spec managed git repos.
   *
   * The three specs in the main config (scenario / smoke / multi-trial) share
   * one API+web server, workspace, and config dir, but each drives its OWN
   * project against its OWN git repo. This keeps their git state isolated:
   * `scenario` merges its epic into default(main), and a shared repo would let
   * that merge contaminate `smoke`'s epic⇔default diff (hello.py would already
   * be on default, so it would not appear as a change). Project layout is keyed
   * by project_id (see config/paths.py), so every repo basename can stay
   * "myrepo" — required for the fake script's `repo: "myrepo"` to resolve.
   */
  repoDirs: {
    scenario: path.join(base, "repo-scenario", "myrepo"),
    smoke: path.join(base, "repo-smoke", "myrepo"),
    multiTrial: path.join(base, "repo-multi-trial", "myrepo"),
    continueBranch: path.join(base, "repo-continue-branch", "myrepo"),
    reviewer: path.join(base, "repo-reviewer", "myrepo"),
  },
} as const;

/**
 * Fake LLM script (role-keyed JSON).
 * Each agent replays its list from the top on every new agent instantiation.
 *
 * Reflects the commit-after-eval lifecycle: the Worker does NOT commit — it only
 * writes files (and uses repo_grep to verify them in the live worktree); the host
 * commits on the Evaluator's accept.  Each scripted tool_use / text turn becomes a
 * separate assistant message, so the conversation renders one bubble per utterance.
 *
 * Manager:   task_update(T1) → dispatch(T1) → text report  (the text turn ends
 *            the turn, so the run parks in "waiting" — a conversation never
 *            "completes"; work-done is detected as waiting + all tasks done)
 * Worker:    fs_write(hello.py) → repo_grep("hello") → fs_write(util.py) → text  (no commit)
 * Evaluator: read_diff (host-staged) → submit_verdict(accepted) → text
 */
export const FAKE_SCRIPT = JSON.stringify({
  manager: [
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
    {
      type: "tool_use",
      tool_name: "dispatch",
      tool_input: { items: [{ task_id: "T1", repo: "myrepo" }] },
    },
    {
      type: "text",
      text: "All tasks are done: hello.py and util.py were implemented and accepted. Please review the branch diff.",
    },
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
      text: "Implemented hello.py (greet) and util.py; confirmed 'hello' is present via repo_grep. Leaving the changes uncommitted for the host to commit on accept.",
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
  // Reviewer (read-only): inspect the branch diff (git-based) AND read a file
  // from the active manager trial's worktree via fs_read (proves the worktree-
  // backed read-only tools are wired), then report in plain body text.  The
  // text turn ends the turn, so the run parks in "waiting" (your turn) —
  // reporting is conversation, not a state-machine event. Only used by
  // reviewer.spec.
  reviewer: [
    {
      type: "tool_use",
      tool_name: "read_branch_diff",
      tool_input: {},
    },
    {
      type: "tool_use",
      tool_name: "fs_read",
      tool_input: { path: "hello.py", repo: "myrepo" },
    },
    {
      type: "text",
      text: "Reviewed the branch: hello.py and util.py are present and match the epic's intent. Approve as-is?",
    },
  ],
});
