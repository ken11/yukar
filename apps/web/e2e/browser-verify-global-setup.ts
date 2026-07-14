/**
 * Global setup for the browser verification E2E scenario.
 *
 * Creates three fixture git repos, each with a committed index.html (heading
 * "Hello Browser Verify") so the tree the host serves — trial worktree in
 * group B, base checkout in group C — really contains the page the headless
 * browser is asked to open.  Server start/stop is managed by the webServer
 * setting in playwright.config.browser-verify.ts.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { BROWSER_VERIFY_SEED, FIXTURE_INDEX_HTML } from "./browser-verify-seed";

function initRepo(repoDir: string): void {
  fs.mkdirSync(repoDir, { recursive: true });
  const git = (args: string[]) => execFileSync("git", args, { cwd: repoDir, stdio: "pipe" });
  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);
  fs.writeFileSync(path.join(repoDir, "index.html"), FIXTURE_INDEX_HTML, "utf8");
  git(["add", "."]);
  git(["commit", "-m", "initial commit"]);
}

export default async function globalSetup(): Promise<void> {
  // Clean up if the temp dir already exists
  if (fs.existsSync(BROWSER_VERIFY_SEED.base)) {
    fs.rmSync(BROWSER_VERIFY_SEED.base, { recursive: true, force: true });
  }

  fs.mkdirSync(BROWSER_VERIFY_SEED.configDir, { recursive: true });
  fs.mkdirSync(BROWSER_VERIFY_SEED.workspaceDir, { recursive: true });

  // Write settings.yaml
  const settingsYaml = `${[
    `workspace_root: "${BROWSER_VERIFY_SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(path.join(BROWSER_VERIFY_SEED.configDir, "settings.yaml"), settingsYaml, "utf8");

  initRepo(BROWSER_VERIFY_SEED.repoDirSettings);
  initRepo(BROWSER_VERIFY_SEED.repoDirAgent);
  initRepo(BROWSER_VERIFY_SEED.repoDirManager);

  console.log("[browser-verify globalSetup] temp dirs ready:", BROWSER_VERIFY_SEED.base);
}
