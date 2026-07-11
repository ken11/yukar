/**
 * Run lifecycle handler.
 *
 * Target cases: RUN_PREPARING / RUN_STARTED / RUN_COMPLETED / RUN_FAILED / RUN_STOPPED /
 *               RUN_PAUSED / RUN_RESUMED / PAUSE_EFFECTIVE / SET_PAUSE_PENDING
 */

import type { RunActivityAction } from "../actions";
import type { ManagerNodeState, RunActivityState } from "../types";
import { finalizeTree } from "./tree";

export function handleLifecycle(
  state: RunActivityState,
  action: RunActivityAction,
): RunActivityState | null {
  switch (action.type) {
    // Transient "preparing" phase before Manager starts (index refresh).
    // runError is cleared here so that if a previous run ended with error,
    // the error banner disappears immediately when a new run begins preparing.
    case "RUN_PREPARING":
      return { ...state, runStatus: "preparing", runError: null, awaitingInput: null };

    // Always push the Manager node to the tree simultaneously with run start (spec: "display Manager simultaneously with run start").
    // For a new run, the INIT from threads.yaml has not yet arrived and the Manager node does not exist,
    // so if not created here, subsequent MANAGER_TURN_STARTED / TOKEN would become no-ops
    // (`if (!tree.manager) return state`) and the tree would remain empty.
    // The Manager's thread_id is variable with multi-trial support. Use state.managerThreadId
    // (REST-restored from RunState.manager_thread) when confirmed; otherwise inherit the existing node's threadId.
    // If an existing node is present, preserve status/streaming and update only threadId to the active trial's real id.
    case "RUN_STARTED": {
      const tree = state.treeState;
      // Real id of the active trial: use managerThreadId if confirmed by REST.
      // If unconfirmed (null) but an existing node is present, inherit the existing threadId.
      // If neither is available, use the temporary placeholder "manager"; it will be overwritten by a subsequent SET_MANAGER_THREAD_ID.
      const resolvedThreadId = state.managerThreadId ?? tree.manager?.threadId ?? "manager";
      const manager: ManagerNodeState = tree.manager
        ? { ...tree.manager, threadId: resolvedThreadId }
        : {
            role: "manager",
            threadId: resolvedThreadId,
            status: "idle",
            isStreaming: false,
            lastMessage: undefined,
          };
      return {
        ...state,
        runStatus: "running",
        runError: null,
        awaitingInput: null,
        treeState: { ...tree, manager },
      };
    }

    // JOB runs only (resolve / arbiter). #34: consolidated into finalizeTree()
    case "RUN_COMPLETED": {
      const tree = state.treeState;
      const { manager, workers } = finalizeTree(tree);
      return {
        ...state,
        runStatus: "completed",
        pausePending: false,
        runError: null,
        awaitingInput: null,
        treeState: { ...tree, manager, workers },
      };
    }

    // #34: consolidated into finalizeTree()
    case "RUN_FAILED": {
      const tree = state.treeState;
      const { manager, workers } = finalizeTree(tree);
      return {
        ...state,
        runStatus: "error",
        pausePending: false,
        runError: action.error ?? null,
        awaitingInput: null,
        treeState: { ...tree, manager, workers },
      };
    }

    // User-initiated stop. The run settles back into "waiting" (your turn) —
    // the conversation is intact and re-runnable. Not a completion/failure,
    // but finalize the tree's streaming/thinking display to stop it.
    case "RUN_STOPPED": {
      const tree = state.treeState;
      const { manager, workers } = finalizeTree(tree);
      return {
        ...state,
        runStatus: "waiting",
        pausePending: false,
        runError: null,
        awaitingInput: null,
        treeState: { ...tree, manager, workers },
      };
    }

    case "RUN_PAUSED":
      return { ...state, runStatus: "paused" };

    case "RUN_RESUMED":
      return { ...state, runStatus: "running", pausePending: false, awaitingInput: null };

    // The run parked in "waiting" — it is the user's turn. The question/report
    // is the agent's final message in the thread (no payload here).
    case "USER_INPUT_REQUESTED": {
      // Do not downgrade a terminal run (completed=job / error) when a delayed
      // parked snapshot arrives due to a race between REST (getRunState) and SSE.
      // Symmetric guard to USER_INPUT_RESOLVED which only reverts to running when waiting.
      if (state.runStatus === "completed" || state.runStatus === "error") {
        return state;
      }
      return {
        ...state,
        runStatus: "waiting",
        awaitingInput: { threadId: action.threadId },
      };
    }

    case "USER_INPUT_RESOLVED":
      // The user's reply woke the run: clear the parked marker and revert to running.
      // Only revert when an actually-parked run exists (waiting + marker):
      // "waiting" alone is also the default/stopped resting state, and a delayed
      // resolved replay after stop / terminal states must not fake "running".
      return {
        ...state,
        runStatus:
          state.runStatus === "waiting" && state.awaitingInput !== null
            ? "running"
            : state.runStatus,
        awaitingInput: null,
      };

    case "PAUSE_EFFECTIVE":
      return { ...state, pausePending: false };

    case "SET_PAUSE_PENDING":
      return { ...state, pausePending: action.value };

    default:
      return null;
  }
}
