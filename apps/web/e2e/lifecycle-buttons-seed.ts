/**
 * Seed constants specific to the lifecycle-buttons scenario.
 *
 * - Scenario A: Manager effort persistence (set effort on epic creation → reload → verify)
 * - Scenario B: Epic close (close a planned epic → status becomes closed)
 *
 * No run is needed. The fake script can be empty.
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-lifecycle");

export const LIFECYCLE_SEED = {
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
export const LIFECYCLE_FAKE_SCRIPT = JSON.stringify({
  manager: [{ type: "text", text: "noop" }],
  worker: [],
  evaluator: [],
});
