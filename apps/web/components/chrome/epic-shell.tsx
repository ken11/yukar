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
import { useT } from "@/lib/i18n/provider";
import { useRunActivity } from "@/lib/sse/use-run-activity";
import { EpicRunProvider } from "./epic-run-context";
import { EpicScopeHeader } from "./epic-scope-header";
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
    // activeTrialId resolution order (P4 split — composer rights only):
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

  return (
    <EpicRunProvider value={contextValue}>
      {/*
       * fix 1: flex h-full flex-col occupies the full viewport height.
       * To delegate the parent (ProjectLayout) overflow-y-auto scroll inward
       * on the epic route, this container itself is overflow-hidden.
       * Each content pane (ThreadPageClient, etc.) manages its own scroll.
       */}
      <div className="flex h-full flex-col overflow-hidden md:h-full">
        {/* nameplate (sticky) — shrink-0.
            Mobile: collapses while scrolling down the conversation (max-h transition);
            desktop is pinned open via md:max-h-none. */}
        <div
          className={cn(
            "shrink-0 overflow-hidden transition-[max-height,opacity] duration-200 md:max-h-none md:opacity-100",
            mobileChromeHidden ? "max-md:max-h-0 max-md:opacity-0" : "max-md:max-h-32",
          )}
        >
          <EpicScopeHeader onStopRequest={() => setShowStopConfirm(true)} />
        </div>

        {/* banner: shown between header and tabbar (visible on all tabs) */}
        {/* error: warm ▲ + concise cause (datum language) */}
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

        {/* preparing: index refresh in progress before Manager starts */}
        {!runFailed && activityState.runStatus === "preparing" && (
          <div
            className="shrink-0 flex items-center gap-2 px-6 py-2"
            style={{
              borderBottom:
                "1px solid color-mix(in oklab, var(--color-on-surface-variant) 15%, transparent)",
            }}
          >
            <Icon
              name="sync"
              className="shrink-0 text-[14px] text-on-surface-variant animate-spin"
            />
            <p className="font-mono text-[11px] text-on-surface-variant">
              {t("epic.status.preparing")}
            </p>
          </div>
        )}

        {/* your turn: cyan dot + concise text (datum language). Shown only when a
            run actually parked (yourTurn marker) — a never-run epic is
            "waiting" too but carries no marker, so no banner. Role-aware (P4):
            currentRun.role says WHICH agent is waiting (Reviewer report vs the
            neutral manager wording). */}
        {!runFailed && activityState.yourTurn != null && (
          <div
            className="shrink-0 flex items-center gap-2 px-6 py-2"
            style={{
              borderBottom: "1px solid color-mix(in oklab, var(--color-light) 20%, transparent)",
            }}
          >
            <span
              className="h-1.5 w-1.5 shrink-0 rounded-full"
              style={{ backgroundColor: "var(--color-light)" }}
              aria-hidden
            />
            <p className="font-mono text-[11px]" style={{ color: "var(--color-light)" }}>
              {/* Role wording only when currentRun matches the parked marker —
                  a late role-refresh response describing an older (reviewer)
                  run must not label a newer manager park. */}
              {activityState.currentRun?.role === "reviewer" &&
              activityState.currentRun.threadId === activityState.yourTurn?.threadId
                ? t("epicShell.awaitingInputReviewer")
                : t("epicShell.awaitingInput")}
            </p>
          </div>
        )}

        {/* tab bar (shrink-0, no top dependency needed) — collapses with the header on mobile */}
        <div
          className={cn(
            "shrink-0 overflow-hidden transition-[max-height,opacity] duration-200 md:max-h-none md:opacity-100",
            mobileChromeHidden ? "max-md:max-h-0 max-md:opacity-0" : "max-md:max-h-16",
          )}
        >
          <EpicTabBar />
        </div>

        {/* void — design-language §spacing/grid (reduced on mobile) */}
        <div aria-hidden className="shrink-0 h-4 md:h-[var(--spacing-void,40px)]" />

        {/*
         * content area — flex-1 min-h-0 overflow-y-auto
         * Plain pages such as tasks/docs use natural scroll.
         * h-full flex pages such as thread/diff manage their own scroll (h-full = scroll container height).
         */}
        <div className="flex-1 min-h-0 overflow-y-auto">{children}</div>
      </div>

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
