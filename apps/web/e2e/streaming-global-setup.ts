/**
 * Global setup for MessageTurn streaming E2E scenario.
 *
 * Prepares a temp dir dedicated to the streaming scenario and initializes a git repo.
 * Server start/stop is managed by playwright.config.streaming.ts via webServer.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { STREAMING_SEED } from "./streaming-seed";

export default async function globalSetup(): Promise<void> {
  // Clean up if the temp dir already exists
  if (fs.existsSync(STREAMING_SEED.base)) {
    fs.rmSync(STREAMING_SEED.base, { recursive: true, force: true });
  }

  fs.mkdirSync(STREAMING_SEED.configDir, { recursive: true });
  fs.mkdirSync(STREAMING_SEED.workspaceDir, { recursive: true });
  fs.mkdirSync(STREAMING_SEED.repoDir, { recursive: true });

  // Write settings.yaml (same content as the config evaluation write, overwriting)
  const settingsYaml = `${[
    `workspace_root: "${STREAMING_SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(path.join(STREAMING_SEED.configDir, "settings.yaml"), settingsYaml, "utf8");

  // Initialize git repo
  const git = (args: string[]) =>
    execFileSync("git", args, { cwd: STREAMING_SEED.repoDir, stdio: "pipe" });
  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);
  const readmePath = path.join(STREAMING_SEED.repoDir, "README.md");
  fs.writeFileSync(readmePath, "# myrepo\n", "utf8");
  git(["add", "."]);
  git(["commit", "-m", "initial commit"]);

  console.log("[streaming globalSetup] temp dirs ready:", STREAMING_SEED.base);
}
