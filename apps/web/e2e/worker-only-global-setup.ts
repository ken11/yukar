/**
 * Global setup for the worker-only dispatch E2E scenario (P6).
 *
 * Prepares a temp dir dedicated to the worker-only scenario and initialises
 * the git repo.  Server start/stop is managed by the webServer setting in
 * playwright.config.worker-only.ts.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { WORKER_ONLY_SEED } from "./worker-only-seed";

export default async function globalSetup(): Promise<void> {
  // Clean up if the temp dir already exists
  if (fs.existsSync(WORKER_ONLY_SEED.base)) {
    fs.rmSync(WORKER_ONLY_SEED.base, { recursive: true, force: true });
  }

  fs.mkdirSync(WORKER_ONLY_SEED.configDir, { recursive: true });
  fs.mkdirSync(WORKER_ONLY_SEED.workspaceDir, { recursive: true });
  fs.mkdirSync(WORKER_ONLY_SEED.repoDir, { recursive: true });

  // Write settings.yaml
  const settingsYaml = `${[
    `workspace_root: "${WORKER_ONLY_SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(path.join(WORKER_ONLY_SEED.configDir, "settings.yaml"), settingsYaml, "utf8");

  // Initialise git repo
  const git = (args: string[]) =>
    execFileSync("git", args, { cwd: WORKER_ONLY_SEED.repoDir, stdio: "pipe" });
  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);
  const readmePath = path.join(WORKER_ONLY_SEED.repoDir, "README.md");
  fs.writeFileSync(readmePath, "# myrepo\n", "utf8");
  git(["add", "."]);
  git(["commit", "-m", "initial commit"]);

  console.log("[worker-only globalSetup] temp dirs ready:", WORKER_ONLY_SEED.base);
}
