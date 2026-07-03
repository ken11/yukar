/**
 * Seed constants for the repos add/delete (CRUD) scenario.
 *
 * Registers a project with one repo (alpha), then exercises the Repos page
 * "Add repository" inline form and per-row delete button against a second
 * local git repo (beta). No run is needed; the fake script can be empty.
 */
import os from "node:os";
import path from "node:path";

const base = path.join(os.tmpdir(), "yukar-e2e-repos-crud");

export const REPOS_CRUD_SEED = {
  /** Temporary root */
  base,
  /** YUKAR_CONFIG_DIR — contains settings.yaml */
  configDir: path.join(base, "config"),
  /** workspace_root in settings.yaml */
  workspaceDir: path.join(base, "workspace"),
  /** First local git repo, registered at project-creation time */
  repoDirA: path.join(base, "repo", "alpha"),
  /** Second local git repo, added later via the Repos page form */
  repoDirB: path.join(base, "repo", "beta"),
} as const;

/** Fake LLM script — minimal configuration since no run is needed. */
export const REPOS_CRUD_FAKE_SCRIPT = JSON.stringify({
  manager: [{ type: "text", text: "noop" }],
  worker: [],
  evaluator: [],
});
