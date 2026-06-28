/**
 * Seed constants for the docs edit/save/persistence scenario.
 *
 * No run needed. Only the docs PUT API is used. The fake script can be empty.
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-docs");

export const DOCS_SEED = {
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
 * Fake LLM script — no run needed, so minimal config (single text item, exits immediately).
 */
export const DOCS_FAKE_SCRIPT = JSON.stringify({
  manager: [{ type: "text", text: "noop" }],
  worker: [],
  evaluator: [],
});
