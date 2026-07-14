"use client";

/**
 * EpicScopeHeader — scope header in the axis language
 *
 * Structure (lines, spacing, and text instead of cards):
 *   One row — ‹back + project crumb + epicId (mono) + vertical rule + epic
 *             title (EpicSwitcher); right cluster: status + RunControlsBar
 *             (primary run action inline, secondary actions behind ⋯ on desktop)
 *   Running only: single cyan point at the left edge (.light-v + .light-live)
 *
 * Maintain the planned/blocked mapping in resolveStatus.
 * Do not break the sticky/flex structure (fix 1 already applied).
 */

import Link from "next/link";
import { useState } from "react";
import { RunControlsBar } from "@/components/features/epics/run-controls";
import { Icon } from "@/components/icon";
import type { StatusValue } from "@/components/ui/status-badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { cn } from "@/lib/cn";
import { useT } from "@/lib/i18n/provider";
import { useEpicRun } from "./epic-run-context";
import { EpicSwitcher } from "./epic-switcher";

export function resolveStatus(
  epicStatus: string | undefined,
  runStatus: string,
  hasParkedRun: boolean,
): StatusValue {
  // Active run states (from SSE) take priority — they reflect live execution.
  if (runStatus === "preparing") return "preparing";
  if (runStatus === "running") return "running";
  if (runStatus === "paused") return "paused";
  // Epic status is a single user-owned bit: completed wins over any stale run
  // state (no run can be active on a completed epic). "Merged" is a fact
  // attribute (epic.merged_at) rendered as a separate badge, not a status.
  if (epicStatus === "completed") return "completed";
  // A parked conversation (your turn) — "waiting" alone is the universal
  // resting state (a never-run epic is waiting too), so only the parked
  // marker shows the your-turn badge.
  if (runStatus === "waiting" && hasParkedRun) return "awaiting";
  // Open epic without an executing turn: fall back to the run status.
  if (runStatus === "completed") return "completed"; // JOB runs (resolve / arbiter)
  if (runStatus === "error") return "error";
  return "idle";
}

/**
 * Mobile-only compact status glyph (icon, no text). The full StatusBadge lives in
 * the controls row, which is collapsed behind the ⋯ toggle on mobile — this glyph
 * keeps the state visible without duplicating the badge's text in the DOM
 * (duplicate text/testids would break Playwright strict mode).
 */
const MOBILE_STATUS_GLYPH: Record<string, { icon: string; color?: string; labelKey: string }> = {
  running: {
    icon: "radio_button_checked",
    color: "var(--color-running)",
    labelKey: "epic.status.running",
  },
  preparing: { icon: "sync", labelKey: "epic.status.preparing" },
  awaiting: {
    icon: "pending_actions",
    color: "var(--color-light)",
    labelKey: "epic.status.awaiting",
  },
  paused: { icon: "pause", labelKey: "epic.status.paused" },
  completed: { icon: "check", labelKey: "epic.status.completed" },
  error: { icon: "error", color: "var(--color-error)", labelKey: "epic.status.error" },
  idle: { icon: "circle", labelKey: "epic.status.idle" },
};

export function EpicScopeHeader({ onStopRequest }: { onStopRequest: () => void }) {
  const t = useT();
  const { projectId, epicId, project, epic, activityState, setPausePending } = useEpicRun();

  // Mobile only: the controls row (status badge + run controls + new trial) is
  // collapsed behind a ⋯ toggle to give the conversation the vertical space.
  // Desktop (md:) ignores this state — the row is always inline there.
  const [mobileActionsOpen, setMobileActionsOpen] = useState(false);

  const isRunning =
    activityState.runStatus === "running" || activityState.runStatus === "preparing";
  const status = resolveStatus(
    epic?.status,
    activityState.runStatus,
    activityState.yourTurn != null,
  );

  return (
    <div
      className={cn(
        "sticky top-0 z-20 edge-h",
        // Running only: single cyan point at the left edge (.light-v + .light-live)
        isRunning ? "light-v light-live" : "",
      )}
      style={{
        backgroundColor: "var(--color-surface-header)",
        // Topmost element on mobile epic routes (the global top bar is hidden there)
        paddingTop: "env(safe-area-inset-top)",
      }}
    >
      {/* Single line: back + project crumb + Epic switcher + right cluster.
       * The old address band (yukar / project / EP-id) merged into this row —
       * the project crumb keeps the way up, the switcher already names the epic.
       * Mobile: flex-col (vertical stack) — row 1 = title row, row 2 = controls
       * Desktop (md:): flex-row, single-line layout as before
       */}
      <div className="flex flex-col gap-2 px-4 py-1.5 md:min-h-[44px] md:flex-row md:items-center md:gap-x-4 md:px-6 md:py-1">
        {/* Row 1: back chevron + project crumb + Epic switcher */}
        <div className="flex min-w-0 items-center gap-x-2 md:flex-1">
          {/* Back chevron — shrink-0 keeps it always visible */}
          <Link
            href={`/projects/${projectId}/epics`}
            aria-label={t("nav.backToEpics")}
            className="shrink-0 text-on-surface-variant transition-colors hover:text-on-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-surface)]"
          >
            <Icon name="chevron_left" className="text-[22px]" />
          </Link>

          {/* Project crumb — desktop only (mobile: the back chevron suffices) */}
          <Link
            href={`/projects/${projectId}`}
            className="address hidden shrink-0 max-w-[16ch] truncate transition-colors hover:text-on-surface md:inline"
          >
            {project?.name ?? projectId}
          </Link>
          <span
            aria-hidden
            className="address hidden shrink-0 md:inline"
            style={{ color: "var(--color-outline-variant)" }}
          >
            ／
          </span>

          {/* Epic switcher: [EP-id │ title ∨] click opens sibling Epic dropdown */}
          {/* min-w-0: allows truncate to work inside flex-1 */}
          <h2 className="min-w-0 flex-1">
            <EpicSwitcher />
          </h2>

          {/* Mobile only: compact status glyph — the full badge is inside the collapsed controls row */}
          {(() => {
            const glyph = MOBILE_STATUS_GLYPH[status] ?? MOBILE_STATUS_GLYPH.idle;
            return (
              <span
                className="flex shrink-0 items-center md:hidden"
                role="status"
                aria-label={t(glyph.labelKey)}
                title={t(glyph.labelKey)}
                style={{ color: glyph.color ?? "var(--color-on-surface-variant)" }}
              >
                <Icon
                  name={glyph.icon}
                  className={cn("text-[16px]", isRunning && "animate-pulse")}
                />
              </span>
            );
          })()}

          {/* Mobile only: ⋯ toggle for the controls row */}
          <button
            type="button"
            onClick={() => setMobileActionsOpen((v) => !v)}
            aria-expanded={mobileActionsOpen}
            aria-label={t("epic.actionsToggle")}
            title={t("epic.actionsToggle")}
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded text-on-surface-variant transition-colors hover:bg-surface-container hover:text-on-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-inset md:hidden"
          >
            <Icon name={mobileActionsOpen ? "expand_less" : "more_horiz"} className="text-[20px]" />
          </button>
        </div>

        {/* Row 2: controls — always inline on desktop; collapsed behind ⋯ on mobile */}
        <div
          className={cn(
            "flex-wrap items-center gap-2 md:flex md:flex-nowrap md:shrink-0 md:gap-3",
            mobileActionsOpen ? "flex" : "hidden",
          )}
        >
          {/* Task progress lives on the tab bar alone (タスク 1/3) — not repeated here. */}
          {/* Merge fact badge (epic.merged_at) — an attribute, independent of the status */}
          {epic?.merged_at && <StatusBadge status="merged" />}
          <StatusBadge status={status} />
          <RunControlsBar
            projectId={projectId}
            epicId={epicId}
            epicStatus={epic?.status}
            activityState={activityState}
            setPausePending={setPausePending}
            onStopRequest={onStopRequest}
          />
          {/* New trial / continue-on-branch live in the thread-list pane alone — not repeated here. */}
        </div>
      </div>
    </div>
  );
}
