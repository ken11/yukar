/**
 * DiffLineRow — component that renders a single line of unified diff
 * Spinner — inline spinner shown while a resolve run is in progress
 *
 * #43: extracted from diff-page-client.tsx.
 * #48: props typed with Pick<DiffLine,...>.
 */

"use client";

import { cn } from "@/lib/cn";
import type { DiffLine } from "@/lib/diff/parse-unified";

/** #43: consolidate the 4 gutter <span> occurrences into LineNoCell */
function LineNoCell({
  className,
  style,
  children,
}: {
  className?: string;
  style?: React.CSSProperties;
  children?: React.ReactNode;
}) {
  return (
    <span
      className={cn(
        "w-12 shrink-0 border-r border-outline-variant/20 pr-2 text-right select-none",
        className,
      )}
      style={style}
    >
      {children ?? <>&nbsp;</>}
    </span>
  );
}

/** #48: props typed with Pick<DiffLine, ...> */
export type DiffLineRowProps = Pick<DiffLine, "type" | "oldNo" | "newNo" | "text">;

export function DiffLineRow({ type, oldNo, newNo, text }: DiffLineRowProps) {
  const base = "flex font-mono text-code-sm leading-[22px]";

  if (type === "header" || type === "hunk") {
    return (
      <div className={cn(base, "bg-surface-container text-outline")}>
        <span className="flex-1 whitespace-pre px-4">{text}</span>
      </div>
    );
  }

  if (type === "add") {
    return (
      <div className={cn(base)} style={{ backgroundColor: "var(--color-added-bg)" }}>
        <LineNoCell className="text-outline/50">&nbsp;</LineNoCell>
        <LineNoCell style={{ color: "color-mix(in oklab, var(--color-added) 70%, transparent)" }}>
          {newNo}
        </LineNoCell>
        <span
          className="w-5 shrink-0 select-none text-center"
          style={{ color: "var(--color-added)" }}
        >
          +
        </span>
        <span className="flex-1 whitespace-pre px-2 text-on-surface-variant">{text}</span>
      </div>
    );
  }

  if (type === "del") {
    return (
      <div className={cn(base)} style={{ backgroundColor: "var(--color-removed-bg)" }}>
        <LineNoCell style={{ color: "color-mix(in oklab, var(--color-removed) 70%, transparent)" }}>
          {oldNo}
        </LineNoCell>
        <LineNoCell className="text-outline/50">&nbsp;</LineNoCell>
        <span
          className="w-5 shrink-0 select-none text-center"
          style={{ color: "var(--color-removed)" }}
        >
          −
        </span>
        <span className="flex-1 whitespace-pre px-2 text-on-surface-variant">{text}</span>
      </div>
    );
  }

  // ctx
  return (
    <div className={cn(base, "hover:bg-white/[0.02]")}>
      <LineNoCell className="text-outline/50">{oldNo}</LineNoCell>
      <LineNoCell className="text-outline/50">{newNo}</LineNoCell>
      <span className="w-5 shrink-0 select-none">&nbsp;</span>
      <span className="flex-1 whitespace-pre px-2 text-on-surface-variant">{text}</span>
    </div>
  );
}

/** Inline spinner used during resolve run */
export function Spinner() {
  return (
    <span
      className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-outline border-t-secondary"
      aria-hidden
    />
  );
}
