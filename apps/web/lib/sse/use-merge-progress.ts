"use client";

/**
 * useMergeProgress — Demuxes EpicMergeProgressEvent and EpicStatusChangedEvent from the
 * single EventSource of ProjectEventStreamProvider and holds the latest progress state.
 *
 * The public API (return value shape and callback contract) is unchanged.
 * The projectId argument is retained for backward compatibility but is ignored since
 * the connection is managed by the Provider.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { EpicMergeProgressEvent } from "@/lib/api/endpoints";
import { useProjectEventStream } from "./project-event-stream-context";

export interface MergeProgressState {
  runId: string;
  total: number;
  completed: number;
  currentEpicId: string | null;
  phase: string;
  results: EpicMergeProgressEvent["results"];
  isFinished: boolean;
}

export interface UseMergeProgressReturn {
  progress: MergeProgressState | null;
  /** Reset local state (e.g. after dismissing the panel) */
  reset: () => void;
}

export function useMergeProgress(
  _projectId: string | undefined,
  onInvalidate?: () => void,
): UseMergeProgressReturn {
  const [progress, setProgress] = useState<MergeProgressState | null>(null);

  const { subscribe, subscribeReconnect } = useProjectEventStream();

  const onInvalidateRef = useRef(onInvalidate);
  onInvalidateRef.current = onInvalidate;

  // The project stream has no replay: a "finished" published while the
  // connection was down (network blip / hidden-tab suspension) never arrives,
  // which would leave an in-flight panel stuck forever. On reconnect, refetch
  // the board (REST reflects the real epic states) and drop unfinished
  // SSE-accumulated progress — a merge still running repaints on its next
  // progress event.
  useEffect(() => {
    return subscribeReconnect(() => {
      onInvalidateRef.current?.();
      setProgress((prev) => (prev !== null && !prev.isFinished ? null : prev));
    });
  }, [subscribeReconnect]);

  useEffect(() => {
    return subscribe(({ data }) => {
      if (data.type === "epic_merge_progress") {
        // discriminated-union narrowing: type === "epic_merge_progress" narrows to EpicMergeProgressEvent
        const ev = data as EpicMergeProgressEvent;
        const isFinished = ev.phase === "finished";
        setProgress({
          runId: ev.run_id,
          total: ev.total,
          completed: ev.completed,
          currentEpicId: ev.current_epic_id ?? null,
          phase: ev.phase,
          results: ev.results ?? [],
          isFinished,
        });
        // Invalidate when an epic finishes or the whole batch finishes
        if (ev.phase === "epic_done" || isFinished) {
          onInvalidateRef.current?.();
        }
      } else if (data.type === "epic_status_changed" || data.type === "epic_merged") {
        // User status toggle (complete/reopen) or a recorded merge fact → refresh board
        onInvalidateRef.current?.();
      }
    });
  }, [subscribe]);

  const reset = useCallback(() => setProgress(null), []);

  return { progress, reset };
}
