"use client";

/**
 * MergeProgressPanel — live progress panel for arbiter batch merge (Feature 2).
 *
 * Subscribes to the project SSE stream via useMergeProgress.
 * Shows:
 *   - progress bar (completed/total)
 *   - current epic + phase
 *   - per-epic result list with links to diff tab for non-merged results
 *   - Stop button → stopMerge
 *   - Finished summary when phase === "finished"
 */

import { useMutation } from "@tanstack/react-query";
import Link from "next/link";
import { Icon } from "@/components/icon";
import type { EpicMergeResult } from "@/lib/api/endpoints";
import { stopMerge } from "@/lib/api/endpoints";
import { cn } from "@/lib/cn";
import { useT } from "@/lib/i18n/provider";
import { useMergeProgress } from "@/lib/sse/use-merge-progress";

interface MergeProgressPanelProps {
  projectId: string;
  onInvalidate: () => void;
  onDismiss: () => void;
}

const RESULT_ICONS: Record<EpicMergeResult["status"], string> = {
  merged: "check",
  conflict_unresolved: "warning",
  vetting_refused: "block",
  skipped: "skip_next",
  error: "error",
};

const RESULT_COLOR: Record<EpicMergeResult["status"], string> = {
  merged: "text-on-surface",
  conflict_unresolved: "text-error",
  vetting_refused: "text-on-surface-variant",
  skipped: "text-outline",
  error: "text-error",
};

export function MergeProgressPanel({
  projectId,
  onInvalidate,
  onDismiss,
}: MergeProgressPanelProps) {
  const t = useT();
  const { progress } = useMergeProgress(projectId, onInvalidate);

  const stopMutation = useMutation({
    mutationFn: () => stopMerge(projectId),
  });

  // Resolve phase label
  function phaseLabel(phase: string): string {
    const key = `merge.phase.${phase}` as Parameters<typeof t>[0];
    const resolved = t(key);
    // If key not found (returns the key itself), fall back to phase string
    return resolved === key ? phase : resolved;
  }

  function resultStatusLabel(status: EpicMergeResult["status"]): string {
    return t(`merge.resultStatus.${status}`);
  }

  // Progress fraction for bar
  const fraction = progress && progress.total > 0 ? progress.completed / progress.total : 0;

  return (
    <div
      data-testid="merge-progress-panel"
      className="mb-6 rounded border border-outline-variant bg-surface-container"
      style={{ borderLeft: "2px solid var(--color-light)" }}
    >
      {/* Header */}
      <div
        className="flex items-center justify-between gap-3 px-4 py-3"
        style={{ borderBottom: "1px solid var(--edge-shadow)" }}
      >
        <div className="flex items-center gap-2">
          <Icon
            name={progress?.isFinished ? "check_circle" : "merge"}
            className={cn(
              "text-[16px]",
              progress?.isFinished ? "text-on-surface" : "text-[var(--color-light)]",
            )}
          />
          <span className="font-mono text-[12px] font-medium uppercase tracking-[0.04em] text-on-surface">
            {t("merge.panelTitle")}
          </span>
          {progress && (
            <span className="font-mono text-[11px] text-on-surface-variant">
              {t("merge.progress")
                .replace("{completed}", String(progress.completed))
                .replace("{total}", String(progress.total))}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {progress && !progress.isFinished && (
            <button
              type="button"
              onClick={() => stopMutation.mutate()}
              disabled={stopMutation.isPending}
              className="flex items-center gap-1 rounded border border-error/40 px-2 py-1 font-mono text-[11px] text-error transition-colors hover:bg-error/10 disabled:opacity-50"
            >
              <Icon name="stop" className="text-[13px]" />
              {stopMutation.isPending ? t("merge.stopping") : t("merge.stop")}
            </button>
          )}
          <button
            type="button"
            onClick={onDismiss}
            className="rounded p-1 text-outline transition-colors hover:text-on-surface"
            aria-label="Dismiss"
          >
            <Icon name="close" className="text-[16px]" />
          </button>
        </div>
      </div>

      {/* Progress bar */}
      {progress && (
        <div className="h-0.5 bg-surface-container-highest">
          <div
            className="h-full bg-[var(--color-light)] transition-all duration-500"
            style={{ width: `${Math.round(fraction * 100)}%` }}
          />
        </div>
      )}

      {/* Body */}
      <div className="px-4 py-3">
        {!progress && (
          <p className="font-mono text-[12px] text-outline">
            <Icon name="sync" className="mr-1 inline animate-spin text-[13px]" />
            {t("merge.phase.started")}…
          </p>
        )}

        {progress && !progress.isFinished && (
          <div className="mb-3 flex items-center gap-2">
            <Icon name="sync" className="animate-spin text-[14px] text-[var(--color-light)]" />
            <span className="font-mono text-[12px] text-on-surface-variant">
              {phaseLabel(progress.phase)}
              {progress.currentEpicId && (
                <>
                  {" — "}
                  <span className="text-on-surface">{progress.currentEpicId}</span>
                </>
              )}
            </span>
          </div>
        )}

        {progress?.isFinished && (
          <p className="mb-3 font-mono text-[12px] text-on-surface">
            <Icon name="check" filled className="mr-1 inline text-[13px]" />
            {t("merge.finishedSummary")}
          </p>
        )}

        {/* Per-epic results */}
        {progress?.results && progress.results.length > 0 && (
          <div className="space-y-1">
            {progress.results.map((r) => (
              <div key={r.epic_id} className="flex items-start gap-2">
                <Icon
                  name={RESULT_ICONS[r.status]}
                  filled={r.status === "merged"}
                  className={cn("mt-0.5 shrink-0 text-[13px]", RESULT_COLOR[r.status])}
                />
                <div className="min-w-0 flex-1">
                  <span className={cn("font-mono text-[12px]", RESULT_COLOR[r.status])}>
                    {r.epic_id}
                  </span>
                  <span className="ml-2 font-mono text-[11px] text-outline">
                    {resultStatusLabel(r.status)}
                  </span>
                  {r.detail && (
                    <span className="ml-2 font-mono text-[11px] text-on-surface-variant">
                      {r.detail}
                    </span>
                  )}
                </div>
                {/* Link to diff tab for non-merged results */}
                {r.status !== "merged" && r.status !== "skipped" && (
                  <Link
                    href={`/projects/${projectId}/epics/${r.epic_id}/diff`}
                    className="shrink-0 font-mono text-[11px] text-on-surface-variant transition-colors hover:text-on-surface"
                  >
                    {t("merge.viewDiff")}
                  </Link>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
