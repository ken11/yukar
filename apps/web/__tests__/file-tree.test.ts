import { describe, expect, it } from "vitest";
import { buildFileTree, type FileTreeNode } from "../lib/diff/file-tree";

interface Stat {
  path: string;
  added: number;
  deleted: number;
}

const stat = (path: string): Stat => ({ path, added: 1, deleted: 0 });

// Flatten the visible tree into "depth:kind:name" lines for concise assertions.
function flatten<T>(nodes: FileTreeNode<T>[], depth = 0, out: string[] = []): string[] {
  for (const node of nodes) {
    out.push(`${depth}:${node.kind}:${node.name}`);
    if (node.kind === "dir") flatten(node.children, depth + 1, out);
  }
  return out;
}

describe("buildFileTree", () => {
  it("nests files under their folders", () => {
    const tree = buildFileTree([stat("apps/web/a.ts"), stat("apps/api/b.py")]);
    // apps has two children (api, web); folders sorted alphabetically.
    expect(flatten(tree)).toEqual([
      "0:dir:apps",
      "1:dir:api",
      "2:file:b.py",
      "1:dir:web",
      "2:file:a.ts",
    ]);
  });

  it("compacts single-child folder chains into one row", () => {
    const tree = buildFileTree([stat("apps/web/components/features/diff/x.tsx")]);
    expect(flatten(tree)).toEqual(["0:dir:apps/web/components/features/diff", "1:file:x.tsx"]);
    // The compacted node keeps the deepest path so collapse-keys stay unique.
    expect(tree[0].path).toBe("apps/web/components/features/diff");
  });

  it("does not compact a folder that has multiple children", () => {
    const tree = buildFileTree([stat("src/a.ts"), stat("src/sub/b.ts")]);
    expect(flatten(tree)).toEqual(["0:dir:src", "1:dir:sub", "2:file:b.ts", "1:file:a.ts"]);
  });

  it("sorts folders before files at each level", () => {
    const tree = buildFileTree([stat("z.ts"), stat("dir/a.ts")]);
    expect(flatten(tree)).toEqual(["0:dir:dir", "1:file:a.ts", "0:file:z.ts"]);
  });

  it("handles root-level files with no folder", () => {
    const tree = buildFileTree([stat("hello.py"), stat("util.py")]);
    expect(flatten(tree)).toEqual(["0:file:hello.py", "0:file:util.py"]);
  });

  it("preserves the original entry on each leaf", () => {
    const tree = buildFileTree([{ path: "a/b.ts", added: 4, deleted: 2 }]);
    const leaf = (tree[0] as Extract<FileTreeNode<Stat>, { kind: "dir" }>).children[0];
    expect(leaf.kind).toBe("file");
    if (leaf.kind === "file") {
      expect(leaf.stat).toEqual({ path: "a/b.ts", added: 4, deleted: 2 });
    }
  });

  it("returns an empty array for no files", () => {
    expect(buildFileTree([])).toEqual([]);
  });
});
