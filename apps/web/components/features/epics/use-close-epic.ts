"use client";

/**
 * useCompleteEpic / useReopenEpic — TanStack Query mutations for the 1-bit epic
 * lifecycle (open ⇄ completed, user-owned).
 *
 * completeEpic: PATCH /api/projects/{p}/epics/{id} body { status: "completed" } → Epic
 *               (409 if a run is active — stop the run first)
 * reopenEpic:   PATCH /api/projects/{p}/epics/{id} body { status: "open" } → Epic
 *
 * "Complete" is the user's single finish action: it covers both approving done
 * work and abandoning unfinished work (the old close/approve pair collapsed
 * into one operation). Both mutations invalidate epics.list + epics.detail.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, patchEpic } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";

export function useCompleteEpic(projectId: string) {
  const qc = useQueryClient();

  return useMutation({
    mutationFn: (epicId: string) => patchEpic(projectId, epicId, { status: "completed" }),
    onSuccess: (_data, epicId) => {
      qc.invalidateQueries({ queryKey: queryKeys.epics.list(projectId) });
      qc.invalidateQueries({ queryKey: queryKeys.epics.detail(projectId, epicId) });
    },
  });
}

export function useReopenEpic(projectId: string) {
  const qc = useQueryClient();

  return useMutation({
    mutationFn: (epicId: string) => patchEpic(projectId, epicId, { status: "open" }),
    onSuccess: (_data, epicId) => {
      qc.invalidateQueries({ queryKey: queryKeys.epics.list(projectId) });
      qc.invalidateQueries({ queryKey: queryKeys.epics.detail(projectId, epicId) });
    },
  });
}

/** Extract a human-readable error message from a completeEpic ApiError. */
export function extractCompleteError(err: unknown, fallbackMsg: string): string {
  if (err instanceof ApiError && err.status === 409) {
    return fallbackMsg;
  }
  if (err instanceof Error) return err.message;
  return String(err);
}
