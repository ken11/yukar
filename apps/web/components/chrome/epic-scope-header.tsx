"use client";

/**
 * EpicScopeHeader — scope header in the axis language
 *
 * Structure (lines, spacing, and text instead of cards):
 *   Top row    — address (datum label): yukar / {project} / {EP-id}
 *                only the last segment (EP-id or epic) has .address-active (white)
 *   datum line — .edge-h (full-field-width horizontal rule)
 *   Bottom row — ‹back + epicId (mono) + vertical rule + epic title (text-title, 2-line wrap)
 *                right cluster: status + RunControlsBar + NewThread
 *   Running only: single cyan point at the left edge (.light-v + .light-live)
 *
 * Maintain the planned/blocked mapping in resolveStatus.
 * Do not break the sticky/flex structure (fix 1 already applied).
 */

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { RunControlsBar } from "@/components/features/epics/run-controls";
import { NewThreadModal } from "@/components/features/threads/new-thread-modal";
import { Icon } from "@/components/icon";
import { AddressLine } from "@/components/ui/address-line";
import type { StatusValue } from "@/components/ui/status-badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { getTasks } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { useT } from "@/lib/i18n/provider";
import { useEpicRun } from "./epic-run-context";
import { EpicSwitcher } from "./epic-switcher";

function resolveStatus(epicStatus: string | undefined, runStatus: string): StatusValue {
  // runStatus (from SSE) takes priority. epic.status is used only as the initial value.
  if (runStatus === "preparing") return "preparing";
  if (runStatus === "running") return "running";
  if (runStatus === "paused") return "paused";
  if (runStatus === "awaiting_input") return "awaiting";
  if (runStatus === "interrupted") return "interrupted";
  if (runStatus === "completed") return "completed";
  if (runStatus === "error") return "error";
  // idle / unknown: fall back to epic status
  if (epicStatus === "in_progress") return "in_progress";
  if (epicStatus === "completed") return "completed";
  if (epicStatus === "merged") return "merged";
  if (epicStatus === "closed") return "closed";
  if (epicStatus === "failed") return "error";
  if (epicStatus === "planned") return "planned";
  if (epicStatus === "blocked") return "blocked";
  return "idle";
}

/** Telemetry instrument — task progress displayed in .data mono */
function TelemetryInstrument({ tasksDone, tasksTotal }: { tasksDone: number; tasksTotal: number }) {
  const t = useT();
  if (tasksTotal === 0) return null;
  const label = t("a11y.tasksDoneOf")
    .replace("{done}", String(tasksDone))
    .replace("{total}", String(tasksTotal));
  return (
    <span
      className="hidden items-center gap-1 sm:inline-flex"
      title={label}
      role="status"
      aria-label={label}
    >
      <span className="data">
        {tasksDone}
        <span style={{ color: "var(--color-outline-variant)" }}>/</span>
        {tasksTotal}
      </span>
      <span
        className="font-mono uppercase"
        style={{
          fontSize: "10px",
          color: "var(--color-outline)",
          letterSpacing: "0.04em",
        }}
      >
        tasks
      </span>
    </span>
  );
}

export function EpicScopeHeader({ onStopRequest }: { onStopRequest: () => void }) {
  const t = useT();
  const { projectId, epicId, project, epic, activityState, setPausePending } = useEpicRun();

  const isRunning =
    activityState.runStatus === "running" || activityState.runStatus === "preparing";
  const status = resolveStatus(epic?.status, activityState.runStatus);

  // Task progress — same query key as EpicTabBar, so this will be a cache hit
  const { data: tasksFile } = useQuery({
    queryKey: queryKeys.tasks.get(projectId, epicId),
    queryFn: () => getTasks(projectId, epicId),
    staleTime: 30_000,
  });
  const tasksDone =
    tasksFile?.progress?.done ?? tasksFile?.tasks?.filter((t) => t.status === "done").length ?? 0;
  const tasksTotal = tasksFile?.progress?.total ?? tasksFile?.tasks?.length ?? 0;

  return (
    <div
      className={cn(
        "sticky top-0 z-20 edge-h",
        // Running only: single cyan point at the left edge (.light-v + .light-live)
        isRunning ? "light-v light-live" : "",
      )}
      style={{ backgroundColor: "var(--color-surface-header)" }}
    >
      {/* Top row: datum address band — edge-h provides the datum line (header surface receives light, boundary with field is the horizon) */}
      <div className="edge-h flex min-w-0 items-center overflow-hidden px-4 pt-2 pb-1.5 md:px-6">
        {/* AddressLine: min-w-0 + truncate prevents long paths from overflowing */}
        <div className="min-w-0 truncate">
          <AddressLine
            segments={[
              { label: "yukar", href: "/projects" },
              { label: project?.name ?? projectId, href: `/projects/${projectId}` },
              { label: epicId, active: true },
            ]}
          />
        </div>
      </div>

      {/* Bottom row: back + title + right cluster
       * Mobile: flex-col (vertical stack) — row 1 = title row, row 2 = controls
       * Desktop (md:): flex-row, single-line layout as before
       */}
      <div className="flex flex-col gap-2 px-4 py-2 md:flex-row md:items-center md:gap-x-4 md:px-6 md:min-h-[52px]">
        {/* Row 1: back chevron + Epic switcher */}
        <div className="flex min-w-0 items-center gap-x-2 md:flex-1">
          {/* Back chevron — shrink-0 keeps it always visible */}
          <Link
            href={`/projects/${projectId}/epics`}
            aria-label={t("nav.backToEpics")}
            className="shrink-0 text-on-surface-variant transition-colors hover:text-on-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-surface)]"
          >
            <Icon name="chevron_left" className="text-[22px]" />
          </Link>

          {/* Epic switcher: [EP-id │ title ∨] click opens sibling Epic dropdown */}
          {/* min-w-0: allows truncate to work inside flex-1 */}
          <h2 className="min-w-0 flex-1">
            <EpicSwitcher />
          </h2>
        </div>

        {/* Row 2: controls — flex-wrap on mobile, flex-nowrap on desktop */}
        <div className="flex flex-wrap items-center gap-2 md:flex-nowrap md:shrink-0 md:gap-3">
          <TelemetryInstrument tasksDone={tasksDone} tasksTotal={tasksTotal} />
          <StatusBadge status={status} />
          <RunControlsBar
            projectId={projectId}
            epicId={epicId}
            epicStatus={epic?.status}
            activityState={activityState}
            setPausePending={setPausePending}
            onStopRequest={onStopRequest}
          />
          <NewThreadModal projectId={projectId} epicId={epicId} />
        </div>
      </div>
    </div>
  );
}
