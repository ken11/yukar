/**
 * Global setup for conflict-resolve E2E scenario.
 *
 * Prepares a temp dir dedicated to the conflict-resolve scenario and initialises a git repo.
 * Server start/stop is managed by the webServer entry in playwright.config.conflict-resolve.ts.
 *
 * Initial commit: conflict.txt = "line1\nBASE\nline3\n"
 * This becomes the BASE commit on main.
 * After the Epic run, the spec commits a "MAIN" version to main,
 * while the epic branch has an "EPIC" version, so line2 conflicts on merge.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { CONFLICT_RESOLVE_SEED } from "./conflict-resolve-seed";

export default async function globalSetup(): Promise<void> {
  // Clean up if the temp dir already exists
  if (fs.existsSync(CONFLICT_RESOLVE_SEED.base)) {
    fs.rmSync(CONFLICT_RESOLVE_SEED.base, { recursive: true, force: true });
  }

  fs.mkdirSync(CONFLICT_RESOLVE_SEED.configDir, { recursive: true });
  fs.mkdirSync(CONFLICT_RESOLVE_SEED.workspaceDir, { recursive: true });
  fs.mkdirSync(CONFLICT_RESOLVE_SEED.repoDir, { recursive: true });

  // Write settings.yaml
  const settingsYaml = `${[
    `workspace_root: "${CONFLICT_RESOLVE_SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(
    path.join(CONFLICT_RESOLVE_SEED.configDir, "settings.yaml"),
    settingsYaml,
    "utf8",
  );

  // Initialise git repo
  const git = (args: string[]) =>
    execFileSync("git", args, { cwd: CONFLICT_RESOLVE_SEED.repoDir, stdio: "pipe" });

  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);

  // BASE commit: conflict.txt = "line1\nBASE\nline3\n"
  // The Epic branch diverges from here.
  const conflictPath = path.join(CONFLICT_RESOLVE_SEED.repoDir, "conflict.txt");
  fs.writeFileSync(conflictPath, "line1\nBASE\nline3\n", "utf8");
  git(["add", "."]);
  git(["commit", "-m", "initial commit: add conflict.txt with BASE content"]);

  console.log("[conflict-resolve globalSetup] temp dirs ready:", CONFLICT_RESOLVE_SEED.base);
}
