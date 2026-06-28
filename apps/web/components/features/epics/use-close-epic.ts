"use client";

/**
 * useCloseEpic / useReopenEpic — TanStack Query mutations for Feature 1 (Epic Close).
 *
 * closeEpic:  POST /api/projects/{p}/epics/{id}/close → Epic  (409 if run active)
 * reopenEpic: PATCH /api/projects/{p}/epics/{id}      body { status: "planned" } → Epic
 *
 * Both invalidate epics.list + epics.detail on success.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, closeEpic, patchEpic } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";

export function useCloseEpic(projectId: string) {
  const qc = useQueryClient();

  return useMutation({
    mutationFn: (epicId: string) => closeEpic(projectId, epicId),
    onSuccess: (_data, epicId) => {
      qc.invalidateQueries({ queryKey: queryKeys.epics.list(projectId) });
      qc.invalidateQueries({ queryKey: queryKeys.epics.detail(projectId, epicId) });
    },
  });
}

export function useReopenEpic(projectId: string) {
  const qc = useQueryClient();

  return useMutation({
    mutationFn: (epicId: string) => patchEpic(projectId, epicId, { status: "planned" }),
    onSuccess: (_data, epicId) => {
      qc.invalidateQueries({ queryKey: queryKeys.epics.list(projectId) });
      qc.invalidateQueries({ queryKey: queryKeys.epics.detail(projectId, epicId) });
    },
  });
}

/** Extract human-readable error message from a closeEpic ApiError. */
export function extractCloseError(err: unknown, fallbackMsg: string): string {
  if (err instanceof ApiError && err.status === 409) {
    return fallbackMsg;
  }
  if (err instanceof Error) return err.message;
  return String(err);
}
