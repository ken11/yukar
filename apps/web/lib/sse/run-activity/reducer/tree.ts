/**
 * Agent tree transition handler + helpers.
 *
 * Target cases: INIT / MANAGER_TURN_STARTED / MANAGER_MESSAGE /
 *               DELEGATION / WORKER_STARTED / WORKER_COMPLETED /
 *               EVALUATOR_STARTED / EVAL_RESULT
 *
 * Exported helpers:
 *   applyTreeInit   — non-destructive reconciliation for INIT actions
 *   finalizeTree    — common finalize for RUN_COMPLETED / RUN_FAILED / RUN_STOPPED (#34)
 */

import type { ThreadEntry } from "@/lib/api/endpoints";
import { clearedStreamState, emptyStreamState } from "@/lib/assistant-ui/runtime";
import type { RunActivityAction } from "../actions";
import type {
  EvaluatorNodeState,
  EvaluatorNodeStatus,
  ManagerNodeState,
  ManagerNodeStatus,
  RunActivityState,
  ThreadTreeState,
  WorkerNodeState,
  WorkerNodeStatus,
} from "../types";

// ---- Tree helpers ----

/**
 * Scope the worker/evaluator tree to a single manager trial.
 *
 * The agent-state tree shows the agents of the ACTIVE trial. Workers carry the
 * id of the manager trial they belong to (`parentManagerId`); evaluators link
 * to their worker via `workerId`. This drops everything that does not belong to
 * `activeManagerId` so an archived (or otherwise inactive) trial's
 * workers/evaluators stop lingering.
 *
 * Rules:
 *   - `activeManagerId === null` → no active trial → empty workers/evaluators
 *     (nothing to scope to; any lingering nodes are cleared).
 *   - Otherwise a worker is kept iff its `parentManagerId` matches the active
 *     trial, or is `null` (just added live under the active trial, parent not
 *     yet resolved — kept optimistically).
 *   - An evaluator is kept iff its parent worker survived, OR it is a DIRECT
 *     evaluator (P6 evaluator-only dispatch: no worker ran, so its live
 *     events carry worker_id "" and its ThreadEntry is parented to the
 *     manager trial itself).
 */
export function scopeTreeToManager(
  tree: ThreadTreeState,
  activeManagerId: string | null,
): ThreadTreeState {
  const workers: Record<string, WorkerNodeState> = {};
  for (const [id, w] of Object.entries(tree.workers)) {
    if (activeManagerId === null) continue; // no active trial → drop all
    if (w.parentManagerId == null || w.parentManagerId === activeManagerId) {
      workers[id] = w;
    }
  }
  const evaluators: Record<string, EvaluatorNodeState> = {};
  for (const [id, e] of Object.entries(tree.evaluators)) {
    if (activeManagerId === null) continue; // no active trial → drop all
    const isDirect = e.workerId === "" || e.workerId === activeManagerId;
    if (isDirect || workers[e.workerId]) evaluators[id] = e;
  }
  const taskToWorker: Record<string, string> = {};
  for (const [task, wid] of Object.entries(tree.taskToWorker)) {
    if (workers[wid]) taskToWorker[task] = wid;
  }
  return { manager: tree.manager, workers, evaluators, taskToWorker };
}

/**
 * Non-destructive reconciliation: preserves existing nodes while adding unregistered threads,
 * then scopes the worker/evaluator tree to the active trial.
 * Called from the INIT action.
 *
 * @param activeManagerId Authoritative active trial id (state.activeTrialId,
 *   sourced from epic.active_thread_id). When null, the active trial is derived
 *   from the first non-archived manager in `threads`; if there is none either,
 *   the tree has no active trial and worker/evaluator nodes are cleared.
 */
export function applyTreeInit(
  tree: ThreadTreeState,
  threads: ThreadEntry[],
  activeManagerId: string | null = null,
): ThreadTreeState {
  let manager: ManagerNodeState | null = tree.manager;
  const workers: Record<string, WorkerNodeState> = { ...tree.workers };
  const evaluators: Record<string, EvaluatorNodeState> = { ...tree.evaluators };
  const taskToWorker: Record<string, string> = { ...tree.taskToWorker };

  // Track the first non-archived manager so it can serve as the scoping anchor
  // when the authoritative active id is not yet known (activeTrialId null).
  let resolvedManagerId: string | null = null;

  for (const t of threads) {
    if (t.role === "manager") {
      // Exclude archived old trials from manager node resolution.
      // Fixes the enumeration-order bug that depended on threads.yaml ordering (implicit assumption: last = newest).
      // By targeting only non-archived managers, the id is not overwritten with an old trial id after re-sorting.
      if (t.status === "archived") continue;
      if (resolvedManagerId === null) resolvedManagerId = t.id;
      if (!manager) {
        manager = {
          role: "manager",
          threadId: t.id,
          status: t.status === "active" ? "idle" : "completed",
          isStreaming: false,
          lastMessage: undefined,
        };
      } else {
        // Update threadId with the real id even when an existing node is present.
        // With multi-trial support, the manager id in threads changes when active_thread_id switches,
        // so sync only the id while preserving the live status/streaming/lastMessage.
        // Since archived is already excluded, this always syncs to the id of a "live trial".
        manager = { ...manager, threadId: t.id };
      }
    } else if (t.role === "worker") {
      const workerStatus: WorkerNodeStatus =
        t.status === "active" ? "running" : t.status === "failed" ? "failed" : "completed";
      if (!workers[t.id]) {
        const pendingId = t.task ? `pending-${t.task}` : null;
        if (pendingId && workers[pendingId]) {
          delete workers[pendingId];
        }
        workers[t.id] = {
          role: "worker",
          threadId: t.id,
          taskId: t.task ?? null,
          repo: t.repo ?? null,
          taskTitle: t.title ?? null,
          status: workerStatus,
          isStreaming: t.status === "active",
          // parent_thread_id = the manager trial this worker was dispatched by.
          parentManagerId: t.parent_thread_id ?? null,
        };
        if (t.task) {
          taskToWorker[t.task] = t.id;
        }
      }
    } else if (t.role === "evaluator") {
      const evalStatus: EvaluatorNodeStatus =
        t.status === "active" ? "evaluating" : t.status === "failed" ? "rejected" : "accepted";
      const parentWorkerId = t.parent_thread_id ?? "";
      if (!evaluators[t.id]) {
        evaluators[t.id] = {
          role: "evaluator",
          threadId: t.id,
          workerId: parentWorkerId,
          taskId: t.task ?? "",
          repo: t.repo ?? "",
          evalId: t.id,
          status: evalStatus,
          isStreaming: t.status === "active",
        };
      }
    }
  }

  // Scope to the authoritative active trial; fall back to the first non-archived
  // manager when the authoritative id is unknown. If neither exists, there is no
  // active trial and worker/evaluator nodes are cleared.
  const filterId = activeManagerId ?? resolvedManagerId;
  return scopeTreeToManager({ manager, workers, evaluators, taskToWorker }, filterId);
}

/**
 * #34: Unified the byte-identical tree finalize for RUN_COMPLETED / RUN_FAILED / RUN_STOPPED.
 */
export function finalizeTree(tree: ThreadTreeState): Pick<ThreadTreeState, "manager" | "workers"> {
  const manager = tree.manager
    ? { ...tree.manager, status: "completed" as ManagerNodeStatus, isStreaming: false }
    : null;
  const workers = Object.fromEntries(
    Object.entries(tree.workers).map(([id, w]) => [
      id,
      w.status === "running"
        ? { ...w, status: "completed" as WorkerNodeStatus, isStreaming: false }
        : w,
    ]),
  );
  return { manager, workers };
}

// ---- Tree transition handler ----

export function handleTree(
  state: RunActivityState,
  action: RunActivityAction,
): RunActivityState | null {
  switch (action.type) {
    case "INIT": {
      // Pass the authoritative active trial id so the tree is scoped to it
      // (archived/inactive trials' workers + evaluators are pruned).
      const treeState = applyTreeInit(state.treeState, action.threads, state.activeTrialId);
      return { ...state, treeState };
    }

    // #38: Removed dead thread_id cast branch. ManagerTurnStartedEvent has no thread_id,
    // so use tree.manager.threadId directly.
    // Clear yourTurn because the Manager has started working again.
    // Turn start = start of a new stream, so reset streamState to emptyStreamState().
    // ensureLiveBuffer preserves done=true for existing keys, so it is not used (#multi-turn-regression).
    case "MANAGER_TURN_STARTED": {
      const tree = state.treeState;
      if (!tree.manager) return state;
      const threadId = tree.manager.threadId;
      return {
        ...state,
        runStatus: state.runStatus === "waiting" ? "running" : state.runStatus,
        yourTurn: null,
        treeState: {
          ...tree,
          manager: { ...tree.manager, status: "thinking", isStreaming: true },
        },
        liveBuffers: {
          ...state.liveBuffers,
          [threadId]: { streamState: emptyStreamState(), isRunning: true },
        },
      };
    }

    case "MANAGER_MESSAGE": {
      const tree = state.treeState;
      if (!tree.manager) return state;
      const threadId = action.event.thread_id ?? tree.manager.threadId;
      // Turn completed: clear the live buffer to prevent double rendering now that REST returns all messages.
      // Setting done=true activates the "CLEAR_LIVE_BUFFER after REST authoritative data arrives" guard in
      // thread-page-client, structurally preventing double rendering of confirmed messages and stream bubbles (#fix3).
      return {
        ...state,
        treeState: {
          ...tree,
          manager: {
            ...tree.manager,
            status: "idle",
            isStreaming: false,
            lastMessage: action.event.text,
          },
        },
        liveBuffers: {
          ...state.liveBuffers,
          [threadId]: { streamState: clearedStreamState(), isRunning: false },
        },
      };
    }

    case "DELEGATION": {
      const tree = state.treeState;
      if (!tree.manager) return state;
      const newWorkers = { ...tree.workers };
      const newTaskToWorker = { ...tree.taskToWorker };

      for (const item of action.event.items) {
        const existingWorkerId = newTaskToWorker[item.task_id];
        if (!existingWorkerId) {
          const pendingId = `pending-${item.task_id}`;
          newWorkers[pendingId] = {
            role: "worker",
            threadId: pendingId,
            taskId: item.task_id,
            repo: item.repo ?? null,
            taskTitle: item.title ?? null,
            status: "pending",
            isStreaming: false,
            // Delegation only happens for the active trial.
            parentManagerId: state.activeTrialId,
          };
          newTaskToWorker[item.task_id] = pendingId;
        }
      }

      return {
        ...state,
        treeState: {
          ...tree,
          manager: { ...tree.manager, status: "delegating", isStreaming: false },
          workers: newWorkers,
          taskToWorker: newTaskToWorker,
        },
      };
    }

    case "WORKER_STARTED": {
      const ev = action.event;
      const tree = state.treeState;
      const taskId = ev.task_id ?? null;
      const existingPendingId = taskId ? tree.taskToWorker[taskId] : null;
      const newWorkers = { ...tree.workers };
      const newTaskToWorker = { ...tree.taskToWorker };

      const existingWorker = tree.workers[ev.worker_id];
      if (existingWorker?.status === "completed" || existingWorker?.status === "failed") {
        return state;
      }

      const prevPending = existingPendingId ? newWorkers[existingPendingId] : undefined;

      if (existingPendingId?.startsWith("pending-")) {
        delete newWorkers[existingPendingId];
      }

      newWorkers[ev.worker_id] = {
        role: "worker",
        threadId: ev.worker_id,
        taskId,
        repo: ev.repo ?? prevPending?.repo ?? null,
        taskTitle: prevPending?.taskTitle ?? null,
        status: "running",
        isStreaming: true,
        // A started worker belongs to the active trial.
        parentManagerId: state.activeTrialId ?? prevPending?.parentManagerId ?? null,
      };

      if (taskId) {
        newTaskToWorker[taskId] = ev.worker_id;
      }

      // Turn start = start of a new stream, so reset streamState to emptyStreamState().
      // ensureLiveBuffer preserves done=true for existing keys, so it is not used (#multi-turn-regression).
      return {
        ...state,
        treeState: {
          ...tree,
          workers: newWorkers,
          taskToWorker: newTaskToWorker,
          manager: tree.manager
            ? {
                ...tree.manager,
                status: tree.manager.status === "delegating" ? "idle" : tree.manager.status,
              }
            : null,
        },
        liveBuffers: {
          ...state.liveBuffers,
          [ev.worker_id]: { streamState: emptyStreamState(), isRunning: true },
        },
      };
    }

    case "WORKER_COMPLETED": {
      const ev = action.event;
      const tree = state.treeState;
      const existing = tree.workers[ev.worker_id];
      if (!existing) return state;
      // Turn completed: set done=true to activate the CLEAR_LIVE_BUFFER guard after REST authoritative data arrives (#fix3)
      return {
        ...state,
        treeState: {
          ...tree,
          workers: {
            ...tree.workers,
            [ev.worker_id]: { ...existing, status: "completed", isStreaming: false },
          },
        },
        liveBuffers: {
          ...state.liveBuffers,
          [ev.worker_id]: { streamState: clearedStreamState(), isRunning: false },
        },
      };
    }

    case "WORKER_FAILED": {
      const ev = action.event;
      const tree = state.treeState;
      const existing = tree.workers[ev.worker_id];
      if (!existing) return state;
      return {
        ...state,
        treeState: {
          ...tree,
          workers: {
            ...tree.workers,
            [ev.worker_id]: { ...existing, status: "failed", isStreaming: false },
          },
        },
        liveBuffers: {
          ...state.liveBuffers,
          [ev.worker_id]: { streamState: clearedStreamState(), isRunning: false },
        },
      };
    }

    case "EVALUATOR_STARTED": {
      const ev = action.event;
      const tree = state.treeState;
      // Turn start = start of a new stream, so reset streamState to emptyStreamState().
      // ensureLiveBuffer preserves done=true for existing keys, so it is not used (#multi-turn-regression).
      return {
        ...state,
        treeState: {
          ...tree,
          evaluators: {
            ...tree.evaluators,
            [ev.eval_id]: {
              role: "evaluator",
              threadId: ev.eval_id,
              workerId: ev.worker_id,
              taskId: ev.task_id,
              repo: ev.repo,
              evalId: ev.eval_id,
              status: "evaluating",
              isStreaming: true,
            },
          },
        },
        liveBuffers: {
          ...state.liveBuffers,
          [ev.eval_id]: { streamState: emptyStreamState(), isRunning: true },
        },
      };
    }

    case "EVAL_RESULT": {
      const ev = action.event;
      const tree = state.treeState;
      const existing = tree.evaluators[ev.eval_id];
      if (!existing) return state;
      // Turn completed: set done=true to activate the CLEAR_LIVE_BUFFER guard after REST authoritative data arrives (#fix3)
      return {
        ...state,
        treeState: {
          ...tree,
          evaluators: {
            ...tree.evaluators,
            [ev.eval_id]: {
              ...existing,
              status: ev.accepted ? "accepted" : "rejected",
              isStreaming: false,
            },
          },
        },
        liveBuffers: {
          ...state.liveBuffers,
          [ev.eval_id]: { streamState: clearedStreamState(), isRunning: false },
        },
      };
    }

    default:
      return null;
  }
}
