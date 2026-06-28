/**
 * Playwright globalSetup.
 * Runs once before all tests in a single Node.js process.
 * Creates deterministic temp directories, writes settings.yaml,
 * and initialises the managed git repo (myrepo).
 */

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { SEED } from "./seed";

/**
 * Initialise a managed git repo (myrepo) with one commit so create-project git
 * validation passes and HEAD exists (required for worktree creation).
 */
function initRepo(repoDir: string): void {
  fs.mkdirSync(repoDir, { recursive: true });
  const git = (args: string[]) => execFileSync("git", args, { cwd: repoDir, stdio: "pipe" });

  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);

  // Initial commit so HEAD exists (required for worktree creation)
  fs.writeFileSync(path.join(repoDir, "README.md"), "# myrepo\n", "utf8");
  git(["add", "."]);
  git(["commit", "-m", "initial commit"]);
}

export default async function globalSetup(): Promise<void> {
  // Clean up any leftover state from a previous run
  if (fs.existsSync(SEED.base)) {
    fs.rmSync(SEED.base, { recursive: true, force: true });
  }

  // Create directory tree
  fs.mkdirSync(SEED.configDir, { recursive: true });
  fs.mkdirSync(SEED.workspaceDir, { recursive: true });

  // Write settings.yaml — use fake for both LLM and embedding to avoid AWS credential errors
  const settingsYaml = `${[
    `workspace_root: "${SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(path.join(SEED.configDir, "settings.yaml"), settingsYaml, "utf8");

  // One isolated repo per main-config spec (scenario merges to default and must
  // not contaminate smoke's epic⇔default diff — see seed.ts repoDirs).
  for (const repoDir of Object.values(SEED.repoDirs)) {
    initRepo(repoDir);
  }

  console.log("[e2e globalSetup] temp dirs ready:", SEED.base);
}
