/**
 * Global setup for Evaluator reject → retry → accept E2E scenario.
 *
 * Prepares a temp dir dedicated to the evaluator-reject scenario and initialises the git repo.
 * Server start/stop is managed by the webServer setting in playwright.config.evaluator-reject.ts.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { EVALUATOR_REJECT_SEED } from "./evaluator-reject-seed";

export default async function globalSetup(): Promise<void> {
  // Clean up if the temp dir already exists
  if (fs.existsSync(EVALUATOR_REJECT_SEED.base)) {
    fs.rmSync(EVALUATOR_REJECT_SEED.base, { recursive: true, force: true });
  }

  fs.mkdirSync(EVALUATOR_REJECT_SEED.configDir, { recursive: true });
  fs.mkdirSync(EVALUATOR_REJECT_SEED.workspaceDir, { recursive: true });
  fs.mkdirSync(EVALUATOR_REJECT_SEED.repoDir, { recursive: true });

  // Write settings.yaml
  const settingsYaml = `${[
    `workspace_root: "${EVALUATOR_REJECT_SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(
    path.join(EVALUATOR_REJECT_SEED.configDir, "settings.yaml"),
    settingsYaml,
    "utf8",
  );

  // Initialise git repo
  const git = (args: string[]) =>
    execFileSync("git", args, { cwd: EVALUATOR_REJECT_SEED.repoDir, stdio: "pipe" });
  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);
  const readmePath = path.join(EVALUATOR_REJECT_SEED.repoDir, "README.md");
  fs.writeFileSync(readmePath, "# myrepo\n", "utf8");
  git(["add", "."]);
  git(["commit", "-m", "initial commit"]);

  console.log("[evaluator-reject globalSetup] temp dirs ready:", EVALUATOR_REJECT_SEED.base);
}
