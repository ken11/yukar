/**
 * Global setup for ask_user E2E scenario.
 *
 * Prepares a temp dir dedicated to the ask_user scenario and initialises a git repo.
 * Server start/stop is managed by the webServer option in playwright.config.ask-user.ts
 * and the module-level killUvicorn/killNextJs helpers.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { ASK_USER_SEED } from "./ask-user-seed";

export default async function globalSetup(): Promise<void> {
  // Clean up if temp dir already exists
  if (fs.existsSync(ASK_USER_SEED.base)) {
    fs.rmSync(ASK_USER_SEED.base, { recursive: true, force: true });
  }

  fs.mkdirSync(ASK_USER_SEED.configDir, { recursive: true });
  fs.mkdirSync(ASK_USER_SEED.workspaceDir, { recursive: true });
  fs.mkdirSync(ASK_USER_SEED.repoDir, { recursive: true });

  // Write settings.yaml (overwrite with the same content produced during config evaluation)
  const settingsYaml = `${[
    `workspace_root: "${ASK_USER_SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(path.join(ASK_USER_SEED.configDir, "settings.yaml"), settingsYaml, "utf8");

  // Initialise git repo
  const git = (args: string[]) =>
    execFileSync("git", args, { cwd: ASK_USER_SEED.repoDir, stdio: "pipe" });
  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);
  const readmePath = path.join(ASK_USER_SEED.repoDir, "README.md");
  fs.writeFileSync(readmePath, "# myrepo\n", "utf8");
  git(["add", "."]);
  git(["commit", "-m", "initial commit"]);

  console.log("[ask-user globalSetup] temp dirs ready:", ASK_USER_SEED.base);
}
