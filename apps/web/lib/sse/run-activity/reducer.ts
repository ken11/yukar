/**
 * runActivityReducer — State transitions for the entire agent tree.
 *
 * #34: Consolidated RUN_COMPLETED / RUN_FAILED into finalizeTree().
 * #37: Clears the live buffer on completion events to prevent double rendering (because REST returns all messages).
 * #38: Removed the dead thread_id cast branch in MANAGER_TURN_STARTED.
 * #40: Removed no-op alias from RUN_STARTED; returns direct spread.
 *
 * Implementation is split into sub-directories by domain:
 *   reducer/lifecycle.ts   — run_* + pause lifecycle
 *   reducer/tree.ts        — Manager/Worker/Evaluator tree transitions + applyTreeInit/finalizeTree
 *   reducer/live-buffer.ts — TOKEN/TOOL_CALL/TOOL_RESULT
 */

import type { RunActivityAction } from "./actions";
import { handleLifecycle } from "./reducer/lifecycle";
import { handleLiveBuffer } from "./reducer/live-buffer";
import { handleTree, scopeTreeToManager } from "./reducer/tree";
import type { RunActivityState, ThreadTreeState } from "./types";

// ---- Initial state ----

const initialTreeState: ThreadTreeState = {
  manager: null,
  workers: {},
  evaluators: {},
  taskToWorker: {},
};

export const initialState: RunActivityState = {
  runStatus: "idle",
  pausePending: false,
  runError: null,
  awaitingInput: null,
  treeState: initialTreeState,
  liveBuffers: {},
  managerThreadId: null,
};

// ---- Main Reducer ----

export function runActivityReducer(
  state: RunActivityState,
  action: RunActivityAction,
): RunActivityState {
  if (action.type === "RESET") {
    return { ...initialState };
  }

  if (action.type === "SET_MANAGER_THREAD_ID") {
    // Update managerThreadId (REST authoritative data) while also syncing the tree's manager node threadId.
    // Live buffer key migration is not performed (SET_MANAGER_THREAD_ID arrives between turns, so
    // even if an existing buffer remains, MANAGER_TURN_STARTED after the threadId change will overwrite with the new id — no issue).
    const newManagerThreadId = action.threadId;
    // Sync the manager node id (when switching to a known trial) and scope the
    // worker/evaluator tree to the active trial, so a previous trial's agents
    // don't bleed into this one. When the id is null there is NO active trial,
    // so scoping clears any lingering nodes — this covers archiving the sole
    // trial mid-session (active_thread_id → null) where no INIT follows to
    // reconcile against the thread list.
    const synced =
      state.treeState.manager && newManagerThreadId
        ? {
            ...state.treeState,
            manager: { ...state.treeState.manager, threadId: newManagerThreadId },
          }
        : state.treeState;
    const treeState = scopeTreeToManager(synced, newManagerThreadId);
    return { ...state, managerThreadId: newManagerThreadId, treeState };
  }

  const lifecycleResult = handleLifecycle(state, action);
  if (lifecycleResult !== null) return lifecycleResult;

  const treeResult = handleTree(state, action);
  if (treeResult !== null) return treeResult;

  const liveResult = handleLiveBuffer(state, action);
  if (liveResult !== null) return liveResult;

  return state;
}
