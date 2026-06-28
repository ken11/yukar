/**
 * Global setup for reindex E2E scenario.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { REINDEX_SEED } from "./reindex-seed";

export default async function globalSetup(): Promise<void> {
  if (fs.existsSync(REINDEX_SEED.base)) {
    fs.rmSync(REINDEX_SEED.base, { recursive: true, force: true });
  }

  fs.mkdirSync(REINDEX_SEED.configDir, { recursive: true });
  fs.mkdirSync(REINDEX_SEED.workspaceDir, { recursive: true });
  fs.mkdirSync(REINDEX_SEED.repoDir, { recursive: true });

  const settingsYaml = `${[
    `workspace_root: "${REINDEX_SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(path.join(REINDEX_SEED.configDir, "settings.yaml"), settingsYaml, "utf8");

  // Initialize the git repo (including files to be indexed)
  const git = (args: string[]) =>
    execFileSync("git", args, { cwd: REINDEX_SEED.repoDir, stdio: "pipe" });
  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);

  // Add files to be included in the index
  const readmePath = path.join(REINDEX_SEED.repoDir, "README.md");
  fs.writeFileSync(readmePath, "# myrepo\nThis is the test repository for reindex E2E.\n", "utf8");
  const codePath = path.join(REINDEX_SEED.repoDir, "main.py");
  fs.writeFileSync(
    codePath,
    "def main():\n    print('hello')\n\nif __name__ == '__main__':\n    main()\n",
    "utf8",
  );

  git(["add", "."]);
  git(["commit", "-m", "initial commit"]);

  console.log("[reindex globalSetup] temp dirs ready:", REINDEX_SEED.base);
}
