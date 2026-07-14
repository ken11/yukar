"use client";

/**
 * RunControls — group of UI components for epic run control.
 *
 * Epic side is a 1-bit user-owned status (open ⇄ completed); the run side is
 * the collapsed run-status vocabulary (preparing / running / paused / waiting / error, plus
 * completed for JOB runs only). "waiting" = your turn — the normal resting
 * state, handled by the default open-epic branch.
 * Imported by EpicSidebar (desktop) and EpicScopeHeader (mobile).
 *
 * All actions are laid out flat — the primary run action plus the secondary
 * actions (Ask Reviewer / Complete) sit side by side; there is no ⋯ submenu.
 * `layout="stack"` makes every button a full-width row for the vertical sidebar;
 * the default `"inline"` keeps them as a wrapping chip row (mobile header).
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Icon } from "@/components/icon";
import { runAction, startReview, startRun } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { useT } from "@/lib/i18n/provider";
import type { useRunActivity } from "@/lib/sse/use-run-activity";
import { extractCompleteError, useCompleteEpic, useReopenEpic } from "./use-close-epic";

// ---------------------------------------------------------------------------
// StopConfirmDialog
// ---------------------------------------------------------------------------

export function StopConfirmDialog({
  open,
  onConfirm,
  onCancel,
  isPending,
}: {
  open: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  isPending: boolean;
}) {
  const t = useT();
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="w-full max-w-md rounded-lg border border-outline-variant bg-surface-container p-6 shadow-lg">
        <div className="mb-4 flex items-center gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-error/30 bg-error/10">
            <Icon name="warning" className="text-[20px] text-error" />
          </div>
          <div>
            <h3 className="text-body-md font-semibold text-on-surface">
              {t("run.stopConfirmTitle")}
            </h3>
            <p className="text-[12px] text-on-surface-variant">{t("run.stopConfirmMessage")}</p>
          </div>
        </div>
        <div className="flex justify-end gap-3">
          <button
            type="button"
            onClick={onCancel}
            disabled={isPending}
            className="rounded border border-outline-variant px-4 py-2 text-body-sm text-on-surface-variant transition-colors hover:bg-surface-container-high disabled:opacity-50"
          >
            {t("common.cancel")}
          </button>
          <button
            type="button"
            data-testid="stop-confirm-btn"
            onClick={onConfirm}
            disabled={isPending}
            className="flex items-center gap-1.5 rounded border border-error/40 bg-error/10 px-4 py-2 text-body-sm font-medium text-error transition-colors hover:bg-error/20 disabled:opacity-50"
          >
            <Icon name="stop" className="text-[16px]" />
            {isPending ? t("common.stopping") : t("common.stopAction")}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// RunControlsBar
// ---------------------------------------------------------------------------

/**
 * Run-control button group. Used in EpicSidebar (desktop) and EpicScopeHeader (mobile).
 *
 * The epic side is a single user-owned bit:
 *   - open      → all run operations + Complete + Ask Reviewer
 *   - completed → read-only: Reopen + Ask Reviewer (reviewer is read-only,
 *                 so inspecting finished work never requires reopening)
 * Run-state branches: preparing/running/paused show execution controls;
 * waiting (your turn — the resting state) falls into the default open branch.
 *
 * Pass activityState directly as the return value of useRunActivity().
 */
export function RunControlsBar({
  projectId,
  epicId,
  epicStatus,
  activityState,
  setPausePending,
  onStopRequest,
  layout = "inline",
}: {
  projectId: string;
  epicId: string;
  /** Current persisted Epic.status (from server) — "open" | "completed" */
  epicStatus?: string;
  activityState: ReturnType<typeof useRunActivity>["state"];
  setPausePending: (v: boolean) => void;
  onStopRequest: () => void;
  /** "stack" = full-width rows (vertical sidebar); "inline" = wrapping chips (mobile). */
  layout?: "inline" | "stack";
}) {
  const t = useT();
  const router = useRouter();
  const qc = useQueryClient();

  const [completeError, setCompleteError] = useState<string | null>(null);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const completeMutation = useCompleteEpic(projectId);
  const reopenMutation = useReopenEpic(projectId);

  const isEpicCompleted = epicStatus === "completed";

  // In "stack" layout every action is a full-width, left-aligned row.
  const stackCls = layout === "stack" ? "w-full justify-start" : "";

  // Secondary actions (Ask Reviewer / Complete) — quiet. In the vertical sidebar
  // they are borderless ghost rows so only the primary run button is filled;
  // in the mobile chip row they keep a hairline outline.
  const secondaryCls = cn(
    "flex items-center gap-1.5 rounded px-3 py-1.5 text-body-sm text-on-surface-variant transition-colors disabled:cursor-not-allowed disabled:opacity-50",
    layout === "stack"
      ? "w-full justify-start hover:bg-surface-container-high hover:text-on-surface"
      : "border border-outline-variant hover:bg-surface-container hover:text-on-surface",
  );

  /**
   * Complete button — the user's single "finish" action (approve done work or
   * abandon unfinished work). Shared across every open-epic branch that is not
   * actively running. Implemented as a render helper called via
   * `{renderCompleteButton()}` rather than a component (`<CompleteButton/>`) to
   * avoid remounting the subtree on every parent re-render (nested component
   * definitions change type on each render).
   */
  const renderCompleteButton = () => (
    <>
      {completeError && <span className="text-[11px] text-error">{completeError}</span>}
      <button
        type="button"
        data-testid="complete-epic-btn"
        onClick={() => {
          setCompleteError(null);
          completeMutation.mutate(epicId, {
            onError: (err) => {
              setCompleteError(extractCompleteError(err, t("epic.completeRunActive")));
            },
          });
        }}
        disabled={completeMutation.isPending}
        className={secondaryCls}
        title={t("epic.completeTitle")}
      >
        <Icon name="check_circle" className="text-[16px]" />
        <span className="hidden sm:inline">
          {completeMutation.isPending ? "…" : t("epic.complete")}
        </span>
      </button>
    </>
  );

  // Navigation target for the active manager trial; use activeTrialId when it is known.
  const managerThreadId = activityState.activeTrialId ?? "manager";

  const runMutation = useMutation({
    mutationFn: () => startRun(projectId, epicId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.epics.list(projectId) });
      qc.invalidateQueries({ queryKey: queryKeys.runState.get(projectId, epicId) });
      if (epicId) {
        router.push(`/projects/${projectId}/epics/${epicId}/threads/${managerThreadId}`);
      }
    },
  });

  const actionMutation = useMutation({
    mutationFn: (action: "pause" | "resume" | "stop") => runAction(projectId, epicId, action),
    onSuccess: (_data, action) => {
      qc.invalidateQueries({ queryKey: queryKeys.epics.list(projectId) });
      qc.invalidateQueries({ queryKey: queryKeys.runState.get(projectId, epicId) });
      if (action === "stop") {
        // caller (parent) closes dialog
      }
    },
  });

  // Start a read-only Reviewer run and jump to its conversation.
  const reviewMutation = useMutation({
    mutationFn: () => startReview(projectId, epicId),
    onSuccess: (thread) => {
      qc.invalidateQueries({ queryKey: queryKeys.threads.list(projectId, epicId) });
      qc.invalidateQueries({ queryKey: queryKeys.runState.get(projectId, epicId) });
      router.push(`/projects/${projectId}/epics/${epicId}/threads/${thread.id}`);
    },
    onError: (err) => {
      setReviewError(err instanceof Error ? err.message : t("epic.reviewFailed"));
    },
  });

  const runStatus = activityState.runStatus;
  const isPreparing = runStatus === "preparing";
  const isRunning = runStatus === "running";
  const isPaused = runStatus === "paused";
  // JOB runs only (resolve / arbiter) — a conversation run never completes.
  const isCompleted = runStatus === "completed";
  const pausePending = activityState.pausePending;

  const pauseLabel = pausePending ? t("common.pausing") : t("common.pause");

  const handlePause = () => {
    setPausePending(true);
    actionMutation.mutate("pause");
  };

  /** Ask Reviewer — available on every open epic branch without an active run,
   *  and on completed epics (the reviewer is read-only). */
  const renderReviewerButton = () => (
    <button
      type="button"
      data-testid="start-review-btn"
      onClick={() => {
        setReviewError(null);
        reviewMutation.mutate();
      }}
      disabled={reviewMutation.isPending}
      className={secondaryCls}
      title={t("epic.reviewCheckTitle")}
    >
      <Icon name="fact_check" className="text-[16px]" />
      <span className="hidden sm:inline">
        {reviewMutation.isPending ? "…" : t("epic.reviewCheck")}
      </span>
    </button>
  );

  // Completed epic (user-owned bit): read-only — Reopen + Ask Reviewer.
  // Run operations are rejected by the backend (409) until the user reopens.
  if (isEpicCompleted) {
    return (
      <>
        {reviewError && <span className="text-[11px] text-error">{reviewError}</span>}
        <button
          type="button"
          data-testid="reopen-btn"
          onClick={() => reopenMutation.mutate(epicId)}
          disabled={reopenMutation.isPending}
          className={cn(
            "flex items-center gap-1.5 rounded border border-outline-variant px-3 py-1.5 text-body-sm text-on-surface-variant transition-colors hover:bg-surface-container hover:text-on-surface disabled:cursor-not-allowed disabled:opacity-50",
            stackCls,
          )}
          title={t("epic.reopen")}
        >
          <Icon name="lock_open" className="text-[16px]" />
          <span className="hidden sm:inline">{t("epic.reopen")}</span>
        </button>
        {renderReviewerButton()}
      </>
    );
  }

  if (isPreparing) {
    return (
      <button
        type="button"
        data-testid="stop-run-btn"
        onClick={onStopRequest}
        disabled={actionMutation.isPending}
        className={cn(
          "flex items-center gap-1.5 rounded border border-error/40 px-3 py-1.5 text-body-sm text-error transition-colors hover:bg-error/10 disabled:opacity-50",
          stackCls,
        )}
        title={t("run.stopWarning")}
        aria-label={t("common.stop")}
      >
        <Icon name="stop" className="text-[16px]" />
        <span className="hidden sm:inline">{t("common.stop")}</span>
      </button>
    );
  }

  if (isRunning) {
    return (
      <>
        <button
          type="button"
          data-testid="pause-run-btn"
          onClick={handlePause}
          disabled={actionMutation.isPending || pausePending}
          className={cn(
            "flex items-center gap-1.5 rounded border border-outline-variant px-3 py-1.5 text-body-sm text-on-surface-variant transition-colors hover:bg-surface-variant hover:text-on-surface disabled:opacity-50",
            stackCls,
          )}
          title={pauseLabel}
        >
          <Icon name="pause" className="text-[16px]" />
          <span className="hidden sm:inline">{pauseLabel}</span>
        </button>
        <button
          type="button"
          data-testid="stop-run-btn"
          onClick={onStopRequest}
          disabled={actionMutation.isPending}
          className={cn(
            "flex items-center gap-1.5 rounded border border-error/40 px-3 py-1.5 text-body-sm text-error transition-colors hover:bg-error/10 disabled:opacity-50",
            stackCls,
          )}
          title={t("run.stopWarning")}
          aria-label={t("common.stop")}
        >
          <Icon name="stop" className="text-[16px]" />
          <span className="hidden sm:inline">{t("common.stop")}</span>
        </button>
      </>
    );
  }

  if (isPaused) {
    return (
      <>
        <button
          type="button"
          data-testid="resume-run-btn"
          onClick={() => actionMutation.mutate("resume")}
          disabled={actionMutation.isPending || pausePending}
          className={cn(
            "flex items-center gap-1.5 rounded bg-primary px-3 py-1.5 text-body-sm font-medium text-on-primary transition-colors hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50",
            stackCls,
          )}
          title={actionMutation.isPending ? t("common.resuming") : t("common.resume")}
        >
          <Icon name="play_arrow" className="text-[16px]" />
          <span className="hidden sm:inline">
            {actionMutation.isPending ? t("common.resuming") : t("common.resume")}
          </span>
        </button>
        <button
          type="button"
          data-testid="stop-run-btn"
          onClick={onStopRequest}
          disabled={actionMutation.isPending}
          className={cn(
            "flex items-center gap-1.5 rounded border border-error/40 px-3 py-1.5 text-body-sm text-error transition-colors hover:bg-error/10 disabled:opacity-50",
            stackCls,
          )}
          title={t("run.stopWarning")}
          aria-label={t("common.stop")}
        >
          <Icon name="stop" className="text-[16px]" />
          <span className="hidden sm:inline">{t("common.stop")}</span>
        </button>
      </>
    );
  }

  // JOB run finished (runStatus="completed" — resolve / arbiter only; a
  // conversation run never completes). The epic itself stays open — finishing
  // a job never transitions the epic. Offer Rerun, and the shared open-epic
  // actions (Reviewer / Complete).
  if (isCompleted) {
    return (
      <>
        {reviewError && <span className="text-[11px] text-error">{reviewError}</span>}
        <button
          type="button"
          data-testid="rerun-btn"
          onClick={() => runMutation.mutate()}
          disabled={runMutation.isPending}
          className={cn(
            "flex items-center gap-1.5 rounded border border-outline-variant px-3 py-1.5 text-body-sm text-on-surface-variant transition-colors hover:bg-surface-container hover:text-on-surface disabled:cursor-not-allowed disabled:opacity-50",
            stackCls,
          )}
          title={runMutation.isPending ? t("common.starting") : t("common.rerun")}
        >
          <Icon name="restart_alt" className="text-[16px]" />
          <span className="hidden sm:inline">
            {runMutation.isPending ? t("common.starting") : t("common.rerun")}
          </span>
        </button>
        {renderReviewerButton()}
        {renderCompleteButton()}
      </>
    );
  }

  // waiting / default (open epic, no executing turn — it is the user's turn).
  // A parked conversation does not hold the run slot, so all open-epic actions
  // are available; Start Run continues the conversation.
  const canStart = !isPreparing && !isRunning && !isPaused;
  return (
    <>
      {reviewError && <span className="text-[11px] text-error">{reviewError}</span>}
      <button
        type="button"
        data-testid="start-run-btn"
        onClick={() => runMutation.mutate()}
        disabled={runMutation.isPending || !canStart}
        className={cn(
          "flex items-center gap-1.5 rounded bg-primary px-3 py-1.5 text-body-sm font-medium text-on-primary transition-colors hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50",
          stackCls,
        )}
        title={runMutation.isPending ? t("common.starting") : t("run.startRun")}
      >
        <Icon name="rocket_launch" className="text-[16px]" />
        <span className="hidden sm:inline">
          {runMutation.isPending ? t("common.starting") : t("run.startRun")}
        </span>
      </button>
      {renderReviewerButton()}
      {renderCompleteButton()}
    </>
  );
}
