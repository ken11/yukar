/**
 * Global setup for pause/resume/stop E2E scenario.
 *
 * Prepares a temp dir dedicated to the pause/resume/stop scenario and initialises the git repo.
 * Server start/stop is managed by the webServer entry in playwright.config.pause-resume-stop.ts.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { PAUSE_RESUME_SEED } from "./pause-resume-stop-seed";

export default async function globalSetup(): Promise<void> {
  // Clean up if the temp dir already exists
  if (fs.existsSync(PAUSE_RESUME_SEED.base)) {
    fs.rmSync(PAUSE_RESUME_SEED.base, { recursive: true, force: true });
  }

  fs.mkdirSync(PAUSE_RESUME_SEED.configDir, { recursive: true });
  fs.mkdirSync(PAUSE_RESUME_SEED.workspaceDir, { recursive: true });
  fs.mkdirSync(PAUSE_RESUME_SEED.repoDir, { recursive: true });

  // Write settings.yaml (same content that config evaluation would write; overwrite is fine)
  const settingsYaml = `${[
    `workspace_root: "${PAUSE_RESUME_SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(path.join(PAUSE_RESUME_SEED.configDir, "settings.yaml"), settingsYaml, "utf8");

  // Initialise git repo
  const git = (args: string[]) =>
    execFileSync("git", args, { cwd: PAUSE_RESUME_SEED.repoDir, stdio: "pipe" });
  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);
  const readmePath = path.join(PAUSE_RESUME_SEED.repoDir, "README.md");
  fs.writeFileSync(readmePath, "# myrepo\n", "utf8");
  git(["add", "."]);
  git(["commit", "-m", "initial commit"]);

  console.log("[pause-resume globalSetup] temp dirs ready:", PAUSE_RESUME_SEED.base);
}
