"use client";

/**
 * EpicSidebar — the persistent desktop chrome for an epic (≥ md).
 *
 * Replaces the old stack of horizontal bands (scope header + tab bar + in-chat
 * role bar) with a single 320px left column, so the right pane is a clean,
 * full-height conversation (no header/footer). Sits to the right of the 56px
 * global rail; its own .edge-v ridge is the boundary with the chat field.
 *
 * Top → bottom:
 *   back → Epics · EpicSwitcher · status
 *   run controls (one filled primary; Reviewer / Complete are quiet ghost rows)
 *   section nav (Conversation / Tasks / Diff / Docs) — the vertical tab bar
 *   thread list + live agent tree (ThreadListPane, scrolls)
 *   Manager effort — a quiet run setting docked at the foot
 *
 * Mounted only on desktop (EpicShell gates on useIsDesktop) so its run-control
 * / effort / thread testids never collide with the mobile chrome's copies.
 */

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useSelectedLayoutSegments } from "next/navigation";
import { ManagerEffortControl } from "@/components/features/conversation/manager-effort-control";
import { ThreadListPane } from "@/components/features/conversation/thread-list-pane";
import { RunControlsBar } from "@/components/features/epics/run-controls";
import { Icon } from "@/components/icon";
import { StatusBadge } from "@/components/ui/status-badge";
import { getGitDiffSummary, getTasks } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { resolveActiveManagerThreadId } from "@/lib/epic-utils";
import { useT } from "@/lib/i18n/provider";
import { useEpicRun } from "./epic-run-context";
import { resolveStatus } from "./epic-scope-header";
import { EpicSwitcher } from "./epic-switcher";

export function EpicSidebar({
  initialThreads,
  onStopRequest,
}: {
  initialThreads: import("@/lib/api/endpoints").ThreadEntry[];
  onStopRequest: () => void;
}) {
  const t = useT();
  const { projectId, epicId, project, epic, activityState, setPausePending } = useEpicRun();

  // Segments below the epic layout: ["threads", id] | ["tasks"] | ["diff"] | ["docs"].
  const segments = useSelectedLayoutSegments();
  const section = segments[0] ?? "threads";
  const currentThreadId = section === "threads" ? (segments[1] ?? "") : "";

  const isRunning =
    activityState.runStatus === "running" || activityState.runStatus === "preparing";
  const isEpicCompleted = epic?.status === "completed";
  const status = resolveStatus(
    epic?.status,
    activityState.runStatus,
    activityState.yourTurn != null,
  );

  const managerThreadId = activityState.activeTrialId ?? resolveActiveManagerThreadId(epic, []);
  const base = `/projects/${projectId}/epics/${epicId}`;

  // Section badges (shared cache with the mobile tab bar — no extra fetch).
  const { data: tasksFile } = useQuery({
    queryKey: queryKeys.tasks.get(projectId, epicId),
    queryFn: () => getTasks(projectId, epicId),
    staleTime: 30_000,
  });
  const tasksDone =
    tasksFile?.progress?.done ?? tasksFile?.tasks?.filter((tk) => tk.status === "done").length ?? 0;
  const tasksTotal = tasksFile?.progress?.total ?? tasksFile?.tasks?.length ?? 0;

  const { data: diffSummary } = useQuery({
    queryKey: queryKeys.git.diffSummary(projectId, epicId, "working"),
    queryFn: () => getGitDiffSummary(projectId, epicId, "working"),
    staleTime: 30_000,
  });
  const diffAdded = diffSummary?.repos?.reduce((s, r) => s + (r.added ?? 0), 0) ?? 0;
  const diffRemoved = diffSummary?.repos?.reduce((s, r) => s + (r.deleted ?? 0), 0) ?? 0;
  const hasDiff = diffAdded > 0 || diffRemoved > 0;

  const navItems = [
    {
      key: "threads",
      href: `${base}/threads/${managerThreadId}`,
      label: t("epic.tabs.conversation"),
      icon: "forum",
    },
    {
      key: "tasks",
      href: `${base}/tasks`,
      label: t("epic.tabs.tasks"),
      icon: "checklist",
      badge:
        tasksTotal > 0 ? (
          <span className="data">
            {tasksDone}/{tasksTotal}
          </span>
        ) : undefined,
    },
    {
      key: "diff",
      href: `${base}/diff`,
      label: t("epic.tabs.diff"),
      icon: "difference",
      badge: hasDiff ? (
        <span className="data">
          <span style={{ color: "var(--color-added)" }}>+{diffAdded}</span>{" "}
          <span style={{ color: "var(--color-removed)" }}>−{diffRemoved}</span>
        </span>
      ) : undefined,
    },
    {
      key: "docs",
      href: `${base}/docs`,
      label: t("epic.tabs.docs"),
      icon: "description",
    },
  ];

  const hairline = { borderBottom: "1px solid var(--color-outline-variant)" };

  return (
    <aside
      aria-label={t("nav.epicNav")}
      className={cn(
        "flex h-full w-[320px] shrink-0 flex-col bg-surface-container-low edge-v",
        // Running only: single cyan point at the top-left edge.
        isRunning ? "light-v light-live" : "",
      )}
    >
      {/* Nameplate — stacked so the epic title gets a full row (a 320px column
          cannot hold crumb + EP-id + title on one line). */}
      <div className="flex shrink-0 flex-col gap-1.5 px-3 py-2.5" style={hairline}>
        {/* Row 1: back → Epics + project crumb */}
        <div className="flex min-w-0 items-center gap-1.5">
          <Link
            href={`/projects/${projectId}/epics`}
            aria-label={t("nav.backToEpics")}
            title={t("nav.backToEpics")}
            className="-ml-1 shrink-0 text-on-surface-variant transition-colors hover:text-on-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-surface-container-low)]"
          >
            <Icon name="chevron_left" className="text-[20px]" />
          </Link>
          <Link
            href={`/projects/${projectId}`}
            className="address min-w-0 flex-1 truncate transition-colors hover:text-on-surface"
          >
            {project?.name ?? projectId}
          </Link>
        </div>
        {/* Row 2: EpicSwitcher (EP-id │ title ∨) */}
        <h2 className="min-w-0">
          <EpicSwitcher compact />
        </h2>
        {/* Row 3: status */}
        <div className="flex flex-wrap items-center gap-2">
          {epic?.merged_at && <StatusBadge status="merged" />}
          <StatusBadge status={status} />
        </div>
      </div>

      {/* Run controls (flat, no ⋯). Separated by spacing, not a hard line. */}
      <div className="flex shrink-0 flex-col gap-1.5 px-3 pt-3 pb-1">
        <RunControlsBar
          projectId={projectId}
          epicId={epicId}
          epicStatus={epic?.status}
          activityState={activityState}
          setPausePending={setPausePending}
          onStopRequest={onStopRequest}
          layout="stack"
        />
      </div>

      {/* Section nav — the vertical tab bar */}
      <nav aria-label={t("nav.epicSections")} className="flex shrink-0 flex-col pb-1">
        {navItems.map((item) => {
          const active = item.key === section;
          return (
            <Link
              key={item.key}
              href={item.href}
              aria-current={active ? "page" : undefined}
              className={cn(
                "flex items-center gap-2.5 px-4 py-2 text-body-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-inset",
                active
                  ? "font-medium text-on-surface"
                  : "text-on-surface-variant hover:bg-surface-container hover:text-on-surface",
              )}
              style={active ? { boxShadow: "inset 2px 0 0 0 var(--color-on-surface)" } : undefined}
            >
              <Icon
                name={item.icon}
                filled={active}
                className={cn(
                  "shrink-0 text-[18px]",
                  active ? "text-on-surface" : "text-on-surface-variant",
                )}
                aria-hidden
              />
              <span className="min-w-0 flex-1 truncate">{item.label}</span>
              {item.badge}
            </Link>
          );
        })}
      </nav>

      {/* Thread list + live agent tree (scrolls) */}
      <div className="min-h-0 flex-1">
        <ThreadListPane
          projectId={projectId}
          epicId={epicId}
          currentThreadId={currentThreadId}
          initialThreads={initialThreads}
          variant="sidebar"
        />
      </div>

      {/* Manager effort — a quiet run setting, docked at the foot. */}
      {!isEpicCompleted && (
        <div
          className="shrink-0 px-3 py-2"
          style={{ borderTop: "1px solid var(--color-outline-variant)" }}
        >
          <ManagerEffortControl projectId={projectId} epicId={epicId} />
        </div>
      )}
    </aside>
  );
}
