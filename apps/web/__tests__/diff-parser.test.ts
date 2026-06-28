import { describe, expect, it } from "vitest";
import { parseUnifiedDiff } from "../lib/diff/parse-unified";
import { buildUnifiedFromRows, type DiffRowInput } from "./helpers/diff-test-helpers";

describe("parseUnifiedDiff", () => {
  it("parses a basic unified diff", () => {
    const raw = `--- a/src/foo.ts
+++ b/src/foo.ts
@@ -1,3 +1,4 @@
 function foo() {
-  return 1;
+  return 2;
+  // updated
 }`;

    const files = parseUnifiedDiff(raw);
    expect(files).toHaveLength(1);
    expect(files[0].oldPath).toBe("src/foo.ts");
    expect(files[0].newPath).toBe("src/foo.ts");

    const lines = files[0].lines;
    // header lines
    expect(lines.filter((l) => l.type === "header")).toHaveLength(2);
    // hunk line
    expect(lines.filter((l) => l.type === "hunk")).toHaveLength(1);
    // context lines
    const ctx = lines.filter((l) => l.type === "ctx");
    expect(ctx[0].text).toBe("function foo() {");
    expect(ctx[0].oldNo).toBe(1);
    expect(ctx[0].newNo).toBe(1);
    // del lines
    const dels = lines.filter((l) => l.type === "del");
    expect(dels).toHaveLength(1);
    expect(dels[0].text).toBe("  return 1;");
    expect(dels[0].oldNo).toBe(2);
    // add lines
    const adds = lines.filter((l) => l.type === "add");
    expect(adds).toHaveLength(2);
    expect(adds[0].text).toBe("  return 2;");
    expect(adds[0].newNo).toBe(2);
  });

  it("returns empty array for empty input", () => {
    expect(parseUnifiedDiff("")).toHaveLength(0);
  });

  it("handles multiple files", () => {
    const raw = `--- a/foo.ts
+++ b/foo.ts
@@ -1,1 +1,1 @@
-old
+new
--- a/bar.ts
+++ b/bar.ts
@@ -1,1 +1,1 @@
-a
+b`;
    const files = parseUnifiedDiff(raw);
    expect(files).toHaveLength(2);
    expect(files[0].oldPath).toBe("foo.ts");
    expect(files[1].oldPath).toBe("bar.ts");
  });
});

describe("parseUnifiedDiff edge cases", () => {
  it("does not treat '--- text' inside a hunk as a new file header", () => {
    // A deletion line whose content is '-- old heading' appears in the diff as '--- old heading'.
    // Because it is NOT followed by '+++ ', it must be treated as a deletion, not a file header.
    const raw = `--- a/src/foo.ts
+++ b/src/foo.ts
@@ -1,3 +1,3 @@
 context line
---- old heading
 context end`;

    const files = parseUnifiedDiff(raw);
    // Must be a single file — not split into two
    expect(files).toHaveLength(1);

    const lines = files[0].lines;
    const dels = lines.filter((l) => l.type === "del");

    // '---- old heading' slices to '--- old heading' (the content of the deleted line)
    expect(dels).toHaveLength(1);
    expect(dels[0].text).toBe("--- old heading");
  });

  it("ignores '\\ No newline at end of file' lines", () => {
    const raw = `--- a/src/bar.ts
+++ b/src/bar.ts
@@ -1,2 +1,2 @@
-old line
\\ No newline at end of file
+new line
\\ No newline at end of file`;

    const files = parseUnifiedDiff(raw);
    expect(files).toHaveLength(1);

    const lines = files[0].lines;
    const dels = lines.filter((l) => l.type === "del");
    const adds = lines.filter((l) => l.type === "add");

    expect(dels).toHaveLength(1);
    expect(dels[0].text).toBe("old line");
    expect(adds).toHaveLength(1);
    expect(adds[0].text).toBe("new line");

    // The "no newline" marker must NOT appear as any parsed line
    expect(lines.some((l) => l.text.includes("No newline"))).toBe(false);
  });

  it("handles content lines that start with '+++ ' in a hunk as additions", () => {
    const raw = `--- a/src/patch.ts
+++ b/src/patch.ts
@@ -1,2 +1,3 @@
 unchanged
++++ marker line
+regular add`;

    const files = parseUnifiedDiff(raw);
    expect(files).toHaveLength(1);

    const adds = files[0].lines.filter((l) => l.type === "add");
    expect(adds).toHaveLength(2);
    expect(adds[0].text).toBe("+++ marker line");
    expect(adds[1].text).toBe("regular add");
  });
});

describe("buildUnifiedFromRows", () => {
  it("builds a valid unified diff header", () => {
    const rows: DiffRowInput[] = [
      { type: "ctx", oldNo: 1, newNo: 1, text: "line one" },
      { type: "del", oldNo: 2, text: "old line" },
      { type: "add", newNo: 2, text: "new line" },
      { type: "ctx", oldNo: 3, newNo: 3, text: "line three" },
    ];
    const result = buildUnifiedFromRows("src/a.ts", "src/a.ts", rows);
    expect(result).toContain("--- a/src/a.ts");
    expect(result).toContain("+++ b/src/a.ts");
    expect(result).toContain("@@");
    expect(result).toContain("+new line");
    expect(result).toContain("-old line");
  });

  it("round-trips through parser", () => {
    const rows: DiffRowInput[] = [
      { type: "ctx", oldNo: 10, newNo: 10, text: "context" },
      { type: "del", oldNo: 11, text: "removed" },
      { type: "add", newNo: 11, text: "added" },
    ];
    const diff = buildUnifiedFromRows("x.ts", "x.ts", rows);
    const parsed = parseUnifiedDiff(diff);
    expect(parsed).toHaveLength(1);
    const dels = parsed[0].lines.filter((l) => l.type === "del");
    const adds = parsed[0].lines.filter((l) => l.type === "add");
    expect(dels[0].text).toBe("removed");
    expect(adds[0].text).toBe("added");
  });
});
