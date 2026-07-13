import { describe, expect, it } from "vitest";
import { parseKickoff } from "@/lib/conversation/kickoff";

const EPIC_KICKOFF = [
  "# Epic: 設定読み込みの共通化と CLI 整備",
  "",
  "src/app.py と src/cli.py に散らばった設定読み込みを config.py へ一本化する。",
  "",
  "# Acceptance Criteria",
  "",
  "config.load() が dict を返す。",
  "",
  "These criteria define the overall done-conditions for this epic. Each task's `contract` must together ensure these criteria are met. The Evaluator will use these to assess final correctness.",
  "",
  "First, inspect the repositories with `repo_summarize` (and `repo_search` as needed) to understand the actual codebase.",
].join("\n");

const TASK_KICKOFF = [
  "# Task: T1 — 設定読み込みの共通化",
  "",
  "Working directory: `/tmp/worktrees/manager/myrepo`",
  "",
  "# Task Contract",
  "",
  "src/config.py を新設し、読み込みを一本化する。",
  "",
  "Implement exactly what the contract specifies. The Evaluator will verify your work against this contract.",
].join("\n");

const REVIEWER_KICKOFF = [
  "# Epic under review: 設定読み込みの共通化と CLI 整備",
  "",
  "説明文。",
  "",
  "# Acceptance Criteria",
  "",
  "config.load() が dict を返す。",
  "",
  "These are the objective done-conditions. Judge whether the branch actually meets every one of them.",
].join("\n");

describe("parseKickoff", () => {
  it("parses the Manager epic kickoff: title, description, AC without boilerplate", () => {
    const v = parseKickoff(EPIC_KICKOFF);
    expect(v).not.toBeNull();
    expect(v?.kind).toBe("epic");
    expect(v?.title).toBe("設定読み込みの共通化と CLI 整備");
    expect(v?.sections[0]).toEqual({
      label: null,
      text: "src/app.py と src/cli.py に散らばった設定読み込みを config.py へ一本化する。",
    });
    const ac = v?.sections.find((s) => s.label === "Acceptance Criteria");
    expect(ac?.text).toBe("config.load() が dict を返す。");
    expect(ac?.text).not.toContain("These criteria define");
  });

  it("parses the Worker task hand-off: title + contract; working dir stays folded", () => {
    const v = parseKickoff(TASK_KICKOFF);
    expect(v?.kind).toBe("task");
    expect(v?.title).toBe("T1 — 設定読み込みの共通化");
    const contract = v?.sections.find((s) => s.label === "Contract");
    expect(contract?.text).toBe("src/config.py を新設し、読み込みを一本化する。");
    expect(contract?.text).not.toContain("Implement exactly");
    // The working-directory line is not a visible section.
    expect(v?.sections.some((s) => s.text.includes("Working directory"))).toBe(false);
  });

  it("parses the Evaluator variant heading (Task Contract (primary evaluation criterion))", () => {
    const text = TASK_KICKOFF.replace(
      "# Task Contract",
      "# Task Contract (primary evaluation criterion)",
    );
    const v = parseKickoff(text);
    expect(v?.sections.find((s) => s.label === "Contract")?.text).toContain("src/config.py");
  });

  it("parses the Reviewer seed as an epic kickoff", () => {
    const v = parseKickoff(REVIEWER_KICKOFF);
    expect(v?.kind).toBe("epic");
    expect(v?.title).toBe("設定読み込みの共通化と CLI 整備");
    const ac = v?.sections.find((s) => s.label === "Acceptance Criteria");
    expect(ac?.text).toBe("config.load() が dict を返す。");
  });

  it("returns null for a plain user reply (renders as-is)", () => {
    expect(parseKickoff("進めてください。")).toBeNull();
    expect(parseKickoff("# メモ\n\nこれは kickoff ではない")).toBeNull();
    expect(parseKickoff("")).toBeNull();
  });
});
