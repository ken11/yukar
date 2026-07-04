"use client";

import { type ReactNode, useMemo, useState } from "react";
import { Icon } from "@/components/icon";
import type { DiffResult } from "@/lib/api/endpoints";
import { cn } from "@/lib/cn";
import { buildFileTree, type FileTreeNode } from "@/lib/diff/file-tree";
import { useT } from "@/lib/i18n/provider";

type FileStat = DiffResult["files"][number];

const BASE_PAD = 8; // px, indentation of the outermost level
const INDENT_STEP = 12; // px added per nesting level
const FILE_EXTRA = 18; // px extra so file names align past a folder's chevron

// GitHub-style file tree for the changed-files panel (desktop). Folders are
// collapsible; single-child folder chains are compacted upstream in
// buildFileTree. Selection is by full path, unchanged from the flat list.
export function FileTree({
  files,
  selectedFile,
  onSelectFile,
  isLoading,
}: {
  files: DiffResult["files"];
  selectedFile: string;
  onSelectFile: (path: string) => void;
  isLoading: boolean;
}) {
  const t = useT();
  const tree = useMemo(() => buildFileTree(files), [files]);
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set<string>());

  const toggle = (path: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  if (files.length === 0) {
    if (isLoading) return null;
    return <p className="px-2 py-2 text-[11px] text-outline">{t("diff.noChangesInMode")}</p>;
  }

  const rows: ReactNode[] = [];
  const walk = (nodes: FileTreeNode<FileStat>[], depth: number) => {
    for (const node of nodes) {
      if (node.kind === "dir") {
        const isCollapsed = collapsed.has(node.path);
        rows.push(
          <button
            key={`d:${node.path}`}
            type="button"
            onClick={() => toggle(node.path)}
            title={node.path}
            aria-expanded={!isCollapsed}
            className="flex w-full items-center gap-1 py-1 pr-2 text-left text-on-surface-variant transition-colors hover:bg-surface-container hover:text-on-surface"
            style={{ paddingLeft: BASE_PAD + depth * INDENT_STEP }}
          >
            <Icon
              name="chevron_right"
              className={cn(
                "shrink-0 text-[16px] text-outline transition-transform",
                !isCollapsed && "rotate-90",
              )}
            />
            <Icon
              name={isCollapsed ? "folder" : "folder_open"}
              className="shrink-0 text-[14px] text-outline"
            />
            <span className="min-w-0 truncate font-mono text-[11px]">{node.name}</span>
          </button>,
        );
        if (!isCollapsed) walk(node.children, depth + 1);
      } else {
        const isSelected = node.path === selectedFile;
        rows.push(
          <button
            key={`f:${node.path}`}
            type="button"
            onClick={() => onSelectFile(node.path)}
            title={node.path}
            className={cn(
              "flex w-full items-center gap-2 py-1 pr-2 text-left transition-colors",
              isSelected
                ? "bg-surface-container-high text-on-surface"
                : "text-on-surface-variant hover:bg-surface-container hover:text-on-surface",
            )}
            style={{ paddingLeft: BASE_PAD + depth * INDENT_STEP + FILE_EXTRA }}
          >
            <span className="min-w-0 flex-1 truncate font-mono text-[11px]">{node.name}</span>
            <span className="data shrink-0">
              <span style={{ color: "var(--color-added)" }}>+{node.stat.added}</span>
              <span style={{ color: "var(--color-removed)" }}> −{node.stat.deleted}</span>
            </span>
          </button>,
        );
      }
    }
  };
  walk(tree, 0);

  return <div className="py-1">{rows}</div>;
}
