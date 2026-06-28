/**
 * Global setup for budget exceeded E2E scenario.
 *
 * Prepares a temp dir dedicated to the budget-exceeded scenario and initialises the git repo.
 * Server start/stop is managed by the webServer option in playwright.config.budget.ts.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { BUDGET_SEED } from "./budget-seed";

export default async function globalSetup(): Promise<void> {
  // Clean up if the temp dir already exists
  if (fs.existsSync(BUDGET_SEED.base)) {
    fs.rmSync(BUDGET_SEED.base, { recursive: true, force: true });
  }

  fs.mkdirSync(BUDGET_SEED.configDir, { recursive: true });
  fs.mkdirSync(BUDGET_SEED.workspaceDir, { recursive: true });
  fs.mkdirSync(BUDGET_SEED.repoDir, { recursive: true });

  // Write settings.yaml
  const settingsYaml = `${[
    `workspace_root: "${BUDGET_SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(path.join(BUDGET_SEED.configDir, "settings.yaml"), settingsYaml, "utf8");

  // Initialise the git repo
  const git = (args: string[]) =>
    execFileSync("git", args, { cwd: BUDGET_SEED.repoDir, stdio: "pipe" });
  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);
  const readmePath = path.join(BUDGET_SEED.repoDir, "README.md");
  fs.writeFileSync(readmePath, "# myrepo\n", "utf8");
  git(["add", "."]);
  git(["commit", "-m", "initial commit"]);

  console.log("[budget globalSetup] temp dirs ready:", BUDGET_SEED.base);
}
