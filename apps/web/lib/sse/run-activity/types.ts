/**
 * Public type definitions for run-activity.
 * Aggregates node state types and store state types.
 */

import type { StreamState } from "@/lib/assistant-ui/runtime";

// ---- Node state types ----

export type ManagerNodeStatus =
  | "idle"
  | "thinking" // manager_turn_started + token
  | "delegating" // delegation event — waiting for workers
  | "completed";

export type WorkerNodeStatus =
  | "pending" // delegation event — waiting to start
  | "running" // worker_started + token
  | "completed"
  | "failed";

export type EvaluatorNodeStatus =
  | "evaluating" // evaluator_started
  | "accepted"
  | "rejected";

export interface ManagerNodeState {
  role: "manager";
  threadId: string;
  status: ManagerNodeStatus;
  isStreaming: boolean;
  lastMessage?: string;
}

export interface WorkerNodeState {
  role: "worker";
  threadId: string;
  taskId: string | null;
  repo: string | null;
  taskTitle: string | null;
  status: WorkerNodeStatus;
  isStreaming: boolean;
  /**
   * thread_id of the manager trial this worker belongs to (its parent in the
   * agent tree). Used to scope the agent-state tree to the active trial so that
   * workers/evaluators of an archived (or otherwise inactive) trial do not
   * linger. `null` = parent not yet known (just added live; kept while an
   * active trial exists, cleared when there is no active trial).
   */
  parentManagerId: string | null;
}

export interface EvaluatorNodeState {
  role: "evaluator";
  threadId: string;
  workerId: string;
  taskId: string;
  repo: string;
  evalId: string;
  status: EvaluatorNodeStatus;
  isStreaming: boolean;
}

// #39: Removed unused ThreadNodeState union

export interface ThreadTreeState {
  manager: ManagerNodeState | null;
  workers: Record<string, WorkerNodeState>;
  evaluators: Record<string, EvaluatorNodeState>;
  /** taskId → workerId mapping (built from delegation events) */
  taskToWorker: Record<string, string>;
}

// ---- Store state types ----

/**
 * Frontend run status (lifecycle redesign P3).
 *
 * - "waiting" is the single resting state = "your turn". A conversation run
 *   parks here after every turn; an epic that has never run is also waiting.
 * - "completed" is JOB runs only (resolve / arbiter) — conversation runs never
 *   end (principle 2), so run_completed is never emitted for them.
 * - "preparing" is a frontend-synthesised phase (index refresh before start).
 */
export type RunActivityStatus =
  | "preparing"
  | "running"
  | "paused"
  | "waiting"
  | "completed"
  | "error";

/** Live buffer state per thread */
export interface ThreadLiveState {
  streamState: StreamState;
  /** At least one token has arrived, or the node is isStreaming */
  isRunning: boolean;
}

export interface RunActivityState {
  /** Overall run status */
  runStatus: RunActivityStatus;
  /** Flag indicating a pending pause is awaiting application */
  pausePending: boolean;
  /** Error message from run_failed. Set only on failure. */
  runError: string | null;
  /**
   * Parked-conversation marker: the run has actually parked in "waiting"
   * (your turn) on this thread. null = no parked conversation (never ran, or
   * a turn is executing). The question itself is NOT carried here — it is the
   * agent's final message in the thread (ask_user was removed in P3).
   */
  awaitingInput: { threadId: string } | null;
  /** Agent tree */
  treeState: ThreadTreeState;
  /** Live buffers per thread. key=threadId */
  liveBuffers: Record<string, ThreadLiveState>;
  /**
   * thread_id of the active manager thread.
   * Set when restored from REST via RunState.manager_thread.
   * null = not yet confirmed (operates with "manager" fallback).
   */
  managerThreadId: string | null;
}
