// Builds a GitHub-style file tree from a flat list of changed files.
// Pure (no React) so it can be unit-tested in isolation. Generic over any
// entry that carries a slash-separated `path`; the whole entry is preserved on
// each leaf as `stat` so callers can render per-file metadata (added/deleted).

export type FileTreeNode<T> =
  | { kind: "file"; name: string; path: string; stat: T }
  | { kind: "dir"; name: string; path: string; children: FileTreeNode<T>[] };

interface DirNode<T> {
  kind: "dir";
  name: string;
  path: string;
  children: FileTreeNode<T>[];
}

/**
 * Convert a flat file list into a nested folder tree.
 *
 * - Folders are sorted before files; both alphabetically (locale-aware).
 * - Single-child folder chains are compacted into one node (e.g. `apps/web/src`
 *   on a single row), matching GitHub's "collapse folders" behavior. The
 *   compacted node's `path` is the deepest segment so it stays unique for
 *   collapse-state keys.
 */
export function buildFileTree<T extends { path: string }>(files: readonly T[]): FileTreeNode<T>[] {
  const roots: FileTreeNode<T>[] = [];
  const dirByPath = new Map<string, DirNode<T>>();

  const childrenOf = (parentPath: string): FileTreeNode<T>[] => {
    if (parentPath === "") return roots;
    const dir = dirByPath.get(parentPath);
    return dir ? dir.children : roots;
  };

  for (const file of files) {
    const segments = file.path.split("/").filter(Boolean);
    if (segments.length === 0) continue;
    const fileName = segments[segments.length - 1];

    let parentPath = "";
    for (let i = 0; i < segments.length - 1; i++) {
      const seg = segments[i];
      const dirPath = parentPath === "" ? seg : `${parentPath}/${seg}`;
      if (!dirByPath.has(dirPath)) {
        const node: DirNode<T> = { kind: "dir", name: seg, path: dirPath, children: [] };
        childrenOf(parentPath).push(node);
        dirByPath.set(dirPath, node);
      }
      parentPath = dirPath;
    }
    childrenOf(parentPath).push({ kind: "file", name: fileName, path: file.path, stat: file });
  }

  sortNodes(roots);
  return roots.map(compactNode);
}

function sortNodes<T>(nodes: FileTreeNode<T>[]): void {
  nodes.sort((a, b) => {
    if (a.kind !== b.kind) return a.kind === "dir" ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  for (const node of nodes) {
    if (node.kind === "dir") sortNodes(node.children);
  }
}

function compactNode<T>(node: FileTreeNode<T>): FileTreeNode<T> {
  if (node.kind === "file") return node;
  let { name, path, children } = node;
  while (children.length === 1 && children[0].kind === "dir") {
    const only = children[0];
    name = `${name}/${only.name}`;
    path = only.path;
    children = only.children;
  }
  return { kind: "dir", name, path, children: children.map(compactNode) };
}
