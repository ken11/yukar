/**
 * RunActivityAction — union type of actions dispatched to the reducer
 */

import type {
  DelegationEvent,
  EvalResultEvent,
  EvaluatorStartedEvent,
  ManagerMessageEvent,
  ManagerTurnStartedEvent,
  ThreadEntry,
  ToolCallEvent,
  ToolResultEvent,
  UserMessageCommittedEvent,
  WorkerCompletedEvent,
  WorkerFailedEvent,
  WorkerStartedEvent,
} from "@/lib/api/endpoints";

export type RunActivityAction =
  // Initialization
  | { type: "INIT"; threads: ThreadEntry[] }
  | { type: "RESET" }
  // Run lifecycle
  | { type: "RUN_PREPARING" }
  | { type: "RUN_STARTED" }
  // RUN_COMPLETED is emitted by JOB runs only (resolve / arbiter) — a
  // conversation run never completes (it parks via USER_INPUT_REQUESTED).
  | { type: "RUN_COMPLETED" }
  | { type: "RUN_FAILED"; error?: string }
  | { type: "RUN_STOPPED" }
  | { type: "RUN_PAUSED" }
  | { type: "RUN_RESUMED" }
  | { type: "PAUSE_EFFECTIVE" }
  | { type: "SET_PAUSE_PENDING"; value: boolean }
  // Manager
  | { type: "MANAGER_TURN_STARTED"; event: ManagerTurnStartedEvent }
  | { type: "MANAGER_MESSAGE"; event: ManagerMessageEvent }
  // Worker / Evaluator
  | { type: "DELEGATION"; event: DelegationEvent }
  | { type: "WORKER_STARTED"; event: WorkerStartedEvent }
  | { type: "WORKER_COMPLETED"; event: WorkerCompletedEvent }
  | { type: "EVALUATOR_STARTED"; event: EvaluatorStartedEvent }
  | { type: "EVAL_RESULT"; event: EvalResultEvent }
  // Live buffer
  | { type: "TOKEN"; threadId: string; delta: string; msgIndex?: number }
  | { type: "TOOL_CALL"; threadId: string; event: ToolCallEvent }
  | { type: "TOOL_RESULT"; threadId: string; event: ToolResultEvent }
  // Worker failure
  | { type: "WORKER_FAILED"; event: WorkerFailedEvent }
  // Your-turn signals (P3): REQUESTED = the run parked in "waiting",
  // RESOLVED = the user's reply woke it. No question payload — the question is
  // the agent's final message in the thread.
  | { type: "USER_INPUT_REQUESTED"; threadId: string }
  | { type: "USER_INPUT_RESOLVED"; threadId: string }
  // Clears the live buffer for the specified thread when REST authoritative data arrives, preventing double rendering (Bug4)
  | { type: "CLEAR_LIVE_BUFFER"; threadId: string }
  // Immediate visibility of injected utterances (PR-C)
  | { type: "USER_MESSAGE_COMMITTED"; event: UserMessageCommittedEvent }
  // Sets the active manager trial id (from epic.active_thread_id) — composer
  // rights + tree scoping + links. P4 split: never sourced from
  // RunState.manager_thread (that is the run's own thread, see SET_CURRENT_RUN).
  | { type: "SET_ACTIVE_TRIAL_ID"; threadId: string | null }
  // Sets the conversation run the epic's state refers to (REST RunState
  // manager_thread + role) — "your turn" attribution and role wording.
  | { type: "SET_CURRENT_RUN"; threadId: string; role: "manager" | "reviewer" | null };
