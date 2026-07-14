"use client";

/**
 * EpicShell — Client chrome scoped to an epic.
 *
 * fix 1: flex h-full flex-col occupies full viewport height.
 *   - EpicScopeHeader + banner + EpicTabBar = shrink-0 fixed area
 *   - content area = flex-1 min-h-0 overflow-hidden
 *   - Eliminates CSS variable dependencies on --epic-header-h / --epic-chrome-h.
 *
 * - useRunActivity is called only once under the epic (double SSE subscription is prohibited).
 * - EpicRunContext.Provider wraps all children and supplies { activityState, setPausePending, project, epic, projectId, epicId }.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { StopConfirmDialog } from "@/components/features/epics/run-controls";
import { Icon } from "@/components/icon";
import type { Epic, Project, RunState, ThreadEntry } from "@/lib/api/endpoints";
import { getEpic, runAction } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { useIsDesktop } from "@/lib/hooks/use-is-desktop";
import { useT } from "@/lib/i18n/provider";
import { useRunActivity } from "@/lib/sse/use-run-activity";
import { EpicRunProvider } from "./epic-run-context";
import { EpicScopeHeader } from "./epic-scope-header";
import { EpicSidebar } from "./epic-sidebar";
import { EpicTabBar } from "./epic-tab-bar";

interface EpicShellProps {
  projectId: string;
  epicId: string;
  project: Project | null;
  epic: Epic | null;
  initialRunState: RunState | null;
  initialThreads: ThreadEntry[];
  children: React.ReactNode;
}

export function EpicShell({
  projectId,
  epicId,
  project,
  epic,
  initialRunState,
  initialThreads,
  children,
}: EpicShellProps) {
  const t = useT();
  const isDesktop = useIsDesktop();
  const [showStopConfirm, setShowStopConfirm] = useState(false);
  // Mobile only: collapse header + tab bar while scrolling down a conversation
  // (driven by ThreadChatInner via context; the classes only apply below md).
  const [mobileChromeHidden, setMobileChromeHidden] = useState(false);
  const qc = useQueryClient();

  // Subscribe to epic.active_thread_id live.
  // On in-app navigation (router.push after a new trial is created) the layout RSC may remain stale,
  // but by invalidating queryKeys.epics.detail (in NewThreadModal's onSuccess),
  // useQuery re-fetches so the client always has the latest active_thread_id.
  // staleTime=30_000. After invalidate (NewThreadModal's onSuccess) a fetch runs immediately. SSR initialData is supplied from props.
  const { data: liveEpic } = useQuery({
    queryKey: queryKeys.epics.detail(projectId, epicId),
    queryFn: () => getEpic(projectId, epicId),
    initialData: epic ?? undefined,
    staleTime: 30_000,
    enabled: !!epicId,
  });

  // Use liveEpic.active_thread_id with highest priority.
  // liveEpic always holds the latest value via the useQuery above, so it takes precedence over the stale epic prop from RSC.
  const liveActiveThreadId = liveEpic?.active_thread_id ?? epic?.active_thread_id ?? null;

  // The single SSE subscription point
  const {
    state: activityState,
    setPausePending,
    clearLiveBuffer,
  } = useRunActivity({
    projectId,
    epicId,
    initialRunState: initialRunState ?? undefined,
    initialThreads,
    // activeTrialId resolution order (attribution split — composer rights only):
    //   1. epic.active_thread_id (liveActiveThreadId) — live-fetched via useQuery, highest priority
    //   2. First thread in initialThreads where role=manager && status!=="archived" (RSC prop, not live cache)
    //   3. "manager" (legacy compatibility fallback, applied by consumers)
    // RunState.thread_id is the RUN's own thread and feeds currentRun
    // (your-turn banner attribution), never the composer.
    // The only update path for composer display: liveActiveThreadId → activeThreadId → SET_ACTIVE_TRIAL_ID.
    // The archived exclusion in INIT / applyTreeInit is a fix for tree display nodes and is separate from the composer.
    activeThreadId: liveActiveThreadId,
  });

  const stopMutation = useMutation({
    mutationFn: () => runAction(projectId, epicId, "stop"),
    onSuccess: () => {
      setShowStopConfirm(false);
      qc.invalidateQueries({ queryKey: queryKeys.epics.list(projectId) });
      qc.invalidateQueries({ queryKey: queryKeys.runState.get(projectId, epicId) });
    },
  });

  const runFailed = activityState.runStatus === "error";
  const runError = activityState.runError;

  const contextValue = useMemo(
    () => ({
      projectId,
      epicId,
      project,
      // Prefer the live-fetched epic: mutations (complete / reopen via
      // useCompleteEpic / useReopenEpic) invalidate epics.detail, and children
      // like RunControlsBar branch on epic.status — the stale RSC prop would
      // leave the controls on the wrong branch until a full reload.
      epic: liveEpic ?? epic,
      activityState,
      setPausePending,
      clearLiveBuffer,
      setMobileChromeHidden,
    }),
    [projectId, epicId, project, epic, liveEpic, activityState, setPausePending, clearLiveBuffer],
  );

  // Run failure / preparing banners — shown over the content pane on both
  // layouts (the sidebar / mobile header also carry a compact status).
  const banners = (
    <>
      {runFailed && (
        <div
          className="shrink-0 flex items-start gap-3 px-6 py-3"
          style={{
            borderBottom: "1px solid color-mix(in oklab, var(--color-error) 20%, transparent)",
          }}
        >
          <Icon name="warning" className="mt-0.5 shrink-0 text-[16px] text-error" />
          <div>
            <p
              className="font-mono text-[12px] font-medium"
              style={{ color: "var(--color-error)" }}
            >
              {t("epicShell.runFailed")}
            </p>
            {runError && (
              <p className="mt-0.5 font-mono text-[11px] text-on-surface-variant">{runError}</p>
            )}
          </div>
        </div>
      )}
      {!runFailed && activityState.runStatus === "preparing" && (
        <div
          className="shrink-0 flex items-center gap-2 px-6 py-2"
          style={{
            borderBottom:
              "1px solid color-mix(in oklab, var(--color-on-surface-variant) 15%, transparent)",
          }}
        >
          <Icon name="sync" className="shrink-0 text-[14px] text-on-surface-variant animate-spin" />
          <p className="font-mono text-[11px] text-on-surface-variant">
            {t("epic.status.preparing")}
          </p>
        </div>
      )}
    </>
  );

  return (
    <EpicRunProvider value={contextValue}>
      {/*
       * Desktop (≥ md): a persistent 320px EpicSidebar beside the 56px global
       * rail, with a clean full-height content pane on the right (no header /
       * tab bands). Mobile keeps the stacked, scroll-collapsing bands.
       * useIsDesktop gates which one MOUNTS so no testid is ever duplicated.
       */}
      {isDesktop ? (
        <div className="flex h-full overflow-hidden">
          <EpicSidebar
            initialThreads={initialThreads}
            onStopRequest={() => setShowStopConfirm(true)}
          />
          <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
            {banners}
            <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
          </div>
        </div>
      ) : (
        <div className="flex h-full flex-col overflow-hidden">
          {/* nameplate — collapses while scrolling down the conversation. */}
          <div
            className={cn(
              "shrink-0 overflow-hidden transition-[max-height,opacity] duration-200",
              mobileChromeHidden ? "max-h-0 opacity-0" : "max-h-32",
            )}
          >
            <EpicScopeHeader onStopRequest={() => setShowStopConfirm(true)} />
          </div>

          {banners}

          {/* Your-turn state is NOT a banner: the passive indicator is the header
              StatusBadge, the active one is the lit composer on the parked
              thread (ThreadChatInner). One voice per state. */}

          {/* tab bar — collapses with the header on mobile */}
          <div
            className={cn(
              "shrink-0 overflow-hidden transition-[max-height,opacity] duration-200",
              mobileChromeHidden ? "max-h-0 opacity-0" : "max-h-16",
            )}
          >
            <EpicTabBar />
          </div>

          <div aria-hidden className="shrink-0 h-4" />

          <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
        </div>
      )}

      {/* Stop confirmation dialog */}
      <StopConfirmDialog
        open={showStopConfirm}
        onConfirm={() => stopMutation.mutate()}
        onCancel={() => setShowStopConfirm(false)}
        isPending={stopMutation.isPending}
      />
    </EpicRunProvider>
  );
}
