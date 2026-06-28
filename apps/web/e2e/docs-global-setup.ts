/**
 * Global setup for docs edit/save/reload persistence E2E scenario.
 *
 * Prepares a temp dir and initialises a git repo.
 * Server start/stop is managed by webServer in playwright.config.docs.ts.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { DOCS_SEED } from "./docs-seed";

export default async function globalSetup(): Promise<void> {
  // Clean up if temp dir already exists
  if (fs.existsSync(DOCS_SEED.base)) {
    fs.rmSync(DOCS_SEED.base, { recursive: true, force: true });
  }

  fs.mkdirSync(DOCS_SEED.configDir, { recursive: true });
  fs.mkdirSync(DOCS_SEED.workspaceDir, { recursive: true });
  fs.mkdirSync(DOCS_SEED.repoDir, { recursive: true });

  // Write settings.yaml
  const settingsYaml = `${[
    `workspace_root: "${DOCS_SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(path.join(DOCS_SEED.configDir, "settings.yaml"), settingsYaml, "utf8");

  // Initialise git repo
  const git = (args: string[]) =>
    execFileSync("git", args, { cwd: DOCS_SEED.repoDir, stdio: "pipe" });
  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);
  const readmePath = path.join(DOCS_SEED.repoDir, "README.md");
  fs.writeFileSync(readmePath, "# myrepo\n", "utf8");
  git(["add", "."]);
  git(["commit", "-m", "initial commit"]);

  console.log("[docs globalSetup] temp dirs ready:", DOCS_SEED.base);
}
