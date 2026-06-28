/**
 * run-activity selectors
 *
 * #36: defaultLiveState() factory + single selectThreadLiveState
 */

import { emptyStreamState } from "@/lib/assistant-ui/runtime";
import type { RunActivityState, ThreadLiveState } from "./types";

/**
 * Factory that returns an empty ThreadLiveState.
 * #10: Exported for reuse across the 3 fallbacks in live-buffer.ts and getLiveState in use-run-activity.ts.
 * Placed in selectors.ts to avoid circular dependency (live-buffer.ts does not import selectors.ts).
 */
export function defaultLiveState(): ThreadLiveState {
  return { streamState: emptyStreamState(), isRunning: false };
}

/**
 * Thin selector that retrieves the live state for a specific thread from the useRunActivity store.
 * Replaces use-thread-stream.
 */
export function selectThreadLiveState(
  activityState: RunActivityState,
  threadId: string,
): ThreadLiveState {
  return activityState.liveBuffers[threadId] ?? defaultLiveState();
}

/**
 * Returns whether a node is "active" (isStreaming or in running/thinking status).
 * Used for typing indicator display. Returns true from WORKER_STARTED/MANAGER_TURN_STARTED even before the first token.
 */
export function isAgentActive(activityState: RunActivityState, threadId: string): boolean {
  const tree = activityState.treeState;
  if (tree.manager?.threadId === threadId) {
    return tree.manager.isStreaming || tree.manager.status === "thinking";
  }
  const worker = tree.workers[threadId];
  if (worker) {
    return worker.isStreaming || worker.status === "running";
  }
  const evaluator = tree.evaluators[threadId];
  if (evaluator) {
    return evaluator.isStreaming || evaluator.status === "evaluating";
  }
  return false;
}
