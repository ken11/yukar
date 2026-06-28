/**
 * useResolveRun — SSE state machine for a resolve run
 *
 * Extracts 4 useState + 2 ref + stream subscription + resolveMutation.
 * #44: separated from diff-page-client.tsx:178-333.
 */

"use client";

import type { QueryClient } from "@tanstack/react-query";
import { useMutation } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";
import type { RunEvent } from "@/lib/api/endpoints";
import { gitResolve } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { useEventStream } from "@/lib/sse/use-event-stream";
import { makeResolveEventHandlers } from "./resolve-event-handlers";

export type ResolveStatus = "idle" | "running" | "completed" | "failed" | "unknown";

export interface UseResolveRunOptions {
  projectId: string;
  epicId: string;
  activeRepo: string;
  mode: "working" | "epic";
  qc: QueryClient;
  onResolved: () => void;
}

export interface UseResolveRunResult {
  resolveStatus: ResolveStatus;
  resolveLastEvent: string;
  resolveError: string | null;
  isResolving: boolean;
  startResolve: () => void;
  dismissResolve: () => void;
}

export function useResolveRun({
  projectId,
  epicId,
  activeRepo,
  mode,
  qc,
  onResolved,
}: UseResolveRunOptions): UseResolveRunResult {
  const [resolveRunId, setResolveRunId] = useState<string | null>(null);
  const [resolveStatus, setResolveStatus] = useState<ResolveStatus>("idle");
  const [resolveLastEvent, setResolveLastEvent] = useState<string>("");
  const [resolveError, setResolveError] = useState<string | null>(null);
  // Guard flag to trigger refetch exactly once as an SSE error fallback
  const resolveErrorRefetchedRef = useRef(false);

  // Access the latest resolveStatus via ref to avoid stale closure
  const resolveStatusRef = useRef(resolveStatus);
  resolveStatusRef.current = resolveStatus;

  // Track the latest value via ref for generating handlers with run_id filtering
  const resolveRunIdRef = useRef(resolveRunId);
  resolveRunIdRef.current = resolveRunId;

  // Terminal fallback: transition to "unknown" if a terminal event was not received when the SSE disconnects
  const handleResolveStreamError = useCallback(() => {
    const status = resolveStatusRef.current;
    if (status !== "running") return;
    setResolveStatus("unknown");
    if (!resolveErrorRefetchedRef.current) {
      resolveErrorRefetchedRef.current = true;
      void qc.refetchQueries({
        queryKey: queryKeys.git.diff(projectId, epicId, activeRepo, mode),
      });
      void qc.refetchQueries({
        queryKey: queryKeys.git.diffSummary(projectId, epicId, mode),
      });
    }
  }, [qc, projectId, epicId, activeRepo, mode]);

  const resolveEventsUrl =
    resolveStatus === "running" ? `/api/projects/${projectId}/epics/${epicId}/run/events` : null;

  useEventStream<RunEvent>({
    url: resolveEventsUrl,
    onError: handleResolveStreamError,
    onMessage: ({ data }) => {
      if (!data || typeof data !== "object" || !("type" in data)) return;
      const event = data as RunEvent;

      makeResolveEventHandlers({
        resolveRunId: resolveRunIdRef.current,
        onRunStarted: () => {
          setResolveLastEvent("Run started");
        },
        onRunCompleted: () => {
          setResolveStatus("completed");
          setResolveLastEvent("Conflicts resolved");
          onResolved();
          // #18: consolidate with the umbrella key queryKeys.git.all()
          qc.invalidateQueries({ queryKey: queryKeys.git.all() });
        },
        onRunFailed: (error) => {
          setResolveStatus("failed");
          setResolveError(error);
        },
        onWorkerStarted: (workerId) => {
          setResolveLastEvent(`Worker started: ${workerId}`);
        },
        onWorkerCompleted: (workerId) => {
          setResolveLastEvent(`Worker completed: ${workerId}`);
        },
      })(event);
    },
  });

  const resolveMutation = useMutation({
    mutationFn: () => gitResolve(projectId, epicId, { repo: activeRepo }),
    onSuccess: (data) => {
      setResolveRunId(data.run_id);
      resolveRunIdRef.current = data.run_id;
      setResolveError(null);
      setResolveLastEvent("Starting resolve run…");
      resolveErrorRefetchedRef.current = false;
      setResolveStatus("running");
    },
    onError: (err) => {
      setResolveStatus("idle");
      setResolveError(err instanceof Error ? err.message : "Failed to start resolve");
    },
  });

  const startResolve = useCallback(() => {
    setResolveError(null);
    setResolveLastEvent("Starting…");
    resolveMutation.mutate();
  }, [resolveMutation]);

  const dismissResolve = useCallback(() => {
    setResolveStatus("idle");
    setResolveError(null);
  }, []);

  return {
    resolveStatus,
    resolveLastEvent,
    resolveError,
    isResolving: resolveMutation.isPending,
    startResolve,
    dismissResolve,
  };
}
