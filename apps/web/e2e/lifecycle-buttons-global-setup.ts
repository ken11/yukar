/**
 * Global setup for lifecycle-buttons E2E scenario.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { LIFECYCLE_SEED } from "./lifecycle-buttons-seed";

export default async function globalSetup(): Promise<void> {
  if (fs.existsSync(LIFECYCLE_SEED.base)) {
    fs.rmSync(LIFECYCLE_SEED.base, { recursive: true, force: true });
  }

  fs.mkdirSync(LIFECYCLE_SEED.configDir, { recursive: true });
  fs.mkdirSync(LIFECYCLE_SEED.workspaceDir, { recursive: true });
  fs.mkdirSync(LIFECYCLE_SEED.repoDir, { recursive: true });

  const settingsYaml = `${[
    `workspace_root: "${LIFECYCLE_SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(path.join(LIFECYCLE_SEED.configDir, "settings.yaml"), settingsYaml, "utf8");

  const git = (args: string[]) =>
    execFileSync("git", args, { cwd: LIFECYCLE_SEED.repoDir, stdio: "pipe" });
  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);
  const readmePath = path.join(LIFECYCLE_SEED.repoDir, "README.md");
  fs.writeFileSync(readmePath, "# myrepo\n", "utf8");
  git(["add", "."]);
  git(["commit", "-m", "initial commit"]);

  console.log("[lifecycle globalSetup] temp dirs ready:", LIFECYCLE_SEED.base);
}
