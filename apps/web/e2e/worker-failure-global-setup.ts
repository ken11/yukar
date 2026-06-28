/**
 * Global setup for Worker failure E2E scenario.
 *
 * Prepares a temp dir dedicated to the Worker failure scenario and initializes a git repo.
 * Server start/stop is managed by playwright.config.worker-failure.ts via webServer.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { WORKER_FAILURE_SEED } from "./worker-failure-seed";

export default async function globalSetup(): Promise<void> {
  // Clean up if the temp dir already exists
  if (fs.existsSync(WORKER_FAILURE_SEED.base)) {
    fs.rmSync(WORKER_FAILURE_SEED.base, { recursive: true, force: true });
  }

  fs.mkdirSync(WORKER_FAILURE_SEED.configDir, { recursive: true });
  fs.mkdirSync(WORKER_FAILURE_SEED.workspaceDir, { recursive: true });
  fs.mkdirSync(WORKER_FAILURE_SEED.repoDir, { recursive: true });

  // Write settings.yaml (same content as the config evaluation write, overwriting)
  const settingsYaml = `${[
    `workspace_root: "${WORKER_FAILURE_SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(path.join(WORKER_FAILURE_SEED.configDir, "settings.yaml"), settingsYaml, "utf8");

  // Initialize git repo
  const git = (args: string[]) =>
    execFileSync("git", args, { cwd: WORKER_FAILURE_SEED.repoDir, stdio: "pipe" });
  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);
  const readmePath = path.join(WORKER_FAILURE_SEED.repoDir, "README.md");
  fs.writeFileSync(readmePath, "# myrepo\n", "utf8");
  git(["add", "."]);
  git(["commit", "-m", "initial commit"]);

  console.log("[worker-failure globalSetup] temp dirs ready:", WORKER_FAILURE_SEED.base);
}
