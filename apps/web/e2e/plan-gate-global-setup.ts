/**
 * Global setup for the plan-approval-gate E2E scenario (bug ⑤).
 *
 * Prepares a temp dir and initialises a git repo. Server start/stop is managed
 * by webServer in playwright.config.plan-gate.ts. Unlike the pre-gate scenarios,
 * this one does NOT set YUKAR_REQUIRE_PLAN_APPROVAL=0 — the gate stays ON so the
 * scenario exercises it.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { PLAN_GATE_SEED } from "./plan-gate-seed";

export default async function globalSetup(): Promise<void> {
  if (fs.existsSync(PLAN_GATE_SEED.base)) {
    fs.rmSync(PLAN_GATE_SEED.base, { recursive: true, force: true });
  }

  fs.mkdirSync(PLAN_GATE_SEED.configDir, { recursive: true });
  fs.mkdirSync(PLAN_GATE_SEED.workspaceDir, { recursive: true });
  fs.mkdirSync(PLAN_GATE_SEED.repoDir, { recursive: true });

  const settingsYaml = `${[
    `workspace_root: "${PLAN_GATE_SEED.workspaceDir}"`,
    "llm:",
    "  provider: fake",
    "embedding:",
    "  provider: fake",
  ].join("\n")}\n`;
  fs.writeFileSync(path.join(PLAN_GATE_SEED.configDir, "settings.yaml"), settingsYaml, "utf8");

  const git = (args: string[]) =>
    execFileSync("git", args, { cwd: PLAN_GATE_SEED.repoDir, stdio: "pipe" });
  git(["-c", "init.defaultBranch=main", "init"]);
  git(["config", "user.name", "yukar-e2e"]);
  git(["config", "user.email", "e2e@yukar.local"]);
  fs.writeFileSync(path.join(PLAN_GATE_SEED.repoDir, "README.md"), "# myrepo\n", "utf8");
  git(["add", "."]);
  git(["commit", "-m", "initial commit"]);

  console.log("[plan-gate globalSetup] temp dirs ready:", PLAN_GATE_SEED.base);
}
