/**
 * Global setup for the full-scenario E2E.
 * Creates the isolated temp dirs, writes settings.yaml, and initialises one git
 * repo per describe block. Server start/stop is managed by the webServer option
 * in playwright.config.full-scenario.ts.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { FULL_SCENARIO_SEED } from "./full-scenario-seed";

function initRepo(repoDir: string): void {
  fs.mkdirSync(repoDir, { recursive: true });
  const git = (args: string[]) => execFileSync("git", args, { cwd: repoDir, stdio: "pipe" });
  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);
  fs.writeFileSync(path.join(repoDir, "README.md"), "# myrepo\n", "utf8");
  git(["add", "."]);
  git(["commit", "-m", "initial commit"]);
}

export default async function globalSetup(): Promise<void> {
  if (fs.existsSync(FULL_SCENARIO_SEED.base)) {
    fs.rmSync(FULL_SCENARIO_SEED.base, { recursive: true, force: true });
  }
  fs.mkdirSync(FULL_SCENARIO_SEED.configDir, { recursive: true });
  fs.mkdirSync(FULL_SCENARIO_SEED.workspaceDir, { recursive: true });

  const settingsYaml = `${[
    `workspace_root: "${FULL_SCENARIO_SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(path.join(FULL_SCENARIO_SEED.configDir, "settings.yaml"), settingsYaml, "utf8");

  for (const repoDir of Object.values(FULL_SCENARIO_SEED.repoDirs)) {
    initRepo(repoDir);
  }
  console.log("[full-scenario globalSetup] temp dirs ready:", FULL_SCENARIO_SEED.base);
}
