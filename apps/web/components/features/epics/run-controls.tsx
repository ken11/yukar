"use client";

/**
 * RunControls — group of UI components for epic run control.
 *
 * Epic side is a 1-bit user-owned status (open ⇄ completed); the run side keeps
 * its own state branches (preparing/running/paused/awaiting_input/…).
 * Imported by EpicScopeHeader and EpicShell.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Icon } from "@/components/icon";
import { runAction, startReview, startRun } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
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
 * Run-control button group. Used in EpicScopeHeader.
 *
 * The epic side is a single user-owned bit:
 *   - open      → all run operations + Complete + Ask Reviewer + Request Fix
 *   - completed → read-only: Reopen + Ask Reviewer (reviewer is read-only,
 *                 so inspecting finished work never requires reopening)
 * Run-state branches (preparing/running/paused/awaiting_input/…) are unchanged.
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
}: {
  projectId: string;
  epicId: string;
  /** Current persisted Epic.status (from server) — "open" | "completed" */
  epicStatus?: string;
  activityState: ReturnType<typeof useRunActivity>["state"];
  setPausePending: (v: boolean) => void;
  onStopRequest: () => void;
}) {
  const t = useT();
  const router = useRouter();
  const qc = useQueryClient();

  const [completeError, setCompleteError] = useState<string | null>(null);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const completeMutation = useCompleteEpic(projectId);
  const reopenMutation = useReopenEpic(projectId);

  const isEpicCompleted = epicStatus === "completed";

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
        className="flex items-center gap-1.5 rounded border border-outline-variant px-3 py-1.5 text-body-sm text-on-surface-variant transition-colors hover:bg-surface-container hover:text-on-surface disabled:cursor-not-allowed disabled:opacity-50"
        title={t("epic.completeTitle")}
      >
        <Icon name="check_circle" className="text-[16px]" />
        <span className="hidden sm:inline">
          {completeMutation.isPending ? "…" : t("epic.complete")}
        </span>
      </button>
    </>
  );

  // Navigation target for the active manager thread; use managerThreadId when it is known.
  const managerThreadId = activityState.managerThreadId ?? "manager";

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
  const isAwaitingInput = runStatus === "awaiting_input";
  const isCompleted = runStatus === "completed";
  const isInterrupted = runStatus === "interrupted";
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
      className="flex items-center gap-1.5 rounded border border-outline-variant px-3 py-1.5 text-body-sm text-on-surface-variant transition-colors hover:bg-surface-container hover:text-on-surface disabled:cursor-not-allowed disabled:opacity-50"
      title={t("epic.reviewCheckTitle")}
    >
      <Icon name="fact_check" className="text-[16px]" />
      <span className="hidden sm:inline">
        {reviewMutation.isPending ? "…" : t("epic.reviewCheck")}
      </span>
    </button>
  );

  /** Request Fix — jump to the active manager conversation. */
  const renderRequestFixButton = () => (
    <button
      type="button"
      onClick={() =>
        router.push(`/projects/${projectId}/epics/${epicId}/threads/${managerThreadId}`)
      }
      className="flex items-center gap-1.5 rounded border border-outline-variant px-3 py-1.5 text-body-sm text-on-surface-variant transition-colors hover:bg-surface-container hover:text-on-surface"
      title={t("common.requestFix")}
    >
      <Icon name="forum" className="text-[16px]" />
      <span className="hidden sm:inline">{t("common.requestFix")}</span>
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
          className="flex items-center gap-1.5 rounded border border-outline-variant px-3 py-1.5 text-body-sm text-on-surface-variant transition-colors hover:bg-surface-container hover:text-on-surface disabled:cursor-not-allowed disabled:opacity-50"
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
        className="flex items-center gap-1.5 rounded border border-error/40 px-3 py-1.5 text-body-sm text-error transition-colors hover:bg-error/10 disabled:opacity-50"
        title={t("run.stopWarning")}
        aria-label={t("common.stop")}
      >
        <Icon name="stop" className="text-[16px]" />
        <span className="hidden sm:inline">{t("common.stop")}</span>
      </button>
    );
  }

  if (isAwaitingInput) {
    return (
      <button
        type="button"
        data-testid="stop-run-btn"
        onClick={onStopRequest}
        disabled={actionMutation.isPending}
        className="flex items-center gap-1.5 rounded border border-error/40 px-3 py-1.5 text-body-sm text-error transition-colors hover:bg-error/10 disabled:opacity-50"
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
          className="flex items-center gap-1.5 rounded border border-outline-variant px-3 py-1.5 text-body-sm text-on-surface-variant transition-colors hover:bg-surface-variant hover:text-on-surface disabled:opacity-50"
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
          className="flex items-center gap-1.5 rounded border border-error/40 px-3 py-1.5 text-body-sm text-error transition-colors hover:bg-error/10 disabled:opacity-50"
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
          className="flex items-center gap-1.5 rounded bg-primary px-3 py-1.5 text-body-sm font-medium text-on-primary transition-colors hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
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
          className="flex items-center gap-1.5 rounded border border-error/40 px-3 py-1.5 text-body-sm text-error transition-colors hover:bg-error/10 disabled:opacity-50"
          title={t("run.stopWarning")}
          aria-label={t("common.stop")}
        >
          <Icon name="stop" className="text-[16px]" />
          <span className="hidden sm:inline">{t("common.stop")}</span>
        </button>
      </>
    );
  }

  if (isInterrupted) {
    return (
      <>
        {reviewError && <span className="text-[11px] text-error">{reviewError}</span>}
        <button
          type="button"
          onClick={() => runMutation.mutate()}
          disabled={runMutation.isPending}
          className="flex items-center gap-1.5 rounded bg-primary px-3 py-1.5 text-body-sm font-medium text-on-primary transition-colors hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
          title={runMutation.isPending ? t("common.resuming") : t("common.resumeFromInterrupt")}
        >
          <Icon name="play_arrow" className="text-[16px]" />
          <span className="hidden sm:inline">
            {runMutation.isPending ? t("common.resuming") : t("common.resumeFromInterrupt")}
          </span>
        </button>
        {renderReviewerButton()}
        {renderRequestFixButton()}
        {renderCompleteButton()}
      </>
    );
  }

  // Run finished (runStatus="completed" from state.yaml): the epic itself stays
  // open — finishing a run never transitions the epic. Offer Rerun, and the
  // shared open-epic actions (Reviewer / Request Fix / Complete).
  if (isCompleted) {
    return (
      <>
        {reviewError && <span className="text-[11px] text-error">{reviewError}</span>}
        <button
          type="button"
          data-testid="rerun-btn"
          onClick={() => runMutation.mutate()}
          disabled={runMutation.isPending}
          className="flex items-center gap-1.5 rounded border border-outline-variant px-3 py-1.5 text-body-sm text-on-surface-variant transition-colors hover:bg-surface-container hover:text-on-surface disabled:cursor-not-allowed disabled:opacity-50"
          title={runMutation.isPending ? t("common.starting") : t("common.rerun")}
        >
          <Icon name="restart_alt" className="text-[16px]" />
          <span className="hidden sm:inline">
            {runMutation.isPending ? t("common.starting") : t("common.rerun")}
          </span>
        </button>
        {renderReviewerButton()}
        {renderRequestFixButton()}
        {renderCompleteButton()}
      </>
    );
  }

  // idle / default (open epic, no active run)
  const canStart = !isPreparing && !isRunning && !isPaused && !isAwaitingInput;
  return (
    <>
      {reviewError && <span className="text-[11px] text-error">{reviewError}</span>}
      <button
        type="button"
        data-testid="start-run-btn"
        onClick={() => runMutation.mutate()}
        disabled={runMutation.isPending || !canStart}
        className="flex items-center gap-1.5 rounded bg-primary px-3 py-1.5 text-body-sm font-medium text-on-primary transition-colors hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        title={runMutation.isPending ? t("common.starting") : t("run.startRun")}
      >
        <Icon name="rocket_launch" className="text-[16px]" />
        <span className="hidden sm:inline">
          {runMutation.isPending ? t("common.starting") : t("run.startRun")}
        </span>
      </button>
      {renderReviewerButton()}
      {renderRequestFixButton()}
      {renderCompleteButton()}
    </>
  );
}
