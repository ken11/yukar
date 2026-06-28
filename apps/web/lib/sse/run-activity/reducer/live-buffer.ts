/**
 * Live buffer handler + helpers.
 *
 * Target cases: TOKEN / TOOL_CALL / TOOL_RESULT
 *
 * #11: Removed export from ensureLiveBuffer since it has zero external imports (made it a module-internal helper).
 * The comment in tree.ts (#multi-turn-regression) is kept for context.
 */

import {
  applyTokenEvent,
  applyToolCallEvent,
  applyToolResultEvent,
} from "@/lib/assistant-ui/runtime";
import type { RunActivityAction } from "../actions";
import { defaultLiveState } from "../selectors";
import type { RunActivityState, ThreadTreeState } from "../types";

// ---- Tree update helper for TOKEN ----

/** Updates the status / isStreaming of tree nodes on TOKEN (does not revive terminal states) */
function applyTokenToTree(tree: ThreadTreeState, threadId: string): ThreadTreeState {
  if (tree.manager?.threadId === threadId) {
    if (tree.manager.status !== "completed") {
      return {
        ...tree,
        manager: { ...tree.manager, isStreaming: true, status: "thinking" },
      };
    }
  } else if (tree.workers[threadId]) {
    const w = tree.workers[threadId];
    if (w.status !== "completed" && w.status !== "failed") {
      return {
        ...tree,
        workers: {
          ...tree.workers,
          [threadId]: { ...w, isStreaming: true, status: "running" },
        },
      };
    }
  } else if (tree.evaluators[threadId]) {
    const ev = tree.evaluators[threadId];
    if (ev.status !== "accepted" && ev.status !== "rejected") {
      return {
        ...tree,
        evaluators: {
          ...tree.evaluators,
          [threadId]: { ...ev, isStreaming: true, status: "evaluating" },
        },
      };
    }
  }
  return tree;
}

// ---- Live buffer handler ----

export function handleLiveBuffer(
  state: RunActivityState,
  action: RunActivityAction,
): RunActivityState | null {
  switch (action.type) {
    case "TOKEN": {
      const { threadId, delta, msgIndex } = action;

      // Update tree node status / isStreaming (do not revive terminal states)
      const newTree = applyTokenToTree(state.treeState, threadId);

      // Add token to live buffer
      // #10: replaced inline ?? object with defaultLiveState()
      const prevBuf = state.liveBuffers[threadId] ?? defaultLiveState();
      const newStreamState = applyTokenEvent(prevBuf.streamState, delta, msgIndex ?? 0);

      return {
        ...state,
        treeState: newTree,
        liveBuffers: {
          ...state.liveBuffers,
          [threadId]: { streamState: newStreamState, isRunning: true },
        },
      };
    }

    case "TOOL_CALL": {
      const { threadId, event } = action;
      // #10: replaced inline ?? object with defaultLiveState()
      const prevBuf = state.liveBuffers[threadId] ?? defaultLiveState();
      return {
        ...state,
        liveBuffers: {
          ...state.liveBuffers,
          [threadId]: {
            ...prevBuf,
            streamState: applyToolCallEvent(prevBuf.streamState, event),
          },
        },
      };
    }

    case "TOOL_RESULT": {
      const { threadId, event } = action;
      // #10: replaced inline ?? object with defaultLiveState()
      const prevBuf = state.liveBuffers[threadId] ?? defaultLiveState();
      return {
        ...state,
        liveBuffers: {
          ...state.liveBuffers,
          [threadId]: {
            ...prevBuf,
            streamState: applyToolResultEvent(prevBuf.streamState, event),
          },
        },
      };
    }

    case "CLEAR_LIVE_BUFFER": {
      const { threadId } = action;
      if (!state.liveBuffers[threadId]) return state;
      // #10: replaced inline ?? object with defaultLiveState()
      return {
        ...state,
        liveBuffers: { ...state.liveBuffers, [threadId]: defaultLiveState() },
      };
    }

    default:
      return null;
  }
}
