/**
 * applyRunCachePatch — Pure functions that patch the TanStack Query cache with SSE events.
 *
 * #35: Extracted cache patch logic from the onMessage switch in the hook.
 * #41: Changed the inline re-cast of token events to the canonical TokenEvent type.
 * BatchC: Introduced discriminated-union narrowing and removed event as XxxEvent casts.
 *         Annotated setQueryData prev type with generated RunState / TasksFile / Message[].
 */

import type { QueryClient } from "@tanstack/react-query";
import type {
  ActiveWorker,
  Epic,
  Message,
  RunEvent,
  RunState,
  Task,
  TasksFile,
} from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import type { RunActivityAction } from "./actions";

/**
 * Converts a RunEvent to a RunActivityAction.
 * #35: Extracted the reducer dispatch switch from onMessage in the hook.
 * #41: Cast token events to the canonical TokenEvent type and removed redundant typeof guards.
 * BatchC: Removed event as XxxEvent casts via discriminated-union narrowing.
 *
 * Returns null for events that cannot be converted.
 */
export function toRunActivityAction(event: RunEvent): RunActivityAction | null {
  switch (event.type) {
    case "run_preparing":
      return { type: "RUN_PREPARING" };
    case "run_started":
      return { type: "RUN_STARTED" };
    case "run_completed":
      return { type: "RUN_COMPLETED" };
    case "run_failed":
      // narrowing: event is RunFailedEvent — error is required(string)
      return { type: "RUN_FAILED", error: event.error ?? undefined };
    case "run_stopped":
      return { type: "RUN_STOPPED" };
    case "run_paused":
      return { type: "RUN_PAUSED" };
    case "run_resumed":
      return { type: "RUN_RESUMED" };
    case "pause_effective":
      return { type: "PAUSE_EFFECTIVE" };
    case "manager_turn_started":
      // narrowing: event is ManagerTurnStartedEvent
      return { type: "MANAGER_TURN_STARTED", event };
    case "manager_message":
      // narrowing: event is ManagerMessageEvent — no cast needed
      return { type: "MANAGER_MESSAGE", event };
    case "delegation":
      return { type: "DELEGATION", event };
    case "worker_started":
      // narrowing: event is WorkerStartedEvent
      return { type: "WORKER_STARTED", event };
    case "worker_completed":
      // narrowing: event is WorkerCompletedEvent
      return { type: "WORKER_COMPLETED", event };
    case "evaluator_started":
      return { type: "EVALUATOR_STARTED", event };
    case "eval_result":
      return { type: "EVAL_RESULT", event };
    case "token": {
      // narrowing: event is TokenEvent
      // thread_id is required(string), but skip when the value is an empty string
      if (!event.thread_id) return null;
      return {
        type: "TOKEN",
        threadId: event.thread_id,
        delta: event.delta ?? "",
        msgIndex: event.msg_index ?? 0,
      };
    }
    case "tool_call": {
      // narrowing: event is ToolCallEvent — thread_id is required(string)
      return { type: "TOOL_CALL", threadId: event.thread_id, event };
    }
    case "tool_result": {
      // narrowing: event is ToolResultEvent — thread_id is required(string)
      return { type: "TOOL_RESULT", threadId: event.thread_id, event };
    }
    case "your_turn": {
      // narrowing: event is YourTurnEvent — the run parked in "waiting"
      // (your turn). The event carries no text: the agent's question or
      // report is its final message in the thread.
      return { type: "YOUR_TURN", threadId: event.thread_id };
    }
    case "your_turn_ended": {
      // narrowing: event is YourTurnEndedEvent
      return { type: "YOUR_TURN_ENDED", threadId: event.thread_id };
    }
    case "user_message_committed": {
      // narrowing: event is UserMessageCommittedEvent
      return { type: "USER_MESSAGE_COMMITTED", event };
    }
    case "worker_failed": {
      // narrowing: event is WorkerFailedEvent
      return { type: "WORKER_FAILED", event };
    }
    default:
      return null;
  }
}

// ---- Helpers ----

/** Thin helper that overwrites only the status field of the runState cache */
function patchRunStatus(
  qc: QueryClient,
  projectId: string,
  epicId: string,
  status: RunState["status"],
): void {
  qc.setQueryData<RunState>(queryKeys.runState.get(projectId, epicId), (prev) =>
    prev ? { ...prev, status } : prev,
  );
}

/**
 * applyRunCachePatch — Patches the TanStack Query cache with SSE events.
 * #35: Extracted the cache patch switch from onMessage in the hook.
 * BatchC: Removed casts via discriminated-union narrowing. Annotated prev type with generated types.
 */
export function applyRunCachePatch(
  qc: QueryClient,
  projectId: string,
  epicId: string,
  event: RunEvent,
): void {
  switch (event.type) {
    case "task_update": {
      // narrowing: event is TaskUpdateEvent
      const key = queryKeys.tasks.get(projectId, epicId);
      const prevTasks = qc.getQueryData<TasksFile>(key);
      const cachedTask = (prevTasks?.tasks ?? []).find((t) => t.id === event.task_id);
      if (event.plan_changed || !cachedTask) {
        // The plan SNAPSHOT may have changed — the Manager's task_update tool
        // (plan_changed=true) can touch any plan-defining field (title/repo/
        // depends_on/contract/agent), most of which this event does not carry,
        // and an unknown task id means a new plan item either way. An in-place
        // patch cannot represent that (the backend-computed plan_hash /
        // plan_approved in the GET /tasks response changed with the snapshot),
        // so refetch instead. This is what makes the plan-approval button
        // appear live — with the CURRENT hash — while the manager is still
        // parked in the same run. Status-only dispatch-progress updates
        // (plan_changed=false) keep the cheap in-place patch: status is
        // excluded from the plan hash.
        qc.invalidateQueries({ queryKey: key });
        break;
      }
      qc.setQueryData<TasksFile>(key, (prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          tasks: (prev.tasks ?? []).map(
            (t): Task =>
              t.id === event.task_id
                ? { ...t, status: event.status as Task["status"], title: event.title || t.title }
                : t,
          ),
        };
      });
      break;
    }
    case "run_started":
      qc.invalidateQueries({ queryKey: queryKeys.epics.detail(projectId, epicId) });
      patchRunStatus(qc, projectId, epicId, "running");
      break;
    case "run_completed":
    case "run_failed":
      qc.invalidateQueries({ queryKey: queryKeys.epics.detail(projectId, epicId) });
      qc.invalidateQueries({ queryKey: queryKeys.tasks.get(projectId, epicId) });
      qc.setQueryData<RunState>(queryKeys.runState.get(projectId, epicId), (prev) =>
        prev
          ? {
              ...prev,
              status: event.type === "run_completed" ? "completed" : "error",
              active_workers: [],
            }
          : prev,
      );
      break;
    case "run_stopped":
      // User-initiated stop → run settles back into "waiting" (your turn,
      // re-runnable), not completed/error.
      qc.invalidateQueries({ queryKey: queryKeys.epics.detail(projectId, epicId) });
      qc.invalidateQueries({ queryKey: queryKeys.tasks.get(projectId, epicId) });
      qc.setQueryData<RunState>(queryKeys.runState.get(projectId, epicId), (prev) =>
        prev ? { ...prev, status: "waiting", active_workers: [] } : prev,
      );
      break;
    case "run_paused":
      patchRunStatus(qc, projectId, epicId, "paused");
      qc.invalidateQueries({ queryKey: queryKeys.epics.detail(projectId, epicId) });
      break;
    case "run_resumed":
      patchRunStatus(qc, projectId, epicId, "running");
      qc.invalidateQueries({ queryKey: queryKeys.epics.detail(projectId, epicId) });
      break;
    case "your_turn":
      // narrowing: event is YourTurnEvent — the run parked in "waiting" (the
      // agent's question or report is its final thread message).
      //
      // Patch the run IDENTITY fields too, not just the status: the runState
      // cache is otherwise "mount-time snapshot + status patches", so
      // run_id / thread_id freeze at mount and a later re-dispatch from
      // this cache (dispatchForRunStatus on epic/trial change) would attribute
      // the parked marker to a long-gone run — reviving the old reviewer
      // misattribution. role is unknown here; the coalesced parked-thread
      // sync (use-run-activity) refreshes it from REST, and the parked
      // thread's messages are merged there as well (no invalidation — see
      // use-thread-messages' no-invalidate contract).
      qc.setQueryData<RunState>(queryKeys.runState.get(projectId, epicId), (prev) =>
        prev
          ? {
              ...prev,
              status: "waiting",
              run_id: event.run_id ?? prev.run_id,
              thread_id: event.thread_id || prev.thread_id,
              // A DIFFERENT thread means a different run parked — the previous
              // run's role must not stick to it (e.g. a stale "reviewer" on a
              // manager park). Reset to the default; the coalesced REST
              // refresh right after this event overwrites it with the truth.
              role: event.thread_id && event.thread_id !== prev.thread_id ? "manager" : prev.role,
            }
          : prev,
      );
      break;
    case "your_turn_ended":
      // narrowing: event is YourTurnEndedEvent
      // Symmetric with reducer YOUR_TURN_ENDED: revert to running only when waiting.
      // Guard to prevent erroneously reverting to running when a delayed your_turn_ended
      // arrives after a terminal state (completed/failed/error).
      qc.setQueryData<RunState>(queryKeys.runState.get(projectId, epicId), (prev) =>
        prev && prev.status === "waiting" ? { ...prev, status: "running" } : prev,
      );
      break;
    case "worker_started": {
      // narrowing: event is WorkerStartedEvent
      qc.setQueryData<RunState>(queryKeys.runState.get(projectId, epicId), (prev) => {
        if (!prev) return prev;
        const already = (prev.active_workers ?? []).some((w) => w.worker_id === event.worker_id);
        if (already) return prev;
        const newWorker: ActiveWorker = {
          worker_id: event.worker_id,
          task_id: event.task_id ?? null,
          repo: event.repo ?? null,
        };
        return { ...prev, active_workers: [...(prev.active_workers ?? []), newWorker] };
      });
      qc.invalidateQueries({ queryKey: queryKeys.threads.list(projectId, epicId) });
      break;
    }
    case "worker_completed": {
      // narrowing: event is WorkerCompletedEvent
      qc.setQueryData<RunState>(queryKeys.runState.get(projectId, epicId), (prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          active_workers: (prev.active_workers ?? []).filter(
            (w) => w.worker_id !== event.worker_id,
          ),
        };
      });
      qc.invalidateQueries({ queryKey: queryKeys.threads.list(projectId, epicId) });
      qc.invalidateQueries({
        queryKey: queryKeys.threads.messages(projectId, epicId, event.worker_id),
      });
      break;
    }
    case "worker_failed": {
      // narrowing: event is WorkerFailedEvent
      qc.setQueryData<RunState>(queryKeys.runState.get(projectId, epicId), (prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          active_workers: (prev.active_workers ?? []).filter(
            (w) => w.worker_id !== event.worker_id,
          ),
        };
      });
      qc.invalidateQueries({ queryKey: queryKeys.threads.list(projectId, epicId) });
      qc.invalidateQueries({
        queryKey: queryKeys.threads.messages(projectId, epicId, event.worker_id),
      });
      break;
    }
    case "manager_message": {
      // narrowing: event is ManagerMessageEvent
      if (event.thread_id) {
        qc.invalidateQueries({
          queryKey: queryKeys.threads.messages(projectId, epicId, event.thread_id),
        });
      }
      break;
    }
    case "eval_result": {
      // narrowing: event is EvalResultEvent
      qc.invalidateQueries({ queryKey: queryKeys.threads.list(projectId, epicId) });
      // On completion, the evaluator's live buffer is cleared (reducer EVAL_RESULT), so
      // refetch persistent messages in the same way as manager_message / worker_completed
      // to prevent the turn immediately after completion from disappearing from the thread.
      if (event.eval_id) {
        qc.invalidateQueries({
          queryKey: queryKeys.threads.messages(projectId, epicId, event.eval_id),
        });
      }
      break;
    }
    case "diff_update":
      // #18: aggregate with umbrella key queryKeys.git.all()
      qc.invalidateQueries({ queryKey: queryKeys.git.all() });
      break;
    case "epic_merged": {
      // narrowing: event is EpicMergedEvent
      // Merge fact recorded (attribute, not a status): patch merged_at into the
      // epic detail/list caches so the "merged" badge appears without a refetch.
      // The epic stays open — no status change is involved.
      qc.setQueryData<Epic>(queryKeys.epics.detail(projectId, epicId), (prev) =>
        prev ? { ...prev, merged_at: event.merged_at } : prev,
      );
      qc.setQueryData<Epic[]>(queryKeys.epics.list(projectId), (prev) =>
        prev ? prev.map((e) => (e.id === epicId ? { ...e, merged_at: event.merged_at } : e)) : prev,
      );
      break;
    }
    case "user_message_committed": {
      // narrowing: event is UserMessageCommittedEvent
      // PR-C: immediate visibility of injected utterances.
      // Optimistically append to the thread messages cache when the SSE arrives.
      // Dedup by message_id so that:
      //   - Duplicate insertion during reconnection backfill replay is prevented.
      //   - Even when the cache is overwritten after REST confirmation (invalidate→refetch),
      //     no double bubble appears since the message_id matches the server-side value.
      const msgKey = queryKeys.threads.messages(projectId, epicId, event.thread_id);
      qc.setQueryData<Message[]>(msgKey, (prev) => {
        if (!prev) return prev;
        // dedup: skip if the same message_id already exists
        if (prev.some((m) => m.message_id === event.message_id)) return prev;
        const newMsg: Message = {
          message_id: event.message_id,
          created_at: event.ts,
          message: {
            role: "user",
            content: [{ text: event.text }],
          },
        };
        return [...prev, newMsg];
      });
      break;
    }
    default:
      break;
  }
}
