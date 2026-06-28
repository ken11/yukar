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

    // #34: consolidated into finalizeTree()
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

    // User-initiated stop. run reverts to idle (re-runnable), so controls return to Start Run.
    // Not a completion/failure, but finalize the tree's streaming/thinking display to stop it.
    case "RUN_STOPPED": {
      const tree = state.treeState;
      const { manager, workers } = finalizeTree(tree);
      return {
        ...state,
        runStatus: "idle",
        pausePending: false,
        runError: null,
        awaitingInput: null,
        treeState: { ...tree, manager, workers },
      };
    }

    // System-initiated interruption (e.g. restart detection). Can be resumed from completed/interrupted (spec Wave 3).
    case "RUN_INTERRUPTED": {
      const tree = state.treeState;
      const { manager, workers } = finalizeTree(tree);
      return {
        ...state,
        runStatus: "interrupted",
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

    case "USER_INPUT_REQUESTED": {
      // Do not revert to awaiting_input for terminal runs (completed/error/interrupted)
      // when a delayed awaiting snapshot arrives due to a race between REST(getRunState) and SSE.
      // Symmetric guard to USER_INPUT_RESOLVED which only reverts to running when awaiting.
      // Note: idle is the initial state and the restore origin on reload (idle→awaiting_input), so it is not excluded.
      if (
        state.runStatus === "completed" ||
        state.runStatus === "error" ||
        state.runStatus === "interrupted"
      ) {
        return state;
      }
      // getRunState / cache restore dispatches with only status known and question="".
      // An empty question confirms only runStatus as awaiting_input without changing awaitingInput.
      //   - If an existing real awaitingInput(threadId+question) is present, preserve it entirely.
      //   - Also keep null as null: set awaitingInput only when REST's pending_question or SSE replay's
      //     user_input_requested (with question) arrives.
      // This prevents the issue of a bubble being rendered in the intermediate state of question=""
      // (because `if (awaitingInput?.question)` in runtime.ts:257 treats "" as falsy).
      if (action.question === "") {
        return { ...state, runStatus: "awaiting_input" };
      }
      return {
        ...state,
        runStatus: "awaiting_input",
        awaitingInput: { threadId: action.threadId, question: action.question },
      };
    }

    case "USER_INPUT_RESOLVED":
      // Approval/answer completed: clear awaiting and revert to running.
      // Only revert runStatus to running when it is awaiting_input.
      // Guard to prevent erroneously reverting to running when a delayed resolved replays
      // after a terminal state (completed/failed/stopped, etc.).
      // Symmetric with MANAGER_TURN_STARTED which has the same "revert to running only when awaiting" guard.
      return {
        ...state,
        runStatus: state.runStatus === "awaiting_input" ? "running" : state.runStatus,
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
