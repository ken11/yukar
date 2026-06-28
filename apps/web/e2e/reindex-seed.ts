/**
 * Seed constants for the reindex scenario.
 *
 * Opens the Repos page with a repo already registered to the project,
 * clicks the reindex button, and verifies that the index status badge transitions
 * to a terminal state. No run is needed; the fake script can be empty.
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-reindex");

export const REINDEX_SEED = {
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
 * Fake LLM script — minimal configuration since no run is needed.
 */
export const REINDEX_FAKE_SCRIPT = JSON.stringify({
  manager: [{ type: "text", text: "noop" }],
  worker: [],
  evaluator: [],
});
