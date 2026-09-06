import { describe, expect, it } from "vitest";
import { parseUnifiedDiff } from "../lib/diff/parse-unified";
import { selectDiffFile } from "../lib/diff/select-file";

// A Terraform-shaped diff: the same file name at the repository root and
// inside a module. The root file sorts first, which is what used to make it
// shadow every nested `main.tf`.
const TERRAFORM_DIFF = `diff --git a/main.tf b/main.tf
--- a/main.tf
+++ b/main.tf
@@ -1,2 +1,2 @@
 module "vpc" {
-  cidr = "10.0.0.0/16"
+  cidr = "10.1.0.0/16"
diff --git a/modules/vpc/main.tf b/modules/vpc/main.tf
--- a/modules/vpc/main.tf
+++ b/modules/vpc/main.tf
@@ -1,2 +1,2 @@
 resource "aws_vpc" "this" {
-  enable_dns_support = false
+  enable_dns_support = true
diff --git a/envs/prod/main.tf b/envs/prod/main.tf
--- a/envs/prod/main.tf
+++ b/envs/prod/main.tf
@@ -1,2 +1,2 @@
 locals {
-  env = "stg"
+  env = "prod"
`;

describe("selectDiffFile", () => {
  const files = parseUnifiedDiff(TERRAFORM_DIFF);

  it("parses every block of the fixture", () => {
    expect(files.map((f) => f.newPath)).toEqual([
      "main.tf",
      "modules/vpc/main.tf",
      "envs/prod/main.tf",
    ]);
  });

  it.each([
    "main.tf",
    "modules/vpc/main.tf",
    "envs/prod/main.tf",
  ])("returns the block for %s and not a same-named file from another directory", (path) => {
    const picked = selectDiffFile(files, path);
    expect(picked?.newPath).toBe(path);
  });

  it("matches a deleted file by its old path", () => {
    const deleted = parseUnifiedDiff(`diff --git a/modules/vpc/outputs.tf b/modules/vpc/outputs.tf
--- a/modules/vpc/outputs.tf
+++ /dev/null
@@ -1,1 +0,0 @@
-output "id" {}
`);
    expect(selectDiffFile(deleted, "modules/vpc/outputs.tf")?.oldPath).toBe(
      "modules/vpc/outputs.tf",
    );
  });

  it("falls back to a segment-boundary suffix when the diff paths carry a prefix", () => {
    const prefixed = [
      { oldPath: "w/src/app.ts", newPath: "w/src/app.ts", lines: [] },
      { oldPath: "w/src/other.ts", newPath: "w/src/other.ts", lines: [] },
    ];
    expect(selectDiffFile(prefixed, "src/app.ts")?.newPath).toBe("w/src/app.ts");
  });

  it("returns undefined rather than a guess when the suffix match is ambiguous", () => {
    const ambiguous = [
      { oldPath: "a/main.tf", newPath: "a/main.tf", lines: [] },
      { oldPath: "b/main.tf", newPath: "b/main.tf", lines: [] },
    ];
    expect(selectDiffFile(ambiguous, "main.tf")).toBeUndefined();
  });

  it("returns undefined when the selected file is absent from the diff", () => {
    expect(selectDiffFile(files, "unrelated/versions.tf")).toBeUndefined();
  });

  it("shows the first block when nothing is selected yet", () => {
    expect(selectDiffFile(files, "")?.newPath).toBe("main.tf");
    expect(selectDiffFile([], "")).toBeUndefined();
  });
});
