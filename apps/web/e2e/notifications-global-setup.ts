/**
 * Global setup for notifications E2E scenario.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { NOTIF_SEED } from "./notifications-seed";

export default async function globalSetup(): Promise<void> {
  if (fs.existsSync(NOTIF_SEED.base)) {
    fs.rmSync(NOTIF_SEED.base, { recursive: true, force: true });
  }

  fs.mkdirSync(NOTIF_SEED.configDir, { recursive: true });
  fs.mkdirSync(NOTIF_SEED.workspaceDir, { recursive: true });
  fs.mkdirSync(NOTIF_SEED.repoDir, { recursive: true });

  const settingsYaml = `${[
    `workspace_root: "${NOTIF_SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(path.join(NOTIF_SEED.configDir, "settings.yaml"), settingsYaml, "utf8");

  const git = (args: string[]) =>
    execFileSync("git", args, { cwd: NOTIF_SEED.repoDir, stdio: "pipe" });
  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);
  const readmePath = path.join(NOTIF_SEED.repoDir, "README.md");
  fs.writeFileSync(readmePath, "# myrepo\n", "utf8");
  git(["add", "."]);
  git(["commit", "-m", "initial commit"]);

  console.log("[notifications globalSetup] temp dirs ready:", NOTIF_SEED.base);
}
