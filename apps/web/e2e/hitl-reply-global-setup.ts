/**
 * Global setup for hitl-reply E2E scenario.
 *
 * Prepares a temp dir dedicated to the hitl-reply scenario and initialises a git repo.
 * Server start/stop is managed by webServer in playwright.config.hitl-reply.ts.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { HITL_REPLY_SEED } from "./hitl-reply-seed";

export default async function globalSetup(): Promise<void> {
  // Clean up if the temp dir already exists
  if (fs.existsSync(HITL_REPLY_SEED.base)) {
    fs.rmSync(HITL_REPLY_SEED.base, { recursive: true, force: true });
  }

  fs.mkdirSync(HITL_REPLY_SEED.configDir, { recursive: true });
  fs.mkdirSync(HITL_REPLY_SEED.workspaceDir, { recursive: true });
  fs.mkdirSync(HITL_REPLY_SEED.repoDir, { recursive: true });

  // Write settings.yaml (overwrite with the same content written during config evaluation)
  const settingsYaml = `${[
    `workspace_root: "${HITL_REPLY_SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(path.join(HITL_REPLY_SEED.configDir, "settings.yaml"), settingsYaml, "utf8");

  // Initialise git repo
  const git = (args: string[]) =>
    execFileSync("git", args, { cwd: HITL_REPLY_SEED.repoDir, stdio: "pipe" });
  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);
  const readmePath = path.join(HITL_REPLY_SEED.repoDir, "README.md");
  fs.writeFileSync(readmePath, "# myrepo\n", "utf8");
  git(["add", "."]);
  git(["commit", "-m", "initial commit"]);

  console.log("[hitl-reply globalSetup] temp dirs ready:", HITL_REPLY_SEED.base);
}
