/**
 * finding: diff-rename-binary
 *
 * parseUnifiedDiff silently discards blocks that have only a `diff --git` line
 * and no `---`/`+++` header pair (rename-only / binary / mode-only).
 *
 * Confirmed bugs are recorded with it.fails to keep the suite green.
 * Related cases that work as specified (rename with content) are normal PASS tests.
 */

import { describe, expect, it } from "vitest";
import { parseUnifiedDiff } from "../lib/diff/parse-unified";

// ---------------------------------------------------------------------------
// Helper: find a filename (from `diff --git`) in the DiffFile list
// ---------------------------------------------------------------------------
function hasFile(files: ReturnType<typeof parseUnifiedDiff>, path: string): boolean {
  return files.some((f) => f.oldPath === path || f.newPath === path);
}

// ---------------------------------------------------------------------------
// 1. Binary diff — no `---`/`+++` pair
//    Typical binary diff output from git diff
// ---------------------------------------------------------------------------
describe("parseUnifiedDiff – binary diff block", () => {
  const binaryDiff = [
    "diff --git a/assets/logo.png b/assets/logo.png",
    "index 3b18e51..8a9c4f2 100644",
    "Binary files a/assets/logo.png and b/assets/logo.png differ",
  ].join("\n");

  // Expected: return the binary file as a DiffFile entry (even as a placeholder).
  it("binary diff block should produce a DiffFile entry", () => {
    const files = parseUnifiedDiff(binaryDiff);
    expect(files).toHaveLength(1);
    expect(hasFile(files, "assets/logo.png")).toBe(true);
  });

  // Related: confirm that a subsequent regular file is not lost in a mixed diff containing a binary block
  // (subsequent file is parsed correctly even if the binary block is dropped)
  it("binary block does not corrupt parsing of subsequent text file", () => {
    const mixedDiff = [
      "diff --git a/assets/logo.png b/assets/logo.png",
      "index 3b18e51..8a9c4f2 100644",
      "Binary files a/assets/logo.png and b/assets/logo.png differ",
      "--- a/src/app.ts",
      "+++ b/src/app.ts",
      "@@ -1,1 +1,1 @@",
      "-old",
      "+new",
    ].join("\n");

    const files = parseUnifiedDiff(mixedDiff);
    // src/app.ts must always be included even if the binary block is dropped
    expect(hasFile(files, "src/app.ts")).toBe(true);
    // Its content is correctly parsed
    const appFile = files.find((f) => f.oldPath === "src/app.ts");
    expect(appFile).toBeDefined();
    const adds = appFile!.lines.filter((l) => l.type === "add");
    expect(adds).toHaveLength(1);
    expect(adds[0].text).toBe("new");
  });
});

// ---------------------------------------------------------------------------
// 2. Rename-only diff (similarity 100%, text file) — no `---`/`+++` pair
//    Format commonly seen with `git diff --diff-filter=R`
// ---------------------------------------------------------------------------
describe("parseUnifiedDiff – rename-only diff block", () => {
  const renameOnlyDiff = [
    "diff --git a/lib/old-name.ts b/lib/new-name.ts",
    "similarity index 100%",
    "rename from lib/old-name.ts",
    "rename to lib/new-name.ts",
  ].join("\n");

  // rename-only has no content change. Expected: return 1 DiffFile (oldPath=old-name / newPath=new-name).
  it("rename-only block should produce a DiffFile with correct old/new paths", () => {
    const files = parseUnifiedDiff(renameOnlyDiff);
    expect(files).toHaveLength(1);
    expect(files[0].oldPath).toContain("old-name.ts");
    expect(files[0].newPath).toContain("new-name.ts");
  });

  // Confirm no impact on subsequent files (same perspective as binary)
  it("rename-only block does not corrupt subsequent text file parsing", () => {
    const mixedDiff = [
      "diff --git a/lib/old-name.ts b/lib/new-name.ts",
      "similarity index 100%",
      "rename from lib/old-name.ts",
      "rename to lib/new-name.ts",
      "--- a/src/changed.ts",
      "+++ b/src/changed.ts",
      "@@ -1,1 +1,1 @@",
      "-before",
      "+after",
    ].join("\n");

    const files = parseUnifiedDiff(mixedDiff);
    expect(hasFile(files, "src/changed.ts")).toBe(true);
    const changedFile = files.find((f) => f.oldPath === "src/changed.ts");
    expect(changedFile).toBeDefined();
    const dels = changedFile!.lines.filter((l) => l.type === "del");
    expect(dels[0].text).toBe("before");
  });
});

// ---------------------------------------------------------------------------
// 3. Rename + content change (similarity < 100%) — has `---`/`+++` pair
//    This format is correctly parsed by existing logic (PASS is expected)
// ---------------------------------------------------------------------------
describe("parseUnifiedDiff – rename with content change (has --- / +++ pair)", () => {
  const renameWithContentDiff = [
    "diff --git a/lib/utils.ts b/lib/helpers.ts",
    "similarity index 80%",
    "rename from lib/utils.ts",
    "rename to lib/helpers.ts",
    "index 4a3f2b1..9e8d7c6 100644",
    "--- a/lib/utils.ts",
    "+++ b/lib/helpers.ts",
    "@@ -1,3 +1,4 @@",
    " export function identity<T>(x: T): T {",
    "-  return x",
    "+  // renamed helper",
    "+  return x;",
    " }",
  ].join("\n");

  it("rename with content change is parsed correctly (has --- / +++ pair)", () => {
    const files = parseUnifiedDiff(renameWithContentDiff);
    expect(files).toHaveLength(1);
    // oldPath has the a/ prefix stripped
    expect(files[0].oldPath).toBe("lib/utils.ts");
    expect(files[0].newPath).toBe("lib/helpers.ts");

    const adds = files[0].lines.filter((l) => l.type === "add");
    const dels = files[0].lines.filter((l) => l.type === "del");
    expect(dels).toHaveLength(1);
    expect(dels[0].text).toBe("  return x");
    expect(adds).toHaveLength(2);
    expect(adds[0].text).toBe("  // renamed helper");
  });
});

// ---------------------------------------------------------------------------
// 4. Mode-only diff (chmod changes etc.) — no `---`/`+++` pair
// ---------------------------------------------------------------------------
describe("parseUnifiedDiff – mode-only diff block", () => {
  const modeOnlyDiff = [
    "diff --git a/scripts/deploy.sh b/scripts/deploy.sh",
    "old mode 100644",
    "new mode 100755",
  ].join("\n");

  // Even a mode-only change should appear as a DiffFile entry.
  it("mode-only diff block should produce a DiffFile entry", () => {
    const files = parseUnifiedDiff(modeOnlyDiff);
    expect(files).toHaveLength(1);
    expect(hasFile(files, "scripts/deploy.sh")).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// G9 regression test: `diff --git` + `--- /dev/null` format for new/deleted files
//   `diff --git a/X b/X` creates a placeholder; the regression being detected is
//   placeholderMatches comparison with `--- /dev/null` failing, producing 2 DiffFiles.
// ---------------------------------------------------------------------------
describe("parseUnifiedDiff – new file (--- /dev/null)", () => {
  const newFileDiff = [
    "diff --git a/src/new.ts b/src/new.ts",
    "new file mode 100644",
    "index 0000000..abc1234",
    "--- /dev/null",
    "+++ b/src/new.ts",
    "@@ -0,0 +1,3 @@",
    "+export function hello() {",
    "+  return 'world';",
    "+}",
  ].join("\n");

  it("new file diff produces exactly 1 DiffFile (no duplication)", () => {
    const files = parseUnifiedDiff(newFileDiff);
    expect(files).toHaveLength(1);
  });

  it("new file newPath is correct", () => {
    const files = parseUnifiedDiff(newFileDiff);
    expect(files[0].newPath).toBe("src/new.ts");
  });

  it("new file hunk lines are included (content pane is not empty)", () => {
    const files = parseUnifiedDiff(newFileDiff);
    const adds = files[0].lines.filter((l) => l.type === "add");
    expect(adds.length).toBeGreaterThan(0);
    expect(adds[0].text).toBe("export function hello() {");
  });
});

describe("parseUnifiedDiff – deleted file (+++ /dev/null)", () => {
  const deletedFileDiff = [
    "diff --git a/src/old.ts b/src/old.ts",
    "deleted file mode 100644",
    "index abc1234..0000000",
    "--- a/src/old.ts",
    "+++ /dev/null",
    "@@ -1,3 +0,0 @@",
    "-export function bye() {",
    "-  return 'gone';",
    "-}",
  ].join("\n");

  it("deleted file diff produces exactly 1 DiffFile (no duplication)", () => {
    const files = parseUnifiedDiff(deletedFileDiff);
    expect(files).toHaveLength(1);
  });

  it("deleted file oldPath is correct", () => {
    const files = parseUnifiedDiff(deletedFileDiff);
    expect(files[0].oldPath).toBe("src/old.ts");
  });

  it("deleted file hunk lines are included (content pane is not empty)", () => {
    const files = parseUnifiedDiff(deletedFileDiff);
    const dels = files[0].lines.filter((l) => l.type === "del");
    expect(dels.length).toBeGreaterThan(0);
    expect(dels[0].text).toBe("export function bye() {");
  });
});

describe("parseUnifiedDiff – mixed diff including new/deleted files", () => {
  const mixedWithNewAndDeleted = [
    // new file
    "diff --git a/src/added.ts b/src/added.ts",
    "new file mode 100644",
    "--- /dev/null",
    "+++ b/src/added.ts",
    "@@ -0,0 +1,1 @@",
    "+const x = 1;",
    // deleted file
    "diff --git a/src/removed.ts b/src/removed.ts",
    "deleted file mode 100644",
    "--- a/src/removed.ts",
    "+++ /dev/null",
    "@@ -1,1 +0,0 @@",
    "-const y = 2;",
    // normal change
    "diff --git a/src/changed.ts b/src/changed.ts",
    "--- a/src/changed.ts",
    "+++ b/src/changed.ts",
    "@@ -1,1 +1,1 @@",
    "-old",
    "+new",
  ].join("\n");

  it("mixed diff with new/deleted/changed files produces 3 DiffFiles", () => {
    const files = parseUnifiedDiff(mixedWithNewAndDeleted);
    expect(files).toHaveLength(3);
  });

  it("each file in the mixed diff has the correct path", () => {
    const files = parseUnifiedDiff(mixedWithNewAndDeleted);
    expect(hasFile(files, "src/added.ts")).toBe(true);
    expect(hasFile(files, "src/removed.ts")).toBe(true);
    expect(hasFile(files, "src/changed.ts")).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 5. Mixed diff with multiple files (binary + rename-only + normal)
//    Currently binary/rename-only are dropped so files.length is 1 → FAIL
// ---------------------------------------------------------------------------
describe("parseUnifiedDiff – mixed diff with binary and rename-only", () => {
  const mixedDiff = [
    // binary
    "diff --git a/img.png b/img.png",
    "index abc..def 100644",
    "Binary files a/img.png and b/img.png differ",
    // rename-only
    "diff --git a/src/old.ts b/src/new.ts",
    "similarity index 100%",
    "rename from src/old.ts",
    "rename to src/new.ts",
    // mode-only
    "diff --git a/run.sh b/run.sh",
    "old mode 100644",
    "new mode 100755",
    // normal text change
    "--- a/src/index.ts",
    "+++ b/src/index.ts",
    "@@ -1,1 +1,1 @@",
    "-const x = 1",
    "+const x = 2",
  ].join("\n");

  it("mixed diff should produce 4 DiffFile entries (binary + rename + mode + text)", () => {
    const files = parseUnifiedDiff(mixedDiff);
    expect(files).toHaveLength(4);
  });

  // Normal text change is already parsed correctly (recorded to prevent regression)
  it("text file in mixed diff is always parsed correctly", () => {
    const files = parseUnifiedDiff(mixedDiff);
    expect(hasFile(files, "src/index.ts")).toBe(true);
    const textFile = files.find((f) => f.oldPath === "src/index.ts");
    expect(textFile).toBeDefined();
    const dels = textFile!.lines.filter((l) => l.type === "del");
    expect(dels[0].text).toBe("const x = 1");
  });
});
