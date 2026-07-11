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
import type { Message, RunEvent, RunState } from "@/lib/api/endpoints";
import { getRunState, getThreadMessages } from "@/lib/api/endpoints";
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
  CurrentRun,
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
 * P3: "waiting" is the resting state (your turn). A real parked run
 * (run_id non-empty) dispatches USER_INPUT_REQUESTED so the your-turn banner
 * is restored from REST alone; a never-run epic (synthesised run_id="") is
 * left at the reducer's default waiting state without the parked marker.
 * The question itself needs no restore path — it is the agent's final message
 * in the thread (pending_question was removed with ask_user).
 *
 * P4 attribution split — two independent concepts:
 *   - activeTrialId (composer rights / tree / links): epic.active_thread_id
 *     with the non-archived-manager fallback. RunState.manager_thread is NOT a
 *     candidate anymore — during a reviewer run it points at the reviewer
 *     thread and used to misattribute the trial (the root-cause bug).
 *   - currentRun (your-turn banner attribution + role wording): the run's own
 *     conversation, i.e. RunState.manager_thread + RunState.role.
 *
 * @param primaryActiveTrialId - Highest-priority value for activeTrialId.
 *   Pass epic.active_thread_id when it has been confirmed.
 * @param fallbackActiveTrialId - Fallback when primaryActiveTrialId is null
 *   (first thread in initialThreads with role=manager && status!="archived").
 *   If also null, consumers use "manager" as the final fallback.
 */
function dispatchForRunStatus(
  runState: Pick<
    import("@/lib/api/endpoints").RunState,
    "status" | "run_id" | "manager_thread" | "role"
  >,
  dispatchFn: (action: import("./run-activity/actions").RunActivityAction) => void,
  primaryActiveTrialId?: string | null,
  fallbackActiveTrialId?: string | null,
): void {
  const { status, run_id, manager_thread, role } = runState;

  // Resolution order: primaryActiveTrialId(epic.active_thread_id) → fallback → null.
  // epic.active_thread_id is the sole authority for the trial; the old
  // RunState.manager_thread fallback is gone (P4) — it is the RUN's thread and
  // caused both the "stale manager_thread hides the composer in the gap between
  // trial creation and first run" regression workaround and the reviewer
  // misattribution.
  //
  // Even when all candidates are null (e.g. all trials archived), dispatch null to resolve the sticky
  // behavior that keeps holding the previous value (archived id). During a run, epic.active_thread_id
  // is always non-null, so there is no regression where it becomes null mid-run and the composer disappears.
  const resolvedTrialId = primaryActiveTrialId ?? fallbackActiveTrialId ?? null;
  dispatchFn({ type: "SET_ACTIVE_TRIAL_ID", threadId: resolvedTrialId });

  // currentRun: the conversation this run rides on + its role — the banner
  // attribution source. Only a real run (run_id non-empty) counts.
  if (run_id && manager_thread) {
    dispatchFn({ type: "SET_CURRENT_RUN", threadId: manager_thread, role: role ?? "manager" });
  }

  if (status === "running") dispatchFn({ type: "RUN_STARTED" });
  else if (status === "paused") dispatchFn({ type: "RUN_PAUSED" });
  else if (status === "completed") dispatchFn({ type: "RUN_COMPLETED" });
  else if (status === "error") dispatchFn({ type: "RUN_FAILED" });
  else if (status === "waiting" && run_id) {
    // A real run parked here (your turn) — restore the parked marker from REST
    // alone (no SSE replay dependency). A synthesised never-run state
    // (run_id="") stays at the reducer default: waiting without the marker.
    // Attribution: the run's OWN thread (manager_thread — for a reviewer run
    // this is the reviewer thread), falling back to the trial only when the
    // state predates manager_thread.
    const parkedThreadId = manager_thread ?? resolvedTrialId ?? "manager";
    dispatchFn({ type: "USER_INPUT_REQUESTED", threadId: parkedThreadId });
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
   * Highest-priority source for activeTrialId (composer rights).
   * Resolution order: activeThreadId(epic.active_thread_id) → non-archived manager in initialThreads → "manager"
   *
   * P4 split: RunState.manager_thread is no longer a candidate here — it is the
   * RUN's own thread (currentRun) and, during a reviewer run, points at the
   * reviewer thread. Removing it also removes the regression class where a
   * stale manager_thread overwrote epic.active_thread_id in the gap between
   * "new trial creation" and "first run start", hiding the composer.
   *
   * The fallback is the first thread in initialThreads with role=manager && status!=="archived".
   * initialThreads is an initial RSC prop and is not a live subscription (live cache is not used).
   * The only update path for the composer is activeThreadId → SET_ACTIVE_TRIAL_ID;
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

    // activeTrialId resolution (composer rights — P4 split):
    // Highest priority: activeThreadId(epic.active_thread_id) — authoritative value guaranteed by the backend.
    // Fallback: first thread in initialThreads with role=manager && status!="archived".
    // Final: "manager" (backward compat, applied by consumers).
    // RunState.manager_thread is NOT a candidate (it is the run's own thread —
    // a reviewer run would misattribute the trial; banner attribution reads it
    // via currentRun instead). This also removes the old regression class where
    // a stale manager_thread overwrote active_thread_id in the gap between
    // "new trial creation" and "first run start", hiding the composer.
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
      // primaryMgrThreadId(epic.active_thread_id) is highest priority, fallbackFromThreads is third candidate
      dispatchForRunStatus(sourceRunState, dispatch, primaryMgrThreadId, fallbackFromThreads);
    } else {
      // Even when RunState is not yet available (e.g. immediately after trial creation on an idle Epic),
      // initialize activeTrialId so the Topbar link points to the correct thread.
      // Dispatch even when null to prevent sticky (holding previous value) behavior.
      const initThreadId = primaryMgrThreadId ?? fallbackFromThreads ?? null;
      dispatch({ type: "SET_ACTIVE_TRIAL_ID", threadId: initThreadId });
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

  // P4 parked-thread sync, coalesced.  user_input_requested triggers a REST
  // round-trip (authoritative RunState for the banner's role + the parked
  // thread's persisted messages), but the SSE replay buffer can carry dozens
  // of park events from earlier turns on every mount/reconnect — the timeout
  // coalesces a synchronous burst into ONE round-trip for the latest event.
  const parkedSyncRef = useRef<{ scheduled: boolean; threadId: string; timer: number | null }>({
    scheduled: false,
    threadId: "",
    timer: null,
  });
  // Cancel a pending sync when the epic changes or the hook unmounts.
  // biome-ignore lint/correctness/useExhaustiveDependencies: cleanup keyed to epic identity only
  useEffect(() => {
    return () => {
      const s = parkedSyncRef.current;
      if (s.timer !== null) window.clearTimeout(s.timer);
      s.scheduled = false;
      s.timer = null;
    };
  }, [projectId, epicId]);

  const syncParkedThread = useCallback(() => {
    const s = parkedSyncRef.current;
    s.scheduled = false;
    s.timer = null;
    const threadId = s.threadId;

    // 1. Authoritative RunState: refresh the identity fields of the runState
    // cache (SSE only patches status, so run_id / manager_thread / role would
    // otherwise stay frozen at mount time — a stale hybrid that can revive
    // the pre-P4 misattribution when dispatchForRunStatus re-reads the cache)
    // and give the banner its role.  Status stays SSE-owned: a late response
    // must not overwrite a newer running/stopped transition.
    getRunState(projectId, epicId)
      .then((rs) => {
        qc.setQueryData<RunState>(queryKeys.runState.get(projectId, epicId), (prev) =>
          prev
            ? {
                ...prev,
                run_id: rs.run_id,
                manager_thread: rs.manager_thread,
                role: rs.role,
              }
            : rs,
        );
        if (rs.run_id && rs.manager_thread) {
          dispatch({
            type: "SET_CURRENT_RUN",
            threadId: rs.manager_thread,
            role: rs.role ?? "manager",
          });
        }
      })
      .catch(() => {
        // Ignore — the banner falls back to neutral wording without a role.
      });

    // 2. Parked thread messages: the agent's final message (question/report)
    // is on disk by the time the park event fires.  manager_message usually
    // covers the refetch, but it is not replayed after the run-teardown SSE
    // sentinel closes the stream (e.g. POST /review shelving the parked run),
    // so the replayed park event anchors it instead.  MERGE by message_id
    // rather than invalidate: an invalidation's refetch can resolve after a
    // concurrent SSE user_message_committed setQueryData and wipe the user's
    // just-sent bubble (use-thread-messages' no-invalidate contract).
    if (threadId) {
      getThreadMessages(projectId, epicId, threadId)
        .then((fetched) => {
          qc.setQueryData<Message[]>(
            queryKeys.threads.messages(projectId, epicId, threadId),
            (prev) => {
              if (!prev) return fetched;
              const known = new Set(fetched.map((m) => m.message_id));
              // Keep cached messages the fetch does not know yet (optimistic /
              // SSE-committed after the fetch left) — they are newer.
              return [...fetched, ...prev.filter((m) => !known.has(m.message_id))];
            },
          );
        })
        .catch(() => {
          // Ignore — manager_message invalidation covers the live path.
        });
    }
  }, [projectId, epicId, qc]);

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

      // 3. Parked-thread sync (P4): the your-turn signal carries the thread
      // but neither the role nor the parked thread's final message.  Schedule
      // ONE coalesced REST round-trip (see syncParkedThread) — a replay burst
      // of park events collapses into a single fetch for the latest one.
      // Attribution only — SET_CURRENT_RUN never touches runStatus, so a late
      // response cannot fake execution state.
      if (event.type === "user_input_requested") {
        const s = parkedSyncRef.current;
        s.threadId = event.thread_id ?? "";
        if (!s.scheduled) {
          s.scheduled = true;
          s.timer = window.setTimeout(syncParkedThread, 0);
        }
      }
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
