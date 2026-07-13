"use client";

import { useEffect, useRef, useState } from "react";
import { Icon } from "@/components/icon";
import type { ThreadEntry } from "@/lib/api/endpoints";
import { cn } from "@/lib/cn";
import { useT } from "@/lib/i18n/provider";
import { roleIcon } from "./message-row";
import { ThreadListPane } from "./thread-list-pane";

/**
 * TrialSwitcher — the P3 strip's conversation switcher (desktop only).
 *
 * The persistent 240px pane is gone; the SAME ThreadListPane (trials,
 * archived section, new-trial / continue-on-branch, agent tree) now lives in
 * a popover under the current conversation's name. Mobile keeps its drawer —
 * this component renders nothing below md.
 */
export function TrialSwitcher({
  projectId,
  epicId,
  currentThreadId,
  currentLabel,
  currentRole,
  initialThreads,
}: {
  projectId: string;
  epicId: string;
  currentThreadId: string;
  currentLabel: string;
  currentRole: string;
  initialThreads: ThreadEntry[];
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const target = e.target as Element | null;
      if (rootRef.current?.contains(target)) return;
      // The pane's modals (new trial / continue-on-branch) render through a
      // Radix portal OUTSIDE this subtree — interacting with them must not
      // count as an outside click.
      if (target?.closest('[role="dialog"]')) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={rootRef} className="relative hidden min-w-0 md:block">
      <button
        type="button"
        data-testid="trial-switcher-btn"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={open ? t("conversation.closeThreadList") : t("conversation.openThreadList")}
        onClick={() => setOpen((v) => !v)}
        className="flex min-w-0 items-center gap-2 rounded px-1.5 py-1 transition-colors hover:bg-surface-container focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white"
      >
        <Icon
          name={roleIcon[currentRole] ?? "chat"}
          className="shrink-0 text-[14px] text-on-surface-variant"
          aria-hidden
        />
        <span className="address truncate text-on-surface-variant">{currentLabel}</span>
        <Icon
          name={open ? "expand_less" : "expand_more"}
          className="shrink-0 text-[14px] text-outline"
          aria-hidden
        />
      </button>

      {/* Kept MOUNTED while closed (CSS-hidden): the pane hosts the new-trial /
          continue modals — unmounting on close would kill an open dialog. */}
      <div
        className={cn(
          "absolute left-0 top-[calc(100%+6px)] z-30 overflow-hidden rounded shadow-lg",
          open ? "block" : "hidden",
        )}
        style={{
          border: "1px solid var(--color-outline-variant)",
          backgroundColor: "var(--color-surface-container-low)",
        }}
      >
        <div className="h-[min(60vh,480px)] w-[260px]">
          <ThreadListPane
            projectId={projectId}
            epicId={epicId}
            currentThreadId={currentThreadId}
            initialThreads={initialThreads}
            onClose={() => setOpen(false)}
          />
        </div>
      </div>
    </div>
  );
}
