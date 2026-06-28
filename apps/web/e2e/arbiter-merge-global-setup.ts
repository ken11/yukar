/**
 * Global setup for the Arbiter merge (bulk merge of multiple Epics) E2E scenario.
 *
 * Prepares the temp dir dedicated to the arbiter-merge scenario and initialises the git repo.
 * Server start/stop is managed by the webServer in playwright.config.arbiter-merge.ts.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { ARBITER_MERGE_SEED } from "./arbiter-merge-seed";

export default async function globalSetup(): Promise<void> {
  // Clean up if temp dir already exists
  if (fs.existsSync(ARBITER_MERGE_SEED.base)) {
    fs.rmSync(ARBITER_MERGE_SEED.base, { recursive: true, force: true });
  }

  fs.mkdirSync(ARBITER_MERGE_SEED.configDir, { recursive: true });
  fs.mkdirSync(ARBITER_MERGE_SEED.workspaceDir, { recursive: true });
  fs.mkdirSync(ARBITER_MERGE_SEED.repoDir, { recursive: true });

  // Write settings.yaml
  const settingsYaml = `${[
    `workspace_root: "${ARBITER_MERGE_SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(path.join(ARBITER_MERGE_SEED.configDir, "settings.yaml"), settingsYaml, "utf8");

  // Initialise git repo
  const git = (args: string[]) =>
    execFileSync("git", args, { cwd: ARBITER_MERGE_SEED.repoDir, stdio: "pipe" });
  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);

  // Initial commit (worktree creation fails without an existing HEAD)
  const readmePath = path.join(ARBITER_MERGE_SEED.repoDir, "README.md");
  fs.writeFileSync(readmePath, "# myrepo\n", "utf8");
  git(["add", "."]);
  git(["commit", "-m", "initial commit"]);

  console.log("[arbiter-merge globalSetup] temp dirs ready:", ARBITER_MERGE_SEED.base);
}
