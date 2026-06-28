"use client";

/**
 * useRunActivity — Store that manages the entire agent tree with a single EventSource at the epic level.
 *
 * Consolidates:
 *   - use-run-events: cache patch + SSE subscription
 *   - use-run-state: RunState management
 *   - use-thread-tree: tree reducer
 *   Per-thread individual EventSources are abolished. Tokens are demuxed by thread_id to manage live buffers.
 *
 * architecture.md §2.2: RSC initial render + Client/SSE patch. Polling is prohibited.
 *
 * This file contains the hook body + barrel re-export of all public names.
 * Implementation is split into sub-modules under lib/sse/run-activity/.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import type { RunEvent } from "@/lib/api/endpoints";
import { getRunState } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { applyRunCachePatch, toRunActivityAction } from "./run-activity/cache-patch";
import { initialState, runActivityReducer } from "./run-activity/reducer";
import { defaultLiveState } from "./run-activity/selectors";
import { useEventStream } from "./use-event-stream";

// ---- Barrel re-export of all public names ----
// Keeps imports in tests and 4 components intact (#33)

// actions
export type { RunActivityAction } from "./run-activity/actions";
// reducer
export { runActivityReducer } from "./run-activity/reducer";
// selectors
export { isAgentActive, selectThreadLiveState } from "./run-activity/selectors";
// types
export type {
  EvaluatorNodeState,
  EvaluatorNodeStatus,
  ManagerNodeState,
  ManagerNodeStatus,
  RunActivityState,
  RunActivityStatus,
  ThreadLiveState,
  ThreadTreeState,
  WorkerNodeState,
  WorkerNodeStatus,
} from "./run-activity/types";

// ---- helpers ----

/**
 * Pure function that maps RunState to the corresponding RunActivityAction.
 * #4: Unified the identical if-else chain that was duplicated in 2 places inside use-run-activity.
 * #REST-restore: When awaiting_input, the bubble can be restored with REST alone if pending_question is non-empty.
 * Does nothing for status values that cannot be converted.
 *
 * @param primaryManagerThreadId - Highest-priority value for managerThreadId.
 *   Pass epic.active_thread_id when it has been confirmed.
 *   If non-null, takes priority over RunState.manager_thread.
 *   If null, resolves in order: RunState.manager_thread → fallbackManagerThreadId.
 * @param fallbackManagerThreadId - Fallback when manager_thread is null and primaryManagerThreadId is also null
 *   (first thread in initialThreads with role=manager && status!="archived").
 *   If also null, "manager" is used as the final fallback.
 */
function dispatchForRunStatus(
  runState: Pick<
    import("@/lib/api/endpoints").RunState,
    "status" | "pending_question" | "manager_thread"
  >,
  dispatchFn: (action: import("./run-activity/actions").RunActivityAction) => void,
  primaryManagerThreadId?: string | null,
  fallbackManagerThreadId?: string | null,
): void {
  const { status, pending_question, manager_thread } = runState;

  // Resolution order: primaryManagerThreadId(epic.active_thread_id) → manager_thread → fallback → null
  // When epic.active_thread_id has been confirmed, it takes priority over RunState.manager_thread.
  // This prevents a regression where a stale manager_thread overwrites active_thread_id
  // in the gap between "new trial creation" and "first run start".
  //
  // Even when all candidates are null (e.g. all trials archived), dispatch null to resolve the sticky
  // behavior that keeps holding the previous value (archived id). During a run, epic.active_thread_id
  // is always non-null, so there is no regression where it becomes null mid-run and the composer disappears.
  const resolvedMgrThreadId =
    primaryManagerThreadId ?? manager_thread ?? fallbackManagerThreadId ?? null;
  dispatchFn({ type: "SET_MANAGER_THREAD_ID", threadId: resolvedMgrThreadId });

  if (status === "running") dispatchFn({ type: "RUN_STARTED" });
  else if (status === "paused") dispatchFn({ type: "RUN_PAUSED" });
  else if (status === "completed") dispatchFn({ type: "RUN_COMPLETED" });
  else if (status === "error") dispatchFn({ type: "RUN_FAILED" });
  else if (status === "interrupted") dispatchFn({ type: "RUN_INTERRUPTED" });
  else if (status === "awaiting_input") {
    // If pending_question is non-empty, restore the bubble immediately with REST alone (no SSE replay dependency).
    // If null/empty, only confirm the status and wait for SSE replay to fill in the rest.
    const question = pending_question ?? "";
    // Resolution order: manager_thread → fallback (epic.active_thread_id, etc.) → "manager"
    const mgrThreadId = resolvedMgrThreadId ?? "manager";
    dispatchFn({ type: "USER_INPUT_REQUESTED", threadId: mgrThreadId, question });
  }
}

// ---- hook ----

export function useRunActivity({
  projectId,
  epicId,
  initialThreads,
  initialRunState,
  activeThreadId,
}: {
  projectId: string;
  epicId: string;
  initialThreads?: import("@/lib/api/endpoints").ThreadEntry[];
  initialRunState?: import("@/lib/api/endpoints").RunState;
  /**
   * Pass epic.active_thread_id from the caller.
   * Used as the highest-priority value for managerThreadId. Takes priority over RunState.manager_thread.
   * Resolution order: activeThreadId(epic.active_thread_id) → RunState.manager_thread → non-archived manager in liveThreads → "manager"
   *
   * This priority order prevents a regression where a stale RunState.manager_thread overwrites
   * epic.active_thread_id in the gap between "new trial creation" and "first run start", causing the composer to disappear.
   *
   * The fallback (third candidate) is the first thread in initialThreads with role=manager && status!=="archived".
   * initialThreads is an initial RSC prop and is not a live subscription (live cache is not used).
   * The only update path for the composer is activeThreadId → SET_MANAGER_THREAD_ID;
   * the archived exclusion in INIT / applyTreeInit is a correction to tree display nodes and is unrelated to the composer.
   */
  activeThreadId?: string | null;
}): {
  state: import("./run-activity/types").RunActivityState;
  getLiveState: (threadId: string) => import("./run-activity/types").ThreadLiveState;
  setPausePending: (value: boolean) => void;
  clearLiveBuffer: (threadId: string) => void;
} {
  const qc = useQueryClient();
  const [state, dispatch] = useReducer(runActivityReducer, initialState);

  // On epicId / initialThreads change: RESET→INIT.
  // First mount: no RESET → set runStatus → INIT (following Mj6 pattern)
  // Epic switch: RESET → set new runStatus → INIT
  const prevEpicRef = useRef<string | null>(null); // null = not yet initialized
  // biome-ignore lint/correctness/useExhaustiveDependencies: qc/initialRunState are stable references
  useEffect(() => {
    const epicChanged = prevEpicRef.current !== null && prevEpicRef.current !== epicId;

    if (epicChanged) {
      dispatch({ type: "RESET" });
    }
    prevEpicRef.current = epicId;

    // managerThreadId resolution:
    // Highest priority: activeThreadId(epic.active_thread_id) — authoritative value guaranteed by the backend.
    // Next: RunState.manager_thread — used inside dispatchForRunStatus.
    // Fallback: first thread in initialThreads with role=manager && status!="archived".
    // Final: "manager" (backward compat).
    //
    // When activeThreadId is non-null it takes priority over RunState.manager_thread.
    // This prevents a regression where a stale manager_thread overwrites active_thread_id
    // in the gap between "new trial creation" and "first run start", causing the composer to disappear.
    const primaryMgrThreadId = activeThreadId ?? null;
    // Find the first non-archived manager thread from initialThreads (for use as fallback)
    const fallbackFromThreads =
      initialThreads?.find((t) => t.role === "manager" && t.status !== "archived")?.id ?? null;

    // Fetch the runState for the current epicId from cache or prop and set runStatus
    const cachedRunState = qc.getQueryData<import("@/lib/api/endpoints").RunState>(
      queryKeys.runState.get(projectId, epicId),
    );
    const sourceRunState = cachedRunState ?? (epicChanged ? undefined : initialRunState);
    if (sourceRunState) {
      // #4: unified the duplicated if-else chain into dispatchForRunStatus
      // #REST-restore: pass the full RunState so pending_question is also available
      // primaryMgrThreadId(epic.active_thread_id) is highest priority, fallbackFromThreads is third candidate
      dispatchForRunStatus(sourceRunState, dispatch, primaryMgrThreadId, fallbackFromThreads);
    } else {
      // Even when RunState is not yet available (e.g. immediately after trial creation on an idle Epic),
      // initialize managerThreadId so the Topbar link points to the correct thread.
      // Dispatch even when null to prevent sticky (holding previous value) behavior.
      const initThreadId = primaryMgrThreadId ?? fallbackFromThreads ?? null;
      dispatch({ type: "SET_MANAGER_THREAD_ID", threadId: initThreadId });
    }

    // When threads exist, perform non-destructive reconciliation via INIT
    if (initialThreads && initialThreads.length > 0) {
      dispatch({ type: "INIT", threads: initialThreads });
    }
  }, [epicId, initialThreads, projectId, activeThreadId]);

  // SSE enabled condition: opened while running/paused. Always kept open to receive run_started even when idle.
  const sseUrl =
    projectId && epicId ? `/api/projects/${projectId}/epics/${epicId}/run/events` : null;

  // Latest reference to reducer state (read from the onReconnect closure)
  const stateRef = useRef(state);
  stateRef.current = state;

  // Minor1: hold latest values in refs so the REST fetch side can resolve the fallback
  const activeThreadIdRef = useRef(activeThreadId);
  activeThreadIdRef.current = activeThreadId;
  const initialThreadsRef = useRef(initialThreads);
  initialThreadsRef.current = initialThreads;

  // #35: split onMessage into applyRunCachePatch + toRunActivityAction
  useEventStream<RunEvent>({
    url: sseUrl,
    onMessage: ({ data }) => {
      if (!data || typeof data !== "object" || !("type" in data)) return;
      const event = data as RunEvent;

      // 1. TanStack Query cache patch
      applyRunCachePatch(qc, projectId, epicId, event);

      // 2. Dispatch to the store's reducer
      const action = toRunActivityAction(event);
      if (action) dispatch(action);
    },
    // On reconnect: the backend resends the backfill, so clear all existing live buffers.
    // After clearing, when the backfill arrives the buffer is rebuilt from scratch without double rendering.
    onReconnect: () => {
      const currentBuffers = stateRef.current.liveBuffers;
      for (const threadId of Object.keys(currentBuffers)) {
        dispatch({ type: "CLEAR_LIVE_BUFFER", threadId });
      }
    },
  });

  // Initial fetch of runState (fills in state before SSE arrives)
  // biome-ignore lint/correctness/useExhaustiveDependencies: qc is a stable reference; only needs to rerun when projectId/epicId changes
  useEffect(() => {
    if (!projectId || !epicId) return;
    getRunState(projectId, epicId)
      .then((rs) => {
        // #4: unified the duplicated if-else chain into dispatchForRunStatus
        // #REST-restore: pass the full RunState so pending_question is also available
        // Pass activeThreadId(epic.active_thread_id) as primaryMgrThreadId at highest priority.
        // This prevents a stale RunState.manager_thread from overwriting active_thread_id.
        const primaryMgrThreadId = activeThreadIdRef.current ?? null;
        const fallbackFromThreads =
          initialThreadsRef.current?.find((t) => t.role === "manager" && t.status !== "archived")
            ?.id ?? null;
        dispatchForRunStatus(rs, dispatch, primaryMgrThreadId, fallbackFromThreads);
        // Also save to cache
        qc.setQueryData(queryKeys.runState.get(projectId, epicId), rs);
      })
      .catch(() => {
        // Ignore fetch failures (SSE will fill in the state later)
      });
  }, [projectId, epicId]);

  const getLiveState = useCallback(
    (threadId: string): import("./run-activity/types").ThreadLiveState =>
      // #10: replaced inline ?? object with defaultLiveState()
      state.liveBuffers[threadId] ?? defaultLiveState(),
    [state.liveBuffers],
  );

  const setPausePending = useCallback(
    (value: boolean) => dispatch({ type: "SET_PAUSE_PENDING", value }),
    [],
  );

  const clearLiveBuffer = useCallback(
    (threadId: string) => dispatch({ type: "CLEAR_LIVE_BUFFER", threadId }),
    [],
  );

  const result = useMemo(
    () => ({ state, getLiveState, setPausePending, clearLiveBuffer }),
    [state, getLiveState, setPausePending, clearLiveBuffer],
  );

  return result;
}
