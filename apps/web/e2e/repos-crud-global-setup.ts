/**
 * Global setup for the repos add/delete (CRUD) E2E scenario.
 *
 * Creates two independent local git repos (alpha, beta) so the spec can
 * register one at project-creation time and add the other via the Repos page.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { REPOS_CRUD_SEED } from "./repos-crud-seed";

function initRepo(repoDir: string, name: string): void {
  fs.mkdirSync(repoDir, { recursive: true });
  const git = (args: string[]) => execFileSync("git", args, { cwd: repoDir, stdio: "pipe" });
  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);
  fs.writeFileSync(path.join(repoDir, "README.md"), `# ${name}\nRepos CRUD E2E fixture.\n`, "utf8");
  git(["add", "."]);
  git(["commit", "-m", "initial commit"]);
}

export default async function globalSetup(): Promise<void> {
  if (fs.existsSync(REPOS_CRUD_SEED.base)) {
    fs.rmSync(REPOS_CRUD_SEED.base, { recursive: true, force: true });
  }

  fs.mkdirSync(REPOS_CRUD_SEED.configDir, { recursive: true });
  fs.mkdirSync(REPOS_CRUD_SEED.workspaceDir, { recursive: true });

  const settingsYaml = `${[
    `workspace_root: "${REPOS_CRUD_SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(path.join(REPOS_CRUD_SEED.configDir, "settings.yaml"), settingsYaml, "utf8");

  initRepo(REPOS_CRUD_SEED.repoDirA, "alpha");
  initRepo(REPOS_CRUD_SEED.repoDirB, "beta");

  console.log("[repos-crud globalSetup] temp dirs ready:", REPOS_CRUD_SEED.base);
}
