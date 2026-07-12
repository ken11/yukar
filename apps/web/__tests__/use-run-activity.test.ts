/**
 * useRunActivity reducer tests (integrated)
 *
 * Coverage migrated from old hooks:
 *   - use-run-events.test.ts     : SSE cache patch (task_update)
 *   - use-run-events-m5.test.ts  : run_paused/resumed, worker_started/completed active_workers
 *   - use-thread-stream-mj5.test.ts : worker_completed per-thread scope
 *   - use-thread-tree.test.ts    : treeReducer full state machine (Mj3/Mj4/Mj6/Mn1/Mn2)
 *
 * Tests from individual hooks have been migrated to runActivityReducer / useRunActivity hook,
 * maintaining coverage without loss.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  DelegationEvent,
  EvalResultEvent,
  EvaluatorStartedEvent,
  ManagerMessageEvent,
  ManagerTurnStartedEvent,
  Message,
  RunState,
  RunStoppedEvent,
  ThreadEntry,
  UserMessageCommittedEvent,
  WorkerCompletedEvent,
  WorkerStartedEvent,
  YourTurnEndedEvent,
  YourTurnEvent,
} from "../lib/api/endpoints";
import { queryKeys } from "../lib/api/query-keys";
import { streamStateIsEmpty, streamStateTextLength } from "../lib/assistant-ui/runtime";
import { applyRunCachePatch, toRunActivityAction } from "../lib/sse/run-activity/cache-patch";
import {
  isAgentActive,
  type RunActivityAction,
  type RunActivityState,
  runActivityReducer,
  selectThreadLiveState,
  useRunActivity,
} from "../lib/sse/use-run-activity";

// ============================================================
// EventSource mock (for SSE hook tests)
// ============================================================

class MockEventSource {
  url: string;
  onerror: ((ev: Event) => void) | null = null;
  private listeners: Map<string, EventListener[]> = new Map();
  static instances: MockEventSource[] = [];

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, handler: EventListener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type)?.push(handler);
  }

  removeEventListener() {}
  close() {}

  emit(type: string, data: string) {
    const ev = { type, data } as MessageEvent;
    const handlers = this.listeners.get(type) ?? [];
    for (const h of handlers) h(ev);
  }
}

// ============================================================
// Common helpers
// ============================================================

const BASE_EVENT = { project_id: "proj1", epic_id: "epic1", run_id: "run-1" };

function makeThreadEntry(
  overrides: Partial<ThreadEntry> & { id: string; role: ThreadEntry["role"] },
): ThreadEntry {
  return {
    id: overrides.id,
    title: overrides.title ?? overrides.id,
    role: overrides.role,
    status: overrides.status ?? "active",
    task: overrides.task ?? null,
    repo: overrides.repo ?? null,
    parent_thread_id: overrides.parent_thread_id ?? null,
    created_at: overrides.created_at ?? undefined,
  };
}

function applyActions(actions: RunActivityAction[]): RunActivityState {
  const initialState: RunActivityState = {
    runStatus: "waiting",
    pausePending: false,
    runError: null,
    yourTurn: null,
    treeState: { manager: null, workers: {}, evaluators: {}, taskToWorker: {} },
    liveBuffers: {},
    activeTrialId: null,
    currentRun: null,
  };
  return actions.reduce(runActivityReducer, initialState);
}

function makeRunState(overrides: Partial<RunState> = {}): RunState {
  return {
    run_id: "run-1",
    status: "running",
    role: "manager",
    active_workers: [],
    ...overrides,
  };
}

// ============================================================
// Setup for SSE hook tests
// ============================================================

const PROJECT_ID = "proj1";
const EPIC_ID = "epic1";

let qc: QueryClient;

beforeEach(() => {
  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource);
  // stub so getRunState is not actually fetched
  vi.stubGlobal("fetch", () => Promise.resolve({ ok: false, json: () => Promise.reject() }));
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  qc.clear();
});

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(QueryClientProvider, { client: qc }, children);
}

function seedRunState(state: RunState) {
  qc.setQueryData(queryKeys.runState.get(PROJECT_ID, EPIC_ID), state);
}

function getRunStateCache(): RunState | undefined {
  return qc.getQueryData<RunState>(queryKeys.runState.get(PROJECT_ID, EPIC_ID));
}

// ============================================================
// 1. TOKEN incremental update tests
// ============================================================

describe("TOKEN — incremental update of the live buffer", () => {
  it("feeding multiple tokens sequentially causes tokenBuffer to grow incrementally", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    let state = applyActions([{ type: "INIT", threads }, { type: "RUN_STARTED" }]);

    state = runActivityReducer(state, { type: "TOKEN", threadId: "mgr", delta: "Hello" });
    expect(selectThreadLiveState(state, "mgr").streamState.segments[0].tokenBuffer).toBe("Hello");

    state = runActivityReducer(state, { type: "TOKEN", threadId: "mgr", delta: " World" });
    expect(selectThreadLiveState(state, "mgr").streamState.segments[0].tokenBuffer).toBe(
      "Hello World",
    );

    state = runActivityReducer(state, { type: "TOKEN", threadId: "mgr", delta: "!" });
    expect(selectThreadLiveState(state, "mgr").streamState.segments[0].tokenBuffer).toBe(
      "Hello World!",
    );
  });

  it("worker token is accumulated incrementally", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    const startEv: WorkerStartedEvent = {
      ...BASE_EVENT,
      type: "worker_started",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    let state = applyActions([
      { type: "INIT", threads },
      { type: "WORKER_STARTED", event: startEv },
    ]);

    state = runActivityReducer(state, { type: "TOKEN", threadId: "w1", delta: "chunk1" });
    state = runActivityReducer(state, { type: "TOKEN", threadId: "w1", delta: "chunk2" });
    expect(selectThreadLiveState(state, "w1").streamState.segments[0].tokenBuffer).toBe(
      "chunk1chunk2",
    );
  });

  it("tokens for different thread_ids are accumulated independently of each other", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    const startEv: WorkerStartedEvent = {
      ...BASE_EVENT,
      type: "worker_started",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    let state = applyActions([
      { type: "INIT", threads },
      { type: "WORKER_STARTED", event: startEv },
    ]);

    state = runActivityReducer(state, { type: "TOKEN", threadId: "mgr", delta: "manager-text" });
    state = runActivityReducer(state, { type: "TOKEN", threadId: "w1", delta: "worker-text" });

    expect(selectThreadLiveState(state, "mgr").streamState.segments[0].tokenBuffer).toBe(
      "manager-text",
    );
    expect(selectThreadLiveState(state, "w1").streamState.segments[0].tokenBuffer).toBe(
      "worker-text",
    );
  });
});

// ============================================================
// 2. Manager state machine
// ============================================================

describe("Manager — emits immediately on run_started / manager_turn_started", () => {
  it("when a manager thread exists on INIT, a manager node is created", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager", status: "active" })];
    const state = applyActions([{ type: "INIT", threads }]);
    expect(state.treeState.manager).not.toBeNull();
    expect(state.treeState.manager?.threadId).toBe("mgr");
  });

  it("RUN_STARTED alone makes runStatus become running", () => {
    const state = applyActions([{ type: "RUN_STARTED" }]);
    expect(state.runStatus).toBe("running");
  });

  it("MANAGER_TURN_STARTED makes manager become thinking+isStreaming", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    const ev: ManagerTurnStartedEvent = { ...BASE_EVENT, type: "manager_turn_started", turn: 0 };
    const state = applyActions([
      { type: "INIT", threads },
      { type: "RUN_STARTED" },
      { type: "MANAGER_TURN_STARTED", event: ev },
    ]);
    expect(state.treeState.manager?.status).toBe("thinking");
    expect(state.treeState.manager?.isStreaming).toBe(true);
  });

  it("MANAGER_MESSAGE makes manager return to idle and buffer become done", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    const turnEv: ManagerTurnStartedEvent = {
      ...BASE_EVENT,
      type: "manager_turn_started",
      turn: 0,
    };
    const msgEv: ManagerMessageEvent = {
      ...BASE_EVENT,
      type: "manager_message",
      thread_id: "mgr",
      turn: 0,
      text: "I will make a plan",
    };
    let state = applyActions([
      { type: "INIT", threads },
      { type: "RUN_STARTED" },
      { type: "MANAGER_TURN_STARTED", event: turnEv },
    ]);
    state = runActivityReducer(state, { type: "TOKEN", threadId: "mgr", delta: "plan" });
    state = runActivityReducer(state, { type: "MANAGER_MESSAGE", event: msgEv });

    expect(state.treeState.manager?.status).toBe("idle");
    expect(state.treeState.manager?.isStreaming).toBe(false);
    expect(state.treeState.manager?.lastMessage).toBe("I will make a plan");
    // Live buffer is cleared when a turn completes (prevents double rendering: REST returns the full message)
    // Setting done=true allows the CLEAR_LIVE_BUFFER guard in thread-page-client to function (#fix3)
    expect(streamStateTextLength(selectThreadLiveState(state, "mgr").streamState)).toBe(0);
    expect(streamStateIsEmpty(selectThreadLiveState(state, "mgr").streamState)).toBe(true);
    expect(selectThreadLiveState(state, "mgr").streamState.done).toBe(true);
    expect(selectThreadLiveState(state, "mgr").isRunning).toBe(false);
  });
});

// ============================================================
// 3. Worker lifecycle transition tests
// ============================================================

describe("Worker lifecycle — delegation → worker_started → worker_completed", () => {
  it("delegation creates a pending node", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    const delEv: DelegationEvent = {
      ...BASE_EVENT,
      type: "delegation",
      items: [{ task_id: "T1", repo: "repo-a", title: "Task one" }],
    };
    const state = applyActions([
      { type: "INIT", threads },
      { type: "DELEGATION", event: delEv },
    ]);
    expect(state.treeState.workers["pending-T1"]).toBeDefined();
    expect(state.treeState.workers["pending-T1"].status).toBe("pending");
    expect(state.treeState.manager?.status).toBe("delegating");
  });

  it("worker_started replaces the pending node with a real node", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    const delEv: DelegationEvent = {
      ...BASE_EVENT,
      type: "delegation",
      items: [{ task_id: "T1", repo: "repo-a", title: "Task one" }],
    };
    const startEv: WorkerStartedEvent = {
      ...BASE_EVENT,
      type: "worker_started",
      worker_id: "w1",
      task_id: "T1",
      repo: "repo-a",
    };
    const state = applyActions([
      { type: "INIT", threads },
      { type: "DELEGATION", event: delEv },
      { type: "WORKER_STARTED", event: startEv },
    ]);
    expect(state.treeState.workers["pending-T1"]).toBeUndefined();
    expect(state.treeState.workers.w1).toBeDefined();
    expect(state.treeState.workers.w1.status).toBe("running");
    expect(state.treeState.workers.w1.isStreaming).toBe(true);
    expect(state.liveBuffers.w1.isRunning).toBe(true);
  });

  it("worker_completed makes the node completed and the buffer done", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    const startEv: WorkerStartedEvent = {
      ...BASE_EVENT,
      type: "worker_started",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    const completedEv: WorkerCompletedEvent = {
      ...BASE_EVENT,
      type: "worker_completed",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    let state = applyActions([
      { type: "INIT", threads },
      { type: "WORKER_STARTED", event: startEv },
    ]);
    state = runActivityReducer(state, { type: "TOKEN", threadId: "w1", delta: "some output" });
    state = runActivityReducer(state, { type: "WORKER_COMPLETED", event: completedEv });

    expect(state.treeState.workers.w1.status).toBe("completed");
    expect(state.treeState.workers.w1.isStreaming).toBe(false);
    expect(state.liveBuffers.w1.isRunning).toBe(false);
    // Live buffer is cleared when a turn completes (prevents double rendering: REST returns the full message)
    // Setting done=true allows the CLEAR_LIVE_BUFFER guard in thread-page-client to function (#fix3)
    expect(streamStateTextLength(state.liveBuffers.w1.streamState)).toBe(0);
    expect(streamStateIsEmpty(state.liveBuffers.w1.streamState)).toBe(true);
    expect(state.liveBuffers.w1.streamState.done).toBe(true);
  });

  // Mj5 migration: worker_completed of a different worker does not affect other live buffers
  it("Mj5: WORKER_COMPLETED of a different worker does not change other workers' live buffers", () => {
    const startA: WorkerStartedEvent = {
      ...BASE_EVENT,
      type: "worker_started",
      worker_id: "worker-A",
      task_id: "T1",
      repo: "r",
    };
    const startB: WorkerStartedEvent = {
      ...BASE_EVENT,
      type: "worker_started",
      worker_id: "worker-B",
      task_id: "T2",
      repo: "r",
    };
    const completedB: WorkerCompletedEvent = {
      ...BASE_EVENT,
      type: "worker_completed",
      worker_id: "worker-B",
      task_id: "T2",
      repo: "r",
    };
    let state = applyActions([
      { type: "WORKER_STARTED", event: startA },
      { type: "WORKER_STARTED", event: startB },
    ]);
    state = runActivityReducer(state, {
      type: "TOKEN",
      threadId: "worker-A",
      delta: "streaming...",
    });
    const tokenBefore = streamStateTextLength(selectThreadLiveState(state, "worker-A").streamState);
    const isRunningBefore = state.liveBuffers["worker-A"].isRunning;

    // Even when worker-B completes, worker-A's buffer does not change
    state = runActivityReducer(state, { type: "WORKER_COMPLETED", event: completedB });

    expect(streamStateTextLength(selectThreadLiveState(state, "worker-A").streamState)).toBe(
      tokenBefore,
    );
    expect(state.liveBuffers["worker-A"].isRunning).toBe(isRunningBefore);
    // worker-B is in completed state
    expect(state.liveBuffers["worker-B"].isRunning).toBe(false);
  });
});

// ============================================================
// 4. Evaluator lifecycle
// ============================================================

describe("Evaluator lifecycle", () => {
  it("evaluator_started → eval_result(accepted) transition", () => {
    const startEv: EvaluatorStartedEvent = {
      ...BASE_EVENT,
      type: "evaluator_started",
      eval_id: "eval-1",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    const resultEv: EvalResultEvent = {
      ...BASE_EVENT,
      type: "eval_result",
      eval_id: "eval-1",
      worker_id: "w1",
      accepted: true,
      feedback: "LGTM",
    };
    let state = applyActions([{ type: "EVALUATOR_STARTED", event: startEv }]);
    expect(state.treeState.evaluators["eval-1"].status).toBe("evaluating");
    expect(state.liveBuffers["eval-1"].isRunning).toBe(true);

    state = runActivityReducer(state, { type: "EVAL_RESULT", event: resultEv });
    expect(state.treeState.evaluators["eval-1"].status).toBe("accepted");
    expect(state.liveBuffers["eval-1"].isRunning).toBe(false);
    // Live buffer is cleared when a turn completes (prevents double rendering: REST returns the full message)
    // Setting done=true allows the CLEAR_LIVE_BUFFER guard in thread-page-client to function (#fix3)
    expect(streamStateTextLength(state.liveBuffers["eval-1"].streamState)).toBe(0);
    expect(streamStateIsEmpty(state.liveBuffers["eval-1"].streamState)).toBe(true);
    expect(state.liveBuffers["eval-1"].streamState.done).toBe(true);
  });

  it("eval_result rejected → rejected status", () => {
    const startEv: EvaluatorStartedEvent = {
      ...BASE_EVENT,
      type: "evaluator_started",
      eval_id: "eval-1",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    const resultEv: EvalResultEvent = {
      ...BASE_EVENT,
      type: "eval_result",
      eval_id: "eval-1",
      worker_id: "w1",
      accepted: false,
      feedback: "Fix needed",
    };
    let state = applyActions([{ type: "EVALUATOR_STARTED", event: startEv }]);
    state = runActivityReducer(state, { type: "EVAL_RESULT", event: resultEv });
    expect(state.treeState.evaluators["eval-1"].status).toBe("rejected");
  });
});

// ============================================================
// 5. pause / resume state transitions
// ============================================================

describe("pause / resume state transitions", () => {
  it("SET_PAUSE_PENDING makes pausePending become true", () => {
    const state = applyActions([{ type: "SET_PAUSE_PENDING", value: true }]);
    expect(state.pausePending).toBe(true);
  });

  it("PAUSE_EFFECTIVE clears pausePending", () => {
    const state = applyActions([
      { type: "SET_PAUSE_PENDING", value: true },
      { type: "PAUSE_EFFECTIVE" },
    ]);
    expect(state.pausePending).toBe(false);
  });

  it("RUN_RESUMED clears pausePending", () => {
    const state = applyActions([
      { type: "RUN_STARTED" },
      { type: "SET_PAUSE_PENDING", value: true },
      { type: "RUN_PAUSED" },
      { type: "RUN_RESUMED" },
    ]);
    expect(state.runStatus).toBe("running");
    expect(state.pausePending).toBe(false);
  });

  it("RUN_COMPLETED clears pausePending", () => {
    const state = applyActions([
      { type: "RUN_STARTED" },
      { type: "SET_PAUSE_PENDING", value: true },
      { type: "RUN_COMPLETED" },
    ]);
    expect(state.runStatus).toBe("completed");
    expect(state.pausePending).toBe(false);
  });
});

// ============================================================
// 6. isAgentActive: true even before the first token if the agent is running
// ============================================================

describe("isAgentActive — detecting active state before the first token", () => {
  it("after MANAGER_TURN_STARTED, isAgentActive=true even when no token has arrived yet", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    const ev: ManagerTurnStartedEvent = { ...BASE_EVENT, type: "manager_turn_started", turn: 0 };
    const state = applyActions([
      { type: "INIT", threads },
      { type: "RUN_STARTED" },
      { type: "MANAGER_TURN_STARTED", event: ev },
    ]);
    expect(streamStateTextLength(selectThreadLiveState(state, "mgr").streamState)).toBe(0);
    expect(isAgentActive(state, "mgr")).toBe(true);
  });

  it("after WORKER_STARTED, isAgentActive=true even when no token has arrived yet", () => {
    const ev: WorkerStartedEvent = {
      ...BASE_EVENT,
      type: "worker_started",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    const state = applyActions([{ type: "WORKER_STARTED", event: ev }]);
    expect(streamStateTextLength(selectThreadLiveState(state, "w1").streamState)).toBe(0);
    expect(isAgentActive(state, "w1")).toBe(true);
  });

  it("after WORKER_COMPLETED, isAgentActive=false", () => {
    const startEv: WorkerStartedEvent = {
      ...BASE_EVENT,
      type: "worker_started",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    const completedEv: WorkerCompletedEvent = {
      ...BASE_EVENT,
      type: "worker_completed",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    const state = applyActions([
      { type: "WORKER_STARTED", event: startEv },
      { type: "WORKER_COMPLETED", event: completedEv },
    ]);
    expect(isAgentActive(state, "w1")).toBe(false);
  });

  it("returns false for a nonexistent threadId", () => {
    const state = applyActions([]);
    expect(isAgentActive(state, "nonexistent")).toBe(false);
  });
});

// ============================================================
// 7. RUN_COMPLETED lifecycle
// ============================================================

describe("RUN_COMPLETED — all nodes become completed", () => {
  it("running manager + workers become completed and streaming=false", () => {
    const threads = [
      makeThreadEntry({ id: "mgr", role: "manager", status: "active" }),
      makeThreadEntry({ id: "w1", role: "worker", task: "T1", status: "active" }),
    ];
    const state = applyActions([
      { type: "INIT", threads },
      { type: "RUN_STARTED" },
      { type: "RUN_COMPLETED" },
    ]);
    expect(state.runStatus).toBe("completed");
    expect(state.treeState.manager?.status).toBe("completed");
    expect(state.treeState.manager?.isStreaming).toBe(false);
    expect(state.treeState.workers.w1.status).toBe("completed");
    expect(state.treeState.workers.w1.isStreaming).toBe(false);
  });
});

// ============================================================
// 8. RESET
// ============================================================

describe("RESET", () => {
  it("RESET returns all state to initialState", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    let state = applyActions([
      { type: "INIT", threads },
      { type: "RUN_STARTED" },
      { type: "SET_PAUSE_PENDING", value: true },
    ]);
    expect(state.treeState.manager).not.toBeNull();
    expect(state.runStatus).toBe("running");

    state = runActivityReducer(state, { type: "RESET" });
    expect(state.treeState.manager).toBeNull();
    expect(state.runStatus).toBe("waiting");
    expect(state.pausePending).toBe(false);
    expect(state.liveBuffers).toEqual({});
  });
});

// ============================================================
// 9. INIT non-destructive reconcile (formerly Mj3)
// ============================================================

describe("Mj3: INIT non-destructive reconcile", () => {
  it("applying INIT to a live manager state does not overwrite thinking/isStreaming", () => {
    const threads: ThreadEntry[] = [
      makeThreadEntry({ id: "manager", role: "manager", status: "active" }),
    ];
    let state = applyActions([{ type: "INIT", threads }]);
    const ev: ManagerTurnStartedEvent = {
      ...BASE_EVENT,
      type: "manager_turn_started",
      turn: 0,
    };
    state = runActivityReducer(state, { type: "MANAGER_TURN_STARTED", event: ev });
    expect(state.treeState.manager?.status).toBe("thinking");
    expect(state.treeState.manager?.isStreaming).toBe(true);

    // Re-apply INIT with the same thread list (reproduces a re-INIT triggered by a worker event)
    state = runActivityReducer(state, { type: "INIT", threads });

    expect(state.treeState.manager?.status).toBe("thinking");
    expect(state.treeState.manager?.isStreaming).toBe(true);
  });

  it("INIT replaces a pending node with a real worker thread", () => {
    const threads: ThreadEntry[] = [makeThreadEntry({ id: "manager", role: "manager" })];
    let state = applyActions([{ type: "INIT", threads }]);

    const delEv: DelegationEvent = {
      ...BASE_EVENT,
      type: "delegation",
      items: [{ task_id: "T1", repo: "repo-a", title: "Task one" }],
    };
    state = runActivityReducer(state, { type: "DELEGATION", event: delEv });
    expect(state.treeState.workers["pending-T1"]).toBeDefined();

    const threadsWithWorker: ThreadEntry[] = [
      makeThreadEntry({ id: "manager", role: "manager" }),
      makeThreadEntry({
        id: "worker-1",
        role: "worker",
        task: "T1",
        status: "active",
        title: "Task one",
      }),
    ];
    state = runActivityReducer(state, { type: "INIT", threads: threadsWithWorker });

    expect(state.treeState.workers["pending-T1"]).toBeUndefined();
    expect(state.treeState.workers["worker-1"]).toBeDefined();
    expect(state.treeState.workers["worker-1"].taskTitle).toBe("Task one");
  });
});

// ============================================================
// 10. taskTitle survival test (formerly Mj4)
// ============================================================

describe("Mj4: taskTitle is carried over by WORKER_STARTED", () => {
  it("delegation → worker_started carries title from pending to the real worker", () => {
    const threads = [makeThreadEntry({ id: "manager", role: "manager" })];
    let state = applyActions([{ type: "INIT", threads }]);

    const delEv: DelegationEvent = {
      ...BASE_EVENT,
      type: "delegation",
      items: [{ task_id: "T1", repo: "repo-a", title: "Task title" }],
    };
    state = runActivityReducer(state, { type: "DELEGATION", event: delEv });
    expect(state.treeState.workers["pending-T1"]?.taskTitle).toBe("Task title");

    const startEv: WorkerStartedEvent = {
      ...BASE_EVENT,
      type: "worker_started",
      worker_id: "worker-1",
      task_id: "T1",
      repo: "repo-a",
    };
    state = runActivityReducer(state, { type: "WORKER_STARTED", event: startEv });

    expect(state.treeState.workers["worker-1"]).toBeDefined();
    expect(state.treeState.workers["worker-1"].taskTitle).toBe("Task title");
    expect(state.treeState.workers["pending-T1"]).toBeUndefined();
  });
});

// ============================================================
// 11. Cross-Epic RESET test (formerly Mj6)
// ============================================================

describe("Mj6: RESET action clears nodes from the previous Epic", () => {
  it("state after RESET equals initialState", () => {
    const threadsA: ThreadEntry[] = [
      makeThreadEntry({ id: "manager", role: "manager" }),
      makeThreadEntry({ id: "worker-a1", role: "worker", task: "T1" }),
    ];
    let state = applyActions([{ type: "INIT", threads: threadsA }]);
    expect(state.treeState.manager).not.toBeNull();
    expect(Object.keys(state.treeState.workers)).toHaveLength(1);

    state = runActivityReducer(state, { type: "RESET" });
    expect(state.treeState.manager).toBeNull();
    expect(state.treeState.workers).toEqual({});
    expect(state.treeState.evaluators).toEqual({});
    expect(state.treeState.taskToWorker).toEqual({});
  });

  it("INIT after RESET contains only nodes for the new Epic (Epic switch flow)", () => {
    const threadsA: ThreadEntry[] = [
      makeThreadEntry({ id: "manager-a", role: "manager" }),
      makeThreadEntry({ id: "worker-a1", role: "worker", task: "T1" }),
    ];
    let state = applyActions([{ type: "INIT", threads: threadsA }]);

    state = runActivityReducer(state, { type: "RESET" });
    const threadsB: ThreadEntry[] = [makeThreadEntry({ id: "manager-b", role: "manager" })];
    state = runActivityReducer(state, { type: "INIT", threads: threadsB });

    expect(state.treeState.manager?.threadId).toBe("manager-b");
    expect(state.treeState.workers["worker-a1"]).toBeUndefined();
  });
});

// ============================================================
// 12. WORKER_STARTED terminal-state guard (defensive)
// ============================================================

describe("WORKER_STARTED terminal-state guard", () => {
  it("WORKER_STARTED arriving for a completed worker causes no state change (same reference = state is returned)", () => {
    const startEv: WorkerStartedEvent = {
      ...BASE_EVENT,
      type: "worker_started",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    const completedEv: WorkerCompletedEvent = {
      ...BASE_EVENT,
      type: "worker_completed",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    let state = applyActions([
      { type: "WORKER_STARTED", event: startEv },
      { type: "WORKER_COMPLETED", event: completedEv },
    ]);
    expect(state.treeState.workers.w1.status).toBe("completed");

    // Even when a delayed WORKER_STARTED arrives, it stays completed (guard early-returns → same reference)
    const before = state;
    state = runActivityReducer(state, { type: "WORKER_STARTED", event: startEv });
    expect(state).toBe(before); // WORKER_STARTED guard returns state as-is
    expect(state.treeState.workers.w1.status).toBe("completed");
  });
});

// ============================================================
// 13. TOKEN terminal-state guard (formerly Mn1)
// ============================================================

describe("Mn1: TOKEN does not revive the tree state of terminal-state nodes", () => {
  // runActivityReducer always updates liveBuffer, but does not change the tree state of terminal-state nodes.
  // (The old treeReducer did not return the state reference as-is, but the integrated reducer updates liveBuffer,
  //  so referential identity is not guaranteed. Verify that tree node status does not change.)

  it("TOKEN arriving for a completed manager leaves the tree status as completed", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager", status: "resolved" })];
    let state = applyActions([{ type: "INIT", threads }, { type: "RUN_COMPLETED" }]);
    expect(state.treeState.manager?.status).toBe("completed");

    state = runActivityReducer(state, { type: "TOKEN", threadId: "mgr", delta: "late token" });
    // Tree node is not revived
    expect(state.treeState.manager?.status).toBe("completed");
    expect(state.treeState.manager?.isStreaming).toBe(false);
  });

  it("TOKEN arriving for a completed worker leaves the tree status as completed", () => {
    const startEv: WorkerStartedEvent = {
      ...BASE_EVENT,
      type: "worker_started",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    const completedEv: WorkerCompletedEvent = {
      ...BASE_EVENT,
      type: "worker_completed",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    let state = applyActions([
      { type: "WORKER_STARTED", event: startEv },
      { type: "WORKER_COMPLETED", event: completedEv },
    ]);
    expect(state.treeState.workers.w1.status).toBe("completed");

    state = runActivityReducer(state, { type: "TOKEN", threadId: "w1", delta: "late" });
    // Tree node is not revived
    expect(state.treeState.workers.w1.status).toBe("completed");
    expect(state.treeState.workers.w1.isStreaming).toBe(false);
  });
});

// ============================================================
// 14. INIT evaluator failed → rejected classification (formerly Mn2)
// ============================================================

describe("Mn2: INIT classifies failed evaluators as rejected", () => {
  it("an evaluator with status=failed is initialized as rejected", () => {
    const threads: ThreadEntry[] = [
      makeThreadEntry({ id: "manager", role: "manager" }),
      // The parent worker is always present (registered before the evaluator);
      // the tree is scoped to the active trial via the worker→manager link.
      makeThreadEntry({ id: "w1", role: "worker", task: "T1", parent_thread_id: "manager" }),
      makeThreadEntry({
        id: "eval-1",
        role: "evaluator",
        status: "failed",
        task: "T1",
        parent_thread_id: "w1",
      }),
    ];
    const state = applyActions([{ type: "INIT", threads }]);
    expect(state.treeState.evaluators["eval-1"].status).toBe("rejected");
  });

  it("an evaluator with status=resolved is initialized as accepted", () => {
    const threads: ThreadEntry[] = [
      makeThreadEntry({ id: "manager", role: "manager" }),
      makeThreadEntry({ id: "w1", role: "worker", task: "T1", parent_thread_id: "manager" }),
      makeThreadEntry({
        id: "eval-2",
        role: "evaluator",
        status: "resolved",
        task: "T1",
        parent_thread_id: "w1",
      }),
    ];
    const state = applyActions([{ type: "INIT", threads }]);
    expect(state.treeState.evaluators["eval-2"].status).toBe("accepted");
  });
});

// ============================================================
// 15. SSE cache patch — useRunActivity hook tests
//     (migrated from old use-run-events.test.ts / use-run-events-m5.test.ts)
// ============================================================

describe("useRunActivity — SSE cache patch (task_update)", () => {
  it("tasks cache is updated by a task_update event", () => {
    qc.setQueryData(queryKeys.tasks.get(PROJECT_ID, EPIC_ID), {
      tasks: [
        { id: "T1", title: "Task one", status: "todo" },
        { id: "T2", title: "Task two", status: "in_progress" },
      ],
      progress: { done: 0, total: 2 },
    });

    renderHook(() => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }), { wrapper });

    const es = MockEventSource.instances[0];
    expect(es).toBeTruthy();

    es.emit(
      "task_update",
      JSON.stringify({
        type: "task_update",
        project_id: PROJECT_ID,
        epic_id: EPIC_ID,
        run_id: "run-1",
        task_id: "T1",
        status: "done",
        title: "Task one",
      }),
    );

    const updated = qc.getQueryData<{ tasks: { id: string; status: string }[] }>(
      queryKeys.tasks.get(PROJECT_ID, EPIC_ID),
    );
    const t1 = updated?.tasks.find((t) => t.id === "T1");
    expect(t1?.status).toBe("done");
    const t2 = updated?.tasks.find((t) => t.id === "T2");
    expect(t2?.status).toBe("in_progress");
  });

  it("invalidates the tasks cache when task_update carries an unknown task id (new plan item)", () => {
    qc.setQueryData(queryKeys.tasks.get(PROJECT_ID, EPIC_ID), {
      tasks: [{ id: "T1", title: "Task one", status: "todo" }],
      progress: { done: 0, total: 1 },
    });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    renderHook(() => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }), { wrapper });

    const es = MockEventSource.instances[0];
    es.emit(
      "task_update",
      JSON.stringify({
        type: "task_update",
        project_id: PROJECT_ID,
        epic_id: EPIC_ID,
        run_id: "run-1",
        task_id: "T9",
        status: "todo",
        title: "Newly registered task",
      }),
    );

    // An in-place patch cannot add a task (and plan_hash / plan_approved would
    // go stale), so the whole tasks query is refetched instead.
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: queryKeys.tasks.get(PROJECT_ID, EPIC_ID),
    });
    const cached = qc.getQueryData<{ tasks: { id: string }[] }>(
      queryKeys.tasks.get(PROJECT_ID, EPIC_ID),
    );
    // The cached list itself is untouched (no phantom half-patched entry).
    expect(cached?.tasks.map((t) => t.id)).toEqual(["T1"]);
  });

  it("invalidates the tasks cache when task_update carries plan_changed (Manager tool)", () => {
    // The Manager's task_update tool can change plan-defining fields the event
    // does NOT carry (contract/repo/depends_on/agent) — even with an unchanged
    // title the plan hash may differ, so plan_changed=true forces a refetch.
    qc.setQueryData(queryKeys.tasks.get(PROJECT_ID, EPIC_ID), {
      tasks: [{ id: "T1", title: "Task one", status: "todo" }],
      progress: { done: 0, total: 1 },
    });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    renderHook(() => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }), { wrapper });

    const es = MockEventSource.instances[0];
    es.emit(
      "task_update",
      JSON.stringify({
        type: "task_update",
        project_id: PROJECT_ID,
        epic_id: EPIC_ID,
        run_id: "run-1",
        task_id: "T1",
        status: "todo",
        title: "Task one",
        plan_changed: true,
      }),
    );

    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: queryKeys.tasks.get(PROJECT_ID, EPIC_ID),
    });
  });
});

describe("useRunActivity — M5: run_paused / run_resumed cache patch", () => {
  it("runState cache becomes paused on run_paused", () => {
    seedRunState(makeRunState({ status: "running" }));
    renderHook(() => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }), { wrapper });

    const es = MockEventSource.instances[0];
    es.emit(
      "run_paused",
      JSON.stringify({
        type: "run_paused",
        project_id: PROJECT_ID,
        epic_id: EPIC_ID,
        run_id: "run-1",
      }),
    );

    expect(getRunStateCache()?.status).toBe("paused");
  });

  it("runState cache becomes running on run_resumed", () => {
    seedRunState(makeRunState({ status: "paused" }));
    renderHook(() => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }), { wrapper });

    const es = MockEventSource.instances[0];
    es.emit(
      "run_resumed",
      JSON.stringify({
        type: "run_resumed",
        project_id: PROJECT_ID,
        epic_id: EPIC_ID,
        run_id: "run-1",
      }),
    );

    expect(getRunStateCache()?.status).toBe("running");
  });

  it("run_paused is a no-op when runState cache is empty", () => {
    renderHook(() => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }), { wrapper });

    const es = MockEventSource.instances[0];
    es.emit(
      "run_paused",
      JSON.stringify({
        type: "run_paused",
        project_id: PROJECT_ID,
        epic_id: EPIC_ID,
        run_id: "run-1",
      }),
    );

    expect(getRunStateCache()).toBeUndefined();
  });
});

describe("useRunActivity — M5: worker_started / worker_completed active_workers patch", () => {
  it("worker_started adds an entry to active_workers", () => {
    seedRunState(makeRunState({ active_workers: [] }));
    renderHook(() => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }), { wrapper });

    const es = MockEventSource.instances[0];
    es.emit(
      "worker_started",
      JSON.stringify({
        type: "worker_started",
        project_id: PROJECT_ID,
        epic_id: EPIC_ID,
        run_id: "run-1",
        worker_id: "W1",
        task_id: "T1",
        repo: "repo-a",
      }),
    );

    const workers = getRunStateCache()?.active_workers ?? [];
    expect(workers).toHaveLength(1);
    expect(workers[0].worker_id).toBe("W1");
    expect(workers[0].task_id).toBe("T1");
    expect(workers[0].repo).toBe("repo-a");
  });

  it("worker_started with the same worker_id does not create duplicates", () => {
    seedRunState(makeRunState({ active_workers: [{ worker_id: "W1", task_id: "T1", repo: "r" }] }));
    renderHook(() => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }), { wrapper });

    const es = MockEventSource.instances[0];
    es.emit(
      "worker_started",
      JSON.stringify({
        type: "worker_started",
        project_id: PROJECT_ID,
        epic_id: EPIC_ID,
        run_id: "run-1",
        worker_id: "W1",
        task_id: "T1",
        repo: "r",
      }),
    );

    expect(getRunStateCache()?.active_workers).toHaveLength(1);
  });

  it("worker_completed removes the matching entry from active_workers", () => {
    seedRunState(
      makeRunState({
        active_workers: [
          { worker_id: "W1", task_id: "T1", repo: "r" },
          { worker_id: "W2", task_id: "T2", repo: "r" },
        ],
      }),
    );
    renderHook(() => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }), { wrapper });

    const es = MockEventSource.instances[0];
    es.emit(
      "worker_completed",
      JSON.stringify({
        type: "worker_completed",
        project_id: PROJECT_ID,
        epic_id: EPIC_ID,
        run_id: "run-1",
        worker_id: "W1",
        task_id: "T1",
        repo: "r",
      }),
    );

    const workers = getRunStateCache()?.active_workers ?? [];
    expect(workers).toHaveLength(1);
    expect(workers[0].worker_id).toBe("W2");
  });

  it("run_completed empties active_workers", () => {
    seedRunState(makeRunState({ active_workers: [{ worker_id: "W1", task_id: "T1", repo: "r" }] }));
    renderHook(() => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }), { wrapper });

    const es = MockEventSource.instances[0];
    es.emit(
      "run_completed",
      JSON.stringify({
        type: "run_completed",
        project_id: PROJECT_ID,
        epic_id: EPIC_ID,
        run_id: "run-1",
      }),
    );

    expect(getRunStateCache()?.active_workers).toHaveLength(0);
    expect(getRunStateCache()?.status).toBe("completed");
  });

  it("run_failed empties active_workers", () => {
    seedRunState(makeRunState({ active_workers: [{ worker_id: "W1", task_id: "T1", repo: "r" }] }));
    renderHook(() => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }), { wrapper });

    const es = MockEventSource.instances[0];
    es.emit(
      "run_failed",
      JSON.stringify({
        type: "run_failed",
        project_id: PROJECT_ID,
        epic_id: EPIC_ID,
        run_id: "run-1",
        error: "oops",
      }),
    );

    expect(getRunStateCache()?.active_workers).toHaveLength(0);
    expect(getRunStateCache()?.status).toBe("error");
  });
});

// ============================================================
// 16. pause_effective reaches the reducer (formerly B2)
// ============================================================

describe("B2: pause_effective reaches the reducer", () => {
  it("pause_effective → PAUSE_EFFECTIVE dispatch clears pausePending", () => {
    seedRunState(makeRunState({ status: "running" }));

    const { result } = renderHook(
      () => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }),
      { wrapper },
    );

    act(() => {
      result.current.setPausePending(true);
    });
    expect(result.current.state.pausePending).toBe(true);

    const es = MockEventSource.instances[0];
    act(() => {
      es.emit(
        "run_paused",
        JSON.stringify({
          type: "run_paused",
          project_id: PROJECT_ID,
          epic_id: EPIC_ID,
          run_id: "r",
        }),
      );
      es.emit(
        "pause_effective",
        JSON.stringify({
          type: "pause_effective",
          project_id: PROJECT_ID,
          epic_id: EPIC_ID,
          run_id: "r",
        }),
      );
    });

    expect(result.current.state.pausePending).toBe(false);
  });
});

// ============================================================
// 17. INIT tree construction test (formerly the INIT section of use-thread-tree.test.ts)
// ============================================================

describe("runActivityReducer — INIT tree construction", () => {
  it("a manager node is built from the manager thread", () => {
    const threads: ThreadEntry[] = [
      makeThreadEntry({ id: "manager", role: "manager", status: "active" }),
    ];
    const state = applyActions([{ type: "INIT", threads }]);
    expect(state.treeState.manager).not.toBeNull();
    expect(state.treeState.manager?.threadId).toBe("manager");
    expect(state.treeState.manager?.status).toBe("idle");
  });

  it("a worker node and taskToWorker are built from the worker thread", () => {
    const threads: ThreadEntry[] = [
      makeThreadEntry({ id: "manager", role: "manager", status: "resolved" }),
      makeThreadEntry({
        id: "worker-1",
        role: "worker",
        task: "T1",
        repo: "repo-a",
        status: "resolved",
      }),
    ];
    const state = applyActions([{ type: "INIT", threads }]);
    expect(state.treeState.workers["worker-1"]).toBeDefined();
    expect(state.treeState.workers["worker-1"].status).toBe("completed");
    expect(state.treeState.taskToWorker.T1).toBe("worker-1");
  });

  it("an evaluator node is built from the evaluator thread (linked by parent_thread_id)", () => {
    const threads: ThreadEntry[] = [
      makeThreadEntry({ id: "manager", role: "manager" }),
      makeThreadEntry({ id: "worker-1", role: "worker", task: "T1", parent_thread_id: "manager" }),
      makeThreadEntry({
        id: "eval-1",
        role: "evaluator",
        task: "T1",
        parent_thread_id: "worker-1",
        status: "resolved",
      }),
    ];
    const state = applyActions([{ type: "INIT", threads }]);
    expect(state.treeState.evaluators["eval-1"]).toBeDefined();
    expect(state.treeState.evaluators["eval-1"].workerId).toBe("worker-1");
  });
});

// ============================================================
// 18. RUN_STOPPED reducer (regression test)
// ============================================================

describe("RUN_STOPPED — reducer: settles into waiting and streaming nodes are finalized", () => {
  it("runStatus becomes waiting and pausePending is cleared", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    const state = applyActions([
      { type: "INIT", threads },
      { type: "RUN_STARTED" },
      { type: "SET_PAUSE_PENDING", value: true },
      { type: "RUN_STOPPED" },
    ]);
    expect(state.runStatus).toBe("waiting");
    expect(state.pausePending).toBe(false);
  });

  it("a running Manager node is finalized to completed + isStreaming=false", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    const turnEv: ManagerTurnStartedEvent = {
      ...BASE_EVENT,
      type: "manager_turn_started",
      turn: 0,
    };
    let state = applyActions([
      { type: "INIT", threads },
      { type: "RUN_STARTED" },
      { type: "MANAGER_TURN_STARTED", event: turnEv },
    ]);
    // Manager should be thinking + isStreaming=true
    expect(state.treeState.manager?.status).toBe("thinking");
    expect(state.treeState.manager?.isStreaming).toBe(true);

    state = runActivityReducer(state, { type: "RUN_STOPPED" });

    expect(state.runStatus).toBe("waiting");
    expect(state.treeState.manager?.status).toBe("completed");
    expect(state.treeState.manager?.isStreaming).toBe(false);
  });

  it("a running Worker node is finalized to completed + isStreaming=false", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    const startEv: WorkerStartedEvent = {
      ...BASE_EVENT,
      type: "worker_started",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    let state = applyActions([
      { type: "INIT", threads },
      { type: "RUN_STARTED" },
      { type: "WORKER_STARTED", event: startEv },
    ]);
    expect(state.treeState.workers.w1.status).toBe("running");
    expect(state.treeState.workers.w1.isStreaming).toBe(true);

    state = runActivityReducer(state, { type: "TOKEN", threadId: "w1", delta: "partial output" });
    state = runActivityReducer(state, { type: "RUN_STOPPED" });

    expect(state.runStatus).toBe("waiting");
    expect(state.treeState.workers.w1.status).toBe("completed");
    expect(state.treeState.workers.w1.isStreaming).toBe(false);
  });

  it("a Worker that was already completed remains completed after RUN_STOPPED", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    const startEv: WorkerStartedEvent = {
      ...BASE_EVENT,
      type: "worker_started",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    const completedEv: WorkerCompletedEvent = {
      ...BASE_EVENT,
      type: "worker_completed",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    let state = applyActions([
      { type: "INIT", threads },
      { type: "RUN_STARTED" },
      { type: "WORKER_STARTED", event: startEv },
      { type: "WORKER_COMPLETED", event: completedEv },
    ]);
    expect(state.treeState.workers.w1.status).toBe("completed");

    state = runActivityReducer(state, { type: "RUN_STOPPED" });

    expect(state.treeState.workers.w1.status).toBe("completed");
    expect(state.treeState.workers.w1.isStreaming).toBe(false);
  });

  it("RUN_STARTED following RUN_STOPPED makes runStatus return to running (re-run flow)", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    let state = applyActions([
      { type: "INIT", threads },
      { type: "RUN_STARTED" },
      { type: "RUN_STOPPED" },
    ]);
    expect(state.runStatus).toBe("waiting");

    state = runActivityReducer(state, { type: "RUN_STARTED" });
    expect(state.runStatus).toBe("running");
  });
});

// ============================================================
// 19. toRunActivityAction — run_stopped conversion (regression test)
// ============================================================

describe("toRunActivityAction — run_stopped → {type:'RUN_STOPPED'}", () => {
  it("run_stopped event is converted to a RUN_STOPPED action", () => {
    const event: RunStoppedEvent = {
      ...BASE_EVENT,
      type: "run_stopped",
    };
    const action = toRunActivityAction(event);
    expect(action).toEqual({ type: "RUN_STOPPED" });
  });

  it("run_completed is converted to RUN_COMPLETED (confirming no interference from adjacent cases)", () => {
    const action = toRunActivityAction({
      ...BASE_EVENT,
      type: "run_completed",
    });
    expect(action).toEqual({ type: "RUN_COMPLETED" });
  });

  it("run_failed is converted to RUN_FAILED (error field propagates)", () => {
    const action = toRunActivityAction({
      ...BASE_EVENT,
      type: "run_failed",
      error: "boom",
    });
    expect(action).toEqual({ type: "RUN_FAILED", error: "boom" });
  });
});

// ============================================================
// 20. applyRunCachePatch — run_stopped cache patch (regression test)
// ============================================================

describe("applyRunCachePatch — run_stopped: runState becomes waiting + active_workers=[]", () => {
  it("run_stopped makes runState cache become status:waiting, active_workers:[]", () => {
    seedRunState(
      makeRunState({
        status: "running",
        active_workers: [{ worker_id: "W1", task_id: "T1", repo: "r" }],
      }),
    );

    const event: RunStoppedEvent = {
      ...BASE_EVENT,
      type: "run_stopped",
    };
    applyRunCachePatch(qc, PROJECT_ID, EPIC_ID, event);

    const cache = getRunStateCache();
    expect(cache?.status).toBe("waiting");
    expect(cache?.active_workers).toHaveLength(0);
  });

  it("run_stopped is a no-op when runState cache is empty (remains undefined)", () => {
    // nothing is set in cache
    const event: RunStoppedEvent = {
      ...BASE_EVENT,
      type: "run_stopped",
    };
    applyRunCachePatch(qc, PROJECT_ID, EPIC_ID, event);

    expect(getRunStateCache()).toBeUndefined();
  });
});

// ============================================================
// 21. useRunActivity hook integration — run_stopped SSE event (regression test)
// ============================================================

describe("useRunActivity — SSE: runState cache is patched to waiting by run_stopped", () => {
  it("runState.status becomes waiting on a run_stopped SSE event", () => {
    seedRunState(
      makeRunState({
        status: "running",
        active_workers: [{ worker_id: "W1", task_id: "T1", repo: "r" }],
      }),
    );

    renderHook(() => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }), { wrapper });

    const es = MockEventSource.instances[0];
    es.emit(
      "run_stopped",
      JSON.stringify({
        type: "run_stopped",
        project_id: PROJECT_ID,
        epic_id: EPIC_ID,
        run_id: "run-1",
      }),
    );

    expect(getRunStateCache()?.status).toBe("waiting");
    expect(getRunStateCache()?.active_workers).toHaveLength(0);
  });

  it("after a run_stopped SSE event, reducer state's runStatus becomes waiting", () => {
    seedRunState(makeRunState({ status: "running" }));

    const { result } = renderHook(
      () => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }),
      { wrapper },
    );

    const es = MockEventSource.instances[0];
    act(() => {
      es.emit(
        "run_stopped",
        JSON.stringify({
          type: "run_stopped",
          project_id: PROJECT_ID,
          epic_id: EPIC_ID,
          run_id: "run-1",
        }),
      );
    });

    expect(result.current.state.runStatus).toBe("waiting");
    expect(result.current.state.pausePending).toBe(false);
  });
});

// ============================================================
// 22. your_turn — approval gate
// ============================================================

describe("YOUR_TURN — the run parks in waiting (your turn)", () => {
  it("YOUR_TURN sets runStatus=waiting and the parked marker", () => {
    const state = applyActions([
      { type: "RUN_STARTED" },
      { type: "YOUR_TURN", threadId: "manager" },
    ]);
    expect(state.runStatus).toBe("waiting");
    expect(state.yourTurn).toEqual({ threadId: "manager" });
  });

  it("RUN_RESUMED clears the parked marker", () => {
    const state = applyActions([
      { type: "RUN_STARTED" },
      { type: "YOUR_TURN", threadId: "manager" },
      { type: "RUN_RESUMED" },
    ]);
    expect(state.runStatus).toBe("running");
    expect(state.yourTurn).toBeNull();
  });

  it("MANAGER_TURN_STARTED clears the parked marker and returns runStatus to running", () => {
    const threads = [makeThreadEntry({ id: "manager", role: "manager" })];
    const turnEv: ManagerTurnStartedEvent = {
      ...BASE_EVENT,
      type: "manager_turn_started",
      turn: 1,
    };
    const state = applyActions([
      { type: "INIT", threads },
      { type: "RUN_STARTED" },
      { type: "YOUR_TURN", threadId: "manager" },
      { type: "MANAGER_TURN_STARTED", event: turnEv },
    ]);
    expect(state.runStatus).toBe("running");
    expect(state.yourTurn).toBeNull();
  });

  it("RUN_COMPLETED (job run) clears the parked marker", () => {
    const state = applyActions([
      { type: "RUN_STARTED" },
      { type: "YOUR_TURN", threadId: "manager" },
      { type: "RUN_COMPLETED" },
    ]);
    expect(state.runStatus).toBe("completed");
    expect(state.yourTurn).toBeNull();
  });

  it("RUN_FAILED clears the parked marker", () => {
    const state = applyActions([
      { type: "RUN_STARTED" },
      { type: "YOUR_TURN", threadId: "manager" },
      { type: "RUN_FAILED", error: "boom" },
    ]);
    expect(state.runStatus).toBe("error");
    expect(state.yourTurn).toBeNull();
  });

  it("a delayed YOUR_TURN after terminal(completed) does not roll back to waiting", () => {
    // Race between REST (getRunState) and SSE: do not revive terminal state when
    // a delayed parked snapshot arrives after run_completed (job run).
    const state = applyActions([
      { type: "RUN_STARTED" },
      { type: "RUN_COMPLETED" },
      { type: "YOUR_TURN", threadId: "manager" },
    ]);
    expect(state.runStatus).toBe("completed");
    expect(state.yourTurn).toBeNull();
  });

  it("a delayed YOUR_TURN after terminal(error) does not roll back to waiting", () => {
    const state = applyActions([
      { type: "RUN_STARTED" },
      { type: "RUN_FAILED", error: "boom" },
      { type: "YOUR_TURN", threadId: "manager" },
    ]);
    expect(state.runStatus).toBe("error");
    expect(state.yourTurn).toBeNull();
  });

  it("from the initial waiting default (reload restore), YOUR_TURN sets the parked marker", () => {
    // initialState.runStatus is "waiting" (never-run default). The terminal guard
    // must not include waiting, or REST restore of a parked run would break.
    const state = applyActions([{ type: "YOUR_TURN", threadId: "manager" }]);
    expect(state.runStatus).toBe("waiting");
    expect(state.yourTurn).toEqual({ threadId: "manager" });
  });

  it("RUN_STOPPED clears the parked marker and stays waiting", () => {
    const state = applyActions([
      { type: "RUN_STARTED" },
      { type: "YOUR_TURN", threadId: "manager" },
      { type: "RUN_STOPPED" },
    ]);
    expect(state.runStatus).toBe("waiting");
    expect(state.yourTurn).toBeNull();
  });

  it("RESET returns the parked marker to null", () => {
    let state = applyActions([{ type: "RUN_STARTED" }, { type: "YOUR_TURN", threadId: "manager" }]);
    expect(state.yourTurn).not.toBeNull();
    state = runActivityReducer(state, { type: "RESET" });
    expect(state.yourTurn).toBeNull();
    expect(state.runStatus).toBe("waiting");
  });
});

// ============================================================
// 23. toRunActivityAction — your_turn conversion
// ============================================================

describe("toRunActivityAction — your_turn → YOUR_TURN", () => {
  it("your_turn event is converted to a YOUR_TURN action (pure signal — no text payload)", () => {
    const event: YourTurnEvent = {
      ...BASE_EVENT,
      type: "your_turn",
      thread_id: "manager",
    };
    const action = toRunActivityAction(event);
    expect(action).toEqual({
      type: "YOUR_TURN",
      threadId: "manager",
    });
  });
});

// ============================================================
// 24. applyRunCachePatch — your_turn cache patch
// ============================================================

describe("applyRunCachePatch — your_turn: runState becomes waiting", () => {
  it("runState cache becomes waiting on your_turn", () => {
    seedRunState(makeRunState({ status: "running" }));

    const event: YourTurnEvent = {
      ...BASE_EVENT,
      type: "your_turn",
      thread_id: "manager",
    };
    applyRunCachePatch(qc, PROJECT_ID, EPIC_ID, event);

    expect(getRunStateCache()?.status).toBe("waiting");
  });

  it("your_turn refreshes the run identity fields, not just status", () => {
    // The runState cache is otherwise "mount snapshot + status patches" — a
    // frozen run_id/thread_id re-dispatched later (dispatchForRunStatus
    // on trial change) would attribute the parked marker to a long-gone run.
    seedRunState(makeRunState({ status: "running", run_id: "run-old", thread_id: "trial-old" }));

    const event: YourTurnEvent = {
      ...BASE_EVENT,
      type: "your_turn",
      run_id: "run-new",
      thread_id: "rev-1",
    };
    applyRunCachePatch(qc, PROJECT_ID, EPIC_ID, event);

    const cached = getRunStateCache();
    expect(cached?.status).toBe("waiting");
    expect(cached?.run_id).toBe("run-new");
    expect(cached?.thread_id).toBe("rev-1");
  });

  it("your_turn is a no-op when runState cache is empty", () => {
    const event: YourTurnEvent = {
      ...BASE_EVENT,
      type: "your_turn",
      thread_id: "manager",
    };
    applyRunCachePatch(qc, PROJECT_ID, EPIC_ID, event);
    expect(getRunStateCache()).toBeUndefined();
  });

  it("your_turn_ended returns a waiting cache to running", () => {
    seedRunState(makeRunState({ status: "waiting" }));

    applyRunCachePatch(qc, PROJECT_ID, EPIC_ID, {
      ...BASE_EVENT,
      type: "your_turn_ended",
      thread_id: "manager",
    });

    expect(getRunStateCache()?.status).toBe("running");
  });
});

// ============================================================
// 25. useRunActivity hook — your_turn SSE event
// ============================================================

describe("useRunActivity — SSE: transitions to waiting on your_turn", () => {
  it("runStatus becomes waiting (with the parked marker) on a your_turn SSE event", () => {
    seedRunState(makeRunState({ status: "running" }));

    const { result } = renderHook(
      () => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }),
      { wrapper },
    );

    const es = MockEventSource.instances[0];
    act(() => {
      es.emit(
        "your_turn",
        JSON.stringify({
          type: "your_turn",
          project_id: PROJECT_ID,
          epic_id: EPIC_ID,
          run_id: "run-1",
          thread_id: "manager",
        }),
      );
    });

    expect(result.current.state.runStatus).toBe("waiting");
    expect(result.current.state.yourTurn).toEqual({ threadId: "manager" });
  });
});

// ============================================================
// 26. CLEAR_LIVE_BUFFER — Bug4 double-rendering fix
// ============================================================

describe("CLEAR_LIVE_BUFFER — Bug4: clear the live buffer when the REST canonical message arrives to prevent double-rendering", () => {
  it("receiving CLEAR_LIVE_BUFFER after TOKEN accumulation empties the buffer", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    let state = applyActions([{ type: "INIT", threads }, { type: "RUN_STARTED" }]);
    state = runActivityReducer(state, { type: "TOKEN", threadId: "mgr", delta: "dispatch..." });
    expect(selectThreadLiveState(state, "mgr").streamState.segments[0].tokenBuffer).toBe(
      "dispatch...",
    );

    state = runActivityReducer(state, { type: "CLEAR_LIVE_BUFFER", threadId: "mgr" });

    expect(streamStateTextLength(selectThreadLiveState(state, "mgr").streamState)).toBe(0);
    expect(streamStateIsEmpty(selectThreadLiveState(state, "mgr").streamState)).toBe(true);
    expect(selectThreadLiveState(state, "mgr").streamState.done).toBe(false);
    expect(selectThreadLiveState(state, "mgr").isRunning).toBe(false);
  });

  it("CLEAR_LIVE_BUFFER wipes everything even when toolCalls have already accumulated", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    let state = applyActions([{ type: "INIT", threads }, { type: "RUN_STARTED" }]);
    state = runActivityReducer(state, {
      type: "TOOL_CALL",
      threadId: "mgr",
      event: {
        type: "tool_call",
        project_id: "p",
        epic_id: "e",
        run_id: "r",
        thread_id: "mgr",
        tool_name: "dispatch",
        tool_input: { task_id: "T1" },
        tool_use_id: "tu-1",
        msg_index: 0,
      },
    });
    expect(selectThreadLiveState(state, "mgr").streamState.segments[0].toolCalls).toHaveLength(1);

    state = runActivityReducer(state, { type: "CLEAR_LIVE_BUFFER", threadId: "mgr" });

    expect(streamStateIsEmpty(selectThreadLiveState(state, "mgr").streamState)).toBe(true);
  });

  it("CLEAR_LIVE_BUFFER for a nonexistent threadId does not change the state reference", () => {
    const state = applyActions([{ type: "RUN_STARTED" }]);
    const after = runActivityReducer(state, {
      type: "CLEAR_LIVE_BUFFER",
      threadId: "nonexistent",
    });
    expect(after).toBe(state);
  });

  it("CLEAR_LIVE_BUFFER clears only the target thread and preserves other thread buffers", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    const startEv: WorkerStartedEvent = {
      ...BASE_EVENT,
      type: "worker_started",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    let state = applyActions([
      { type: "INIT", threads },
      { type: "WORKER_STARTED", event: startEv },
    ]);
    state = runActivityReducer(state, { type: "TOKEN", threadId: "mgr", delta: "mgr-token" });
    state = runActivityReducer(state, { type: "TOKEN", threadId: "w1", delta: "worker-token" });

    state = runActivityReducer(state, { type: "CLEAR_LIVE_BUFFER", threadId: "mgr" });

    expect(streamStateTextLength(selectThreadLiveState(state, "mgr").streamState)).toBe(0);
    // worker buffer is unchanged
    expect(selectThreadLiveState(state, "w1").streamState.segments[0].tokenBuffer).toBe(
      "worker-token",
    );
  });

  it("CLEAR_LIVE_BUFFER is dispatched via the clearLiveBuffer callback", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    const { result } = renderHook(
      () =>
        useRunActivity({
          projectId: PROJECT_ID,
          epicId: EPIC_ID,
          initialThreads: threads,
        }),
      { wrapper },
    );

    const es = MockEventSource.instances[0];
    act(() => {
      es.emit(
        "token",
        JSON.stringify({
          type: "token",
          project_id: PROJECT_ID,
          epic_id: EPIC_ID,
          run_id: "run-1",
          thread_id: "mgr",
          delta: "streaming dispatch tool-call",
        }),
      );
    });
    expect(
      selectThreadLiveState(result.current.state, "mgr").streamState.segments[0].tokenBuffer,
    ).toBe("streaming dispatch tool-call");

    act(() => {
      result.current.clearLiveBuffer("mgr");
    });

    expect(
      streamStateTextLength(selectThreadLiveState(result.current.state, "mgr").streamState),
    ).toBe(0);
    expect(selectThreadLiveState(result.current.state, "mgr").isRunning).toBe(false);
  });
});

// ============================================================
// 27. YOUR_TURN_ENDED — Bug3 fix for status lingering after approval
// ============================================================

describe("YOUR_TURN_ENDED — Bug3: waiting does not linger on replay", () => {
  it("YOUR_TURN → YOUR_TURN_ENDED in order makes yourTurn null", () => {
    const state = applyActions([
      { type: "RUN_STARTED" },
      { type: "YOUR_TURN", threadId: "manager" },
      { type: "YOUR_TURN_ENDED", threadId: "manager" },
    ]);
    expect(state.yourTurn).toBeNull();
    expect(state.runStatus).toBe("running");
  });

  it("YOUR_TURN_ENDED while running stays running (no parked marker to release)", () => {
    const state = applyActions([
      { type: "RUN_STARTED" },
      { type: "YOUR_TURN_ENDED", threadId: "manager" },
    ]);
    expect(state.yourTurn).toBeNull();
    expect(state.runStatus).toBe("running");
  });

  it("replay scenario: final state is not waiting when request→resolved arrive in order", () => {
    // Simulating replay on SSE reconnect: request arrives first, then resolved
    let state = applyActions([{ type: "RUN_STARTED" }]);
    state = runActivityReducer(state, {
      type: "YOUR_TURN",
      threadId: "manager",
    });
    expect(state.runStatus).toBe("waiting");
    expect(state.yourTurn).not.toBeNull();

    state = runActivityReducer(state, {
      type: "YOUR_TURN_ENDED",
      threadId: "manager",
    });
    expect(state.yourTurn).toBeNull();
    expect(state.runStatus).toBe("running");
  });
});

describe("toRunActivityAction — your_turn_ended → YOUR_TURN_ENDED", () => {
  it("your_turn_ended event is converted to a YOUR_TURN_ENDED action", () => {
    const event: YourTurnEndedEvent = {
      ...BASE_EVENT,
      type: "your_turn_ended",
      thread_id: "manager",
    };
    const action = toRunActivityAction(event);
    expect(action).toEqual({ type: "YOUR_TURN_ENDED", threadId: "manager" });
  });
});

describe("applyRunCachePatch — your_turn_ended: runState returns to running", () => {
  it("runState cache becomes running on your_turn_ended", () => {
    seedRunState(makeRunState({ status: "waiting" }));

    const event: YourTurnEndedEvent = {
      ...BASE_EVENT,
      type: "your_turn_ended",
      thread_id: "manager",
    };
    applyRunCachePatch(qc, PROJECT_ID, EPIC_ID, event);

    expect(getRunStateCache()?.status).toBe("running");
  });

  it("your_turn_ended is a no-op when runState cache is empty", () => {
    const event: YourTurnEndedEvent = {
      ...BASE_EVENT,
      type: "your_turn_ended",
      thread_id: "manager",
    };
    applyRunCachePatch(qc, PROJECT_ID, EPIC_ID, event);
    expect(getRunStateCache()).toBeUndefined();
  });
});

describe("useRunActivity — SSE: waiting is cleared by your_turn_ended", () => {
  it("a your_turn_ended SSE event returns runStatus to running and clears the parked marker", () => {
    seedRunState(makeRunState({ status: "waiting" }));

    const { result } = renderHook(
      () => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }),
      { wrapper },
    );

    const es = MockEventSource.instances[0];
    // first, park the run (your turn)
    act(() => {
      es.emit(
        "your_turn",
        JSON.stringify({
          type: "your_turn",
          project_id: PROJECT_ID,
          epic_id: EPIC_ID,
          run_id: "run-1",
          thread_id: "manager",
        }),
      );
    });
    expect(result.current.state.runStatus).toBe("waiting");

    // resolved releases it
    act(() => {
      es.emit(
        "your_turn_ended",
        JSON.stringify({
          type: "your_turn_ended",
          project_id: PROJECT_ID,
          epic_id: EPIC_ID,
          run_id: "run-1",
          thread_id: "manager",
        }),
      );
    });

    expect(result.current.state.runStatus).toBe("running");
    expect(result.current.state.yourTurn).toBeNull();
  });
});

// ============================================================
// 28. Bug4 guard: do not clear on messages.length increase while streaming
// (verifying the meaning of the done flag at the reducer level)
// ============================================================

describe("CLEAR_LIVE_BUFFER — Bug4 guard: the done flag can be respected even if clear is called while done=false", () => {
  it("after TOKEN arrives, done=false: liveState.streamState.done is false", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    let state = applyActions([{ type: "INIT", threads }, { type: "RUN_STARTED" }]);
    state = runActivityReducer(state, { type: "TOKEN", threadId: "mgr", delta: "streaming..." });
    // done=false while streaming — thread-page-client checks this to suppress clearing
    expect(selectThreadLiveState(state, "mgr").streamState.done).toBe(false);
    expect(selectThreadLiveState(state, "mgr").streamState.segments[0].tokenBuffer).toBe(
      "streaming...",
    );
  });
});

// ============================================================
// 29. YOUR_TURN_ENDED guard: does not overwrite terminal state
// ============================================================

describe("YOUR_TURN_ENDED — does not overwrite terminal/stopped state with running", () => {
  it("YOUR_TURN_ENDED arriving after completed leaves runStatus as completed", () => {
    const state = applyActions([
      { type: "RUN_STARTED" },
      { type: "YOUR_TURN", threadId: "manager" },
      { type: "RUN_COMPLETED" }, // transition to terminal state
      { type: "YOUR_TURN_ENDED", threadId: "manager" }, // delayed resolved
    ]);
    expect(state.runStatus).toBe("completed");
    expect(state.yourTurn).toBeNull(); // yourTurn is cleared
  });

  it("YOUR_TURN_ENDED arriving after failed leaves runStatus as error", () => {
    const state = applyActions([
      { type: "RUN_STARTED" },
      { type: "YOUR_TURN", threadId: "manager" },
      { type: "RUN_FAILED", error: "some error" },
      { type: "YOUR_TURN_ENDED", threadId: "manager" },
    ]);
    expect(state.runStatus).toBe("error");
    expect(state.yourTurn).toBeNull();
  });

  it("YOUR_TURN_ENDED arriving after stopped stays waiting (marker already cleared)", () => {
    // RUN_STOPPED settles into waiting WITHOUT the parked marker; a delayed
    // resolved replay must not fake "running" out of that resting state.
    const state = applyActions([
      { type: "RUN_STARTED" },
      { type: "YOUR_TURN", threadId: "manager" },
      { type: "RUN_STOPPED" },
      { type: "YOUR_TURN_ENDED", threadId: "manager" },
    ]);
    expect(state.runStatus).toBe("waiting");
    expect(state.yourTurn).toBeNull();
  });
});

describe("applyRunCachePatch — your_turn_ended: does not overwrite terminal state with running", () => {
  it("cache remains completed when your_turn_ended arrives in completed state", () => {
    seedRunState(makeRunState({ status: "completed" }));
    const event: YourTurnEndedEvent = {
      ...BASE_EVENT,
      type: "your_turn_ended",
      thread_id: "manager",
    };
    applyRunCachePatch(qc, PROJECT_ID, EPIC_ID, event);
    expect(getRunStateCache()?.status).toBe("completed");
  });

  it("in waiting state, your_turn_ended returns to running (happy path)", () => {
    seedRunState(makeRunState({ status: "waiting" }));
    const event: YourTurnEndedEvent = {
      ...BASE_EVENT,
      type: "your_turn_ended",
      thread_id: "manager",
    };
    applyRunCachePatch(qc, PROJECT_ID, EPIC_ID, event);
    expect(getRunStateCache()?.status).toBe("running");
  });
});

// ============================================================
// 31. Multi-turn re-stream (#multi-turn-regression fix verification)
// ============================================================

describe("Multi-turn re-stream: streamState is reset by *_STARTED after a turn completes", () => {
  it("MANAGER_MESSAGE(done=true) → MANAGER_TURN_STARTED resets streamState to done=false", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    const turnEv1: ManagerTurnStartedEvent = {
      ...BASE_EVENT,
      type: "manager_turn_started",
      turn: 0,
    };
    const msgEv: ManagerMessageEvent = {
      ...BASE_EVENT,
      type: "manager_message",
      thread_id: "mgr",
      turn: 0,
      text: "Turn 1 complete",
    };
    const turnEv2: ManagerTurnStartedEvent = {
      ...BASE_EVENT,
      type: "manager_turn_started",
      turn: 1,
    };

    // Turn 1: STARTED → TOKEN → MANAGER_MESSAGE(done=true)
    let state = applyActions([{ type: "INIT", threads }, { type: "RUN_STARTED" }]);
    state = runActivityReducer(state, { type: "MANAGER_TURN_STARTED", event: turnEv1 });
    state = runActivityReducer(state, { type: "TOKEN", threadId: "mgr", delta: "Turn 1 text" });
    state = runActivityReducer(state, { type: "MANAGER_MESSAGE", event: msgEv });

    // After turn 1 completes: done=true
    expect(selectThreadLiveState(state, "mgr").streamState.done).toBe(true);

    // Turn 2: reset by MANAGER_TURN_STARTED
    state = runActivityReducer(state, { type: "MANAGER_TURN_STARTED", event: turnEv2 });

    // streamState is reset to done=false
    expect(selectThreadLiveState(state, "mgr").streamState.done).toBe(false);
    expect(streamStateTextLength(selectThreadLiveState(state, "mgr").streamState)).toBe(0);
    expect(streamStateIsEmpty(selectThreadLiveState(state, "mgr").streamState)).toBe(true);
    expect(selectThreadLiveState(state, "mgr").isRunning).toBe(true);
  });

  it("a TOKEN in turn 2 makes the stream bubble renderable (bubble appears with done=false)", () => {
    const threads = [makeThreadEntry({ id: "mgr", role: "manager" })];
    const turnEv1: ManagerTurnStartedEvent = {
      ...BASE_EVENT,
      type: "manager_turn_started",
      turn: 0,
    };
    const msgEv: ManagerMessageEvent = {
      ...BASE_EVENT,
      type: "manager_message",
      thread_id: "mgr",
      turn: 0,
      text: "Turn 1 complete",
    };
    const turnEv2: ManagerTurnStartedEvent = {
      ...BASE_EVENT,
      type: "manager_turn_started",
      turn: 1,
    };

    let state = applyActions([{ type: "INIT", threads }, { type: "RUN_STARTED" }]);
    state = runActivityReducer(state, { type: "MANAGER_TURN_STARTED", event: turnEv1 });
    state = runActivityReducer(state, { type: "TOKEN", threadId: "mgr", delta: "t1" });
    state = runActivityReducer(state, { type: "MANAGER_MESSAGE", event: msgEv });
    state = runActivityReducer(state, { type: "MANAGER_TURN_STARTED", event: turnEv2 });
    state = runActivityReducer(state, { type: "TOKEN", threadId: "mgr", delta: "Turn 2 text" });

    // After turn 2 TOKEN: done=false and tokenBuffer has a value
    const liveState = selectThreadLiveState(state, "mgr");
    expect(liveState.streamState.done).toBe(false);
    expect(liveState.streamState.segments[0].tokenBuffer).toBe("Turn 2 text");
    // → buildYukarAdapter is in a state where it can render the stream bubble
  });

  it("WORKER_COMPLETED(done=true) → WORKER_STARTED resets streamState to done=false", () => {
    const startEv1: WorkerStartedEvent = {
      ...BASE_EVENT,
      type: "worker_started",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    const completedEv: WorkerCompletedEvent = {
      ...BASE_EVENT,
      type: "worker_completed",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    // Worker restart (e.g., same worker_id started again) is rejected by the WORKER_STARTED guard,
    // so we use a different worker to verify the pattern
    const startEv2: WorkerStartedEvent = {
      ...BASE_EVENT,
      type: "worker_started",
      worker_id: "w2",
      task_id: "T2",
      repo: "r",
    };

    let state = applyActions([{ type: "WORKER_STARTED", event: startEv1 }]);
    state = runActivityReducer(state, { type: "TOKEN", threadId: "w1", delta: "w1 output" });
    state = runActivityReducer(state, { type: "WORKER_COMPLETED", event: completedEv });

    // w1 completed: done=true
    expect(state.liveBuffers.w1.streamState.done).toBe(true);

    // w2 new start: done=false + empty buffer
    state = runActivityReducer(state, { type: "WORKER_STARTED", event: startEv2 });
    expect(state.liveBuffers.w2.streamState.done).toBe(false);
    expect(streamStateTextLength(state.liveBuffers.w2.streamState)).toBe(0);
    expect(state.liveBuffers.w2.isRunning).toBe(true);
  });

  it("EVAL_RESULT(done=true) → EVALUATOR_STARTED resets streamState to done=false", () => {
    const startEv1: EvaluatorStartedEvent = {
      ...BASE_EVENT,
      type: "evaluator_started",
      eval_id: "eval-1",
      worker_id: "w1",
      task_id: "T1",
      repo: "r",
    };
    const resultEv: EvalResultEvent = {
      ...BASE_EVENT,
      type: "eval_result",
      eval_id: "eval-1",
      worker_id: "w1",
      accepted: true,
      feedback: "LGTM",
    };
    const startEv2: EvaluatorStartedEvent = {
      ...BASE_EVENT,
      type: "evaluator_started",
      eval_id: "eval-1",
      worker_id: "w2",
      task_id: "T2",
      repo: "r",
    };

    let state = applyActions([{ type: "EVALUATOR_STARTED", event: startEv1 }]);
    state = runActivityReducer(state, {
      type: "TOKEN",
      threadId: "eval-1",
      delta: "Evaluating...",
    });
    state = runActivityReducer(state, { type: "EVAL_RESULT", event: resultEv });

    // eval-1 completed: done=true
    expect(state.liveBuffers["eval-1"].streamState.done).toBe(true);

    // same eval_id restarts with EVALUATOR_STARTED: reset to done=false
    state = runActivityReducer(state, { type: "EVALUATOR_STARTED", event: startEv2 });
    expect(state.liveBuffers["eval-1"].streamState.done).toBe(false);
    expect(streamStateTextLength(state.liveBuffers["eval-1"].streamState)).toBe(0);
    expect(state.liveBuffers["eval-1"].isRunning).toBe(true);
  });
});

// ============================================================
// 32. PR-C: USER_MESSAGE_COMMITTED — immediate visibility of injected utterances + dedup
// ============================================================

// Helper: retrieve the thread messages cache
function getThreadMsgsCache(threadId: string): Message[] | undefined {
  return qc.getQueryData<Message[]>(queryKeys.threads.messages(PROJECT_ID, EPIC_ID, threadId));
}

describe("toRunActivityAction — user_message_committed → USER_MESSAGE_COMMITTED", () => {
  it("user_message_committed event is converted to a USER_MESSAGE_COMMITTED action", () => {
    const event: UserMessageCommittedEvent = {
      ...BASE_EVENT,
      type: "user_message_committed",
      thread_id: "manager",
      text: "Please start the task",
      message_id: 3,
    };
    const action = toRunActivityAction(event);
    expect(action).toEqual({ type: "USER_MESSAGE_COMMITTED", event });
  });
});

describe("applyRunCachePatch — user_message_committed: optimistic append to thread messages cache", () => {
  it("normal inject: one user message is added to the thread messages cache", () => {
    qc.setQueryData<Message[]>(queryKeys.threads.messages(PROJECT_ID, EPIC_ID, "manager"), [
      {
        message_id: 1,
        message: { role: "assistant", content: [{ text: "I will make a plan" }] },
      },
    ]);

    const event: UserMessageCommittedEvent = {
      ...BASE_EVENT,
      type: "user_message_committed",
      thread_id: "manager",
      text: "I approve",
      message_id: 2,
    };
    applyRunCachePatch(qc, PROJECT_ID, EPIC_ID, event);

    const msgs = getThreadMsgsCache("manager");
    expect(msgs).toHaveLength(2);
    const last = msgs?.[1];
    expect(last?.message_id).toBe(2);
    expect(last?.message.role).toBe("user");
    expect(last?.message.content[0].text).toBe("I approve");
  });

  it("message_id dedup: not added when the same message_id already exists in cache", () => {
    qc.setQueryData<Message[]>(queryKeys.threads.messages(PROJECT_ID, EPIC_ID, "manager"), [
      {
        message_id: 2,
        message: { role: "user", content: [{ text: "I approve" }] },
      },
    ]);

    const event: UserMessageCommittedEvent = {
      ...BASE_EVENT,
      type: "user_message_committed",
      thread_id: "manager",
      text: "I approve",
      message_id: 2,
    };
    applyRunCachePatch(qc, PROJECT_ID, EPIC_ID, event);

    // not added due to dedup
    expect(getThreadMsgsCache("manager")).toHaveLength(1);
  });

  it("no duplicate when the same event arrives twice during reconnection backfill replay", () => {
    qc.setQueryData<Message[]>(queryKeys.threads.messages(PROJECT_ID, EPIC_ID, "manager"), []);

    const event: UserMessageCommittedEvent = {
      ...BASE_EVENT,
      type: "user_message_committed",
      thread_id: "manager",
      text: "Reconnect test",
      message_id: 5,
    };

    // 1st time (normal receive)
    applyRunCachePatch(qc, PROJECT_ID, EPIC_ID, event);
    // 2nd time (reconnect backfill replay)
    applyRunCachePatch(qc, PROJECT_ID, EPIC_ID, event);

    expect(getThreadMsgsCache("manager")).toHaveLength(1);
    expect(getThreadMsgsCache("manager")?.[0].message_id).toBe(5);
  });

  it("user_message_committed is a no-op when cache is undefined", () => {
    // nothing is set in cache
    const event: UserMessageCommittedEvent = {
      ...BASE_EVENT,
      type: "user_message_committed",
      thread_id: "manager",
      text: "hello",
      message_id: 1,
    };
    applyRunCachePatch(qc, PROJECT_ID, EPIC_ID, event);
    // cache remains undefined
    expect(getThreadMsgsCache("manager")).toBeUndefined();
  });

  it("structural verification that no double bubble occurs because server message_id matches after REST confirmation (refetch)", () => {
    // Scenario where the server returns a Message with the same message_id into an optimistically-appended cache:
    // setQueryData overwrites on REST refetch, so dedup kicks in due to message_id match.
    qc.setQueryData<Message[]>(queryKeys.threads.messages(PROJECT_ID, EPIC_ID, "manager"), []);

    const event: UserMessageCommittedEvent = {
      ...BASE_EVENT,
      type: "user_message_committed",
      thread_id: "manager",
      text: "Approve",
      message_id: 10,
    };
    // optimistic append via SSE
    applyRunCachePatch(qc, PROJECT_ID, EPIC_ID, event);
    expect(getThreadMsgsCache("manager")).toHaveLength(1);

    // REST refetch returns the full list (containing the same message_id) and overwrites cache
    const serverMessages: Message[] = [
      { message_id: 10, message: { role: "user", content: [{ text: "Approve" }] } },
    ];
    qc.setQueryData<Message[]>(
      queryKeys.threads.messages(PROJECT_ID, EPIC_ID, "manager"),
      serverMessages,
    );

    // Cache holds only the 1 canonical REST item — no duplicates
    expect(getThreadMsgsCache("manager")).toHaveLength(1);
    expect(getThreadMsgsCache("manager")?.[0].message_id).toBe(10);
  });
});

describe("useRunActivity — SSE: user_message_committed is immediately reflected in the thread messages cache", () => {
  it("one utterance is added to the thread messages cache on a user_message_committed SSE event", () => {
    qc.setQueryData<Message[]>(queryKeys.threads.messages(PROJECT_ID, EPIC_ID, "manager"), []);

    renderHook(() => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }), { wrapper });

    const es = MockEventSource.instances[0];
    act(() => {
      es.emit(
        "user_message_committed",
        JSON.stringify({
          type: "user_message_committed",
          project_id: PROJECT_ID,
          epic_id: EPIC_ID,
          run_id: "run-1",
          thread_id: "manager",
          text: "Please start the task",
          message_id: 3,
        }),
      );
    });

    const msgs = getThreadMsgsCache("manager");
    expect(msgs).toHaveLength(1);
    expect(msgs?.[0].message_id).toBe(3);
    expect(msgs?.[0].message.role).toBe("user");
    expect(msgs?.[0].message.content[0].text).toBe("Please start the task");
  });

  it("only 1 entry remains in cache via message_id dedup even when user_message_committed arrives twice", () => {
    qc.setQueryData<Message[]>(queryKeys.threads.messages(PROJECT_ID, EPIC_ID, "manager"), []);

    renderHook(() => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }), { wrapper });

    const es = MockEventSource.instances[0];
    const payload = JSON.stringify({
      type: "user_message_committed",
      project_id: PROJECT_ID,
      epic_id: EPIC_ID,
      run_id: "run-1",
      thread_id: "manager",
      text: "Duplicate test",
      message_id: 7,
    });

    act(() => {
      es.emit("user_message_committed", payload);
    });
    act(() => {
      // simulating reconnect backfill replay
      es.emit("user_message_committed", payload);
    });

    expect(getThreadMsgsCache("manager")).toHaveLength(1);
  });
});

// ============================================================
// 33. Reload scenario integration test — your-turn (waiting) restore
// ============================================================
//
// P3 semantics:
//   - A parked run persists status="waiting" with a real run_id. On reload the
//     REST RunState alone restores the parked marker (yourTurn) — the
//     question needs no restore path because it is the agent's final message
//     in the thread (fetched with the thread messages).
//   - A never-run epic gets a synthesised RunState (run_id="") — it is also
//     "waiting" (your turn) but carries NO parked marker, so no banner.
//
// ============================================================

describe("Reload scenario — the parked marker is restored from REST waiting state", () => {
  it("initialRunState waiting with a real run_id restores the parked marker on mount (no SSE needed)", () => {
    const initialRunState: RunState = makeRunState({
      status: "waiting",
      thread_id: "manager",
    });

    const { result } = renderHook(
      () =>
        useRunActivity({
          projectId: PROJECT_ID,
          epicId: EPIC_ID,
          initialRunState,
        }),
      { wrapper },
    );

    expect(result.current.state.runStatus).toBe("waiting");
    expect(result.current.state.yourTurn).toEqual({ threadId: "manager" });
  });

  it("a synthesised never-run RunState (run_id='') stays waiting WITHOUT the parked marker", () => {
    const initialRunState: RunState = makeRunState({ run_id: "", status: "waiting" });

    const { result } = renderHook(
      () =>
        useRunActivity({
          projectId: PROJECT_ID,
          epicId: EPIC_ID,
          initialRunState,
        }),
      { wrapper },
    );

    expect(result.current.state.runStatus).toBe("waiting");
    expect(result.current.state.yourTurn).toBeNull();
  });

  it("the parked marker is attributed to RunState.thread_id when present", () => {
    const initialRunState: RunState = makeRunState({
      status: "waiting",
      thread_id: "trial-2",
    });

    const { result } = renderHook(
      () =>
        useRunActivity({
          projectId: PROJECT_ID,
          epicId: EPIC_ID,
          initialRunState,
        }),
      { wrapper },
    );

    expect(result.current.state.yourTurn).toEqual({ threadId: "trial-2" });
  });

  it("the parked marker is also restored when the getRunState fetch returns waiting", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(makeRunState({ status: "waiting", thread_id: "manager" })),
      }),
    );

    const { result } = renderHook(
      () => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID }),
      { wrapper },
    );

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
    });

    expect(result.current.state.runStatus).toBe("waiting");
    expect(result.current.state.yourTurn).toEqual({ threadId: "manager" });
  });
});

// ============================================================
// 34. M1: Multi-trial support — unify manager node threadId to the real id
// ============================================================

describe("M1: Multi-trial — manager node threadId is unified to the real id", () => {
  it("SET_ACTIVE_TRIAL_ID updates treeState.manager.threadId to the real id", () => {
    const threads = [makeThreadEntry({ id: "trial-1", role: "manager" })];
    let state = applyActions([{ type: "INIT", threads }]);
    expect(state.treeState.manager?.threadId).toBe("trial-1");

    // Restore from RunState.thread_id via REST
    state = runActivityReducer(state, {
      type: "SET_ACTIVE_TRIAL_ID",
      threadId: "trial-2",
    });

    expect(state.activeTrialId).toBe("trial-2");
    // Tree node is also synced
    expect(state.treeState.manager?.threadId).toBe("trial-2");
  });

  it("SET_ACTIVE_TRIAL_ID when manager is null does not change treeState", () => {
    let state = applyActions([]);
    expect(state.treeState.manager).toBeNull();

    state = runActivityReducer(state, {
      type: "SET_ACTIVE_TRIAL_ID",
      threadId: "trial-1",
    });

    expect(state.activeTrialId).toBe("trial-1");
    expect(state.treeState.manager).toBeNull(); // remains null
  });

  it("RUN_STARTED creates the manager node preferring managerThreadId (real id)", () => {
    // Receive RUN_STARTED with managerThreadId already restored from REST
    let state = applyActions([{ type: "SET_ACTIVE_TRIAL_ID", threadId: "trial-3" }]);
    expect(state.activeTrialId).toBe("trial-3");

    state = runActivityReducer(state, { type: "RUN_STARTED" });

    expect(state.treeState.manager?.threadId).toBe("trial-3");
    expect(state.runStatus).toBe("running");
  });

  it("RUN_STARTED overwrites the existing node's threadId with managerThreadId", () => {
    const threads = [makeThreadEntry({ id: "trial-1", role: "manager" })];
    let state = applyActions([
      { type: "INIT", threads },
      { type: "SET_ACTIVE_TRIAL_ID", threadId: "trial-2" },
    ]);
    expect(state.treeState.manager?.threadId).toBe("trial-2"); // already synced after SET

    state = runActivityReducer(state, { type: "RUN_STARTED" });
    // RUN_STARTED also uses managerThreadId, so trial-2 is preserved
    expect(state.treeState.manager?.threadId).toBe("trial-2");
  });

  it("INIT overwrites threadId with the real id even when an existing manager node is present (not preserved)", () => {
    const threads1 = [makeThreadEntry({ id: "trial-1", role: "manager", status: "active" })];
    let state = applyActions([{ type: "INIT", threads: threads1 }]);
    expect(state.treeState.manager?.threadId).toBe("trial-1");

    // After switching to a new trial, the threads list is updated
    const threads2 = [
      makeThreadEntry({ id: "trial-1", role: "manager", status: "archived" }),
      makeThreadEntry({ id: "trial-2", role: "manager", status: "active" }),
    ];
    // Re-apply INIT (new active thread is at the end)
    state = runActivityReducer(state, { type: "INIT", threads: threads2 });
    // Updated with the id of the last processed manager thread
    expect(state.treeState.manager?.threadId).toBe("trial-2");
  });

  it("MANAGER_TURN_STARTED live buffer key matches manager.threadId (real id)", () => {
    const threads = [makeThreadEntry({ id: "trial-2", role: "manager" })];
    const ev: ManagerTurnStartedEvent = {
      ...BASE_EVENT,
      type: "manager_turn_started",
      turn: 0,
    };
    const state = applyActions([
      { type: "INIT", threads },
      { type: "RUN_STARTED" },
      { type: "MANAGER_TURN_STARTED", event: ev },
    ]);

    // Live buffer key is the real id "trial-2"
    expect(state.liveBuffers["trial-2"]).toBeDefined();
    expect(state.liveBuffers["trial-2"].isRunning).toBe(true);
    // No buffer with a hardcoded id like "manager" exists
    expect(state.liveBuffers.manager).toBeUndefined();
  });

  it("MANAGER_MESSAGE live buffer key matches manager.threadId (real id)", () => {
    const threads = [makeThreadEntry({ id: "trial-2", role: "manager" })];
    const turnEv: ManagerTurnStartedEvent = {
      ...BASE_EVENT,
      type: "manager_turn_started",
      turn: 0,
    };
    const msgEv: ManagerMessageEvent = {
      ...BASE_EVENT,
      type: "manager_message",
      thread_id: "trial-2",
      turn: 0,
      text: "Plan complete",
    };
    const state = applyActions([
      { type: "INIT", threads },
      { type: "RUN_STARTED" },
      { type: "MANAGER_TURN_STARTED", event: turnEv },
      { type: "MANAGER_MESSAGE", event: msgEv },
    ]);

    // Live buffer is done=true after MANAGER_MESSAGE (prevents double rendering)
    expect(state.liveBuffers["trial-2"]).toBeDefined();
    expect(state.liveBuffers["trial-2"].streamState.done).toBe(true);
    // No buffer with key "manager" exists
    expect(state.liveBuffers.manager).toBeUndefined();
  });
});

// ============================================================
// 35. activeTrialId resolution (P4 split) — epic.active_thread_id is the sole authority;
//     RunState.thread_id never feeds the trial (it is the run's own thread → currentRun)
//
// Regression verification for the fix:
//   Immediately after creating a new trial, epic.active_thread_id = new trial, but
//   RunState.thread_id = the thread id from a completed old run (stale).
//   If activeThreadId is not prioritized, the composer disappears.
// ============================================================

describe("35: activeTrialId resolution — activeThreadId (epic.active_thread_id) takes top priority", () => {
  it("when activeThreadId is specified, it is not overwritten by stale RunState.thread_id", async () => {
    // RunState.thread_id = stale id from an old trial
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve(makeRunState({ status: "completed", thread_id: "th-old-stale" })),
      }),
    );

    const { result } = renderHook(
      () =>
        useRunActivity({
          projectId: PROJECT_ID,
          epicId: EPIC_ID,
          // activeThreadId = epic.active_thread_id = new trial (source of truth)
          activeThreadId: "th-new-active",
        }),
      { wrapper },
    );

    // Wait for the getRunState fetch to complete
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
    });

    // activeThreadId (th-new-active) takes priority and is not overwritten by stale th-old-stale
    expect(result.current.state.activeTrialId).toBe("th-new-active");
  });

  it("when activeThreadId is not specified, thread_id feeds currentRun — NOT activeTrialId (P4 split)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve(makeRunState({ status: "running", thread_id: "th-current-run" })),
      }),
    );

    const { result } = renderHook(
      () =>
        useRunActivity({
          projectId: PROJECT_ID,
          epicId: EPIC_ID,
          // activeThreadId not specified
        }),
      { wrapper },
    );

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
    });

    // P4: RunState.thread_id is the RUN's own thread. It is captured as
    // currentRun (banner attribution) and never becomes the active trial
    // (during a reviewer run it would point at the reviewer thread).
    expect(result.current.state.activeTrialId).toBeNull();
    expect(result.current.state.currentRun).toEqual({
      threadId: "th-current-run",
      role: "manager",
    });
  });

  it("when activeThreadId is specified, it is not overwritten by initialRunState's thread_id either", () => {
    // Pass a stale thread_id as initialRunState
    const initialRunState: RunState = makeRunState({
      status: "completed",
      thread_id: "th-old-stale",
    });

    const { result } = renderHook(
      () =>
        useRunActivity({
          projectId: PROJECT_ID,
          epicId: EPIC_ID,
          initialRunState,
          activeThreadId: "th-new-active",
        }),
      { wrapper },
    );

    // Immediately after mount (sync): activeThreadId takes priority over initialRunState
    expect(result.current.state.activeTrialId).toBe("th-new-active");
  });

  it("when activeThreadId=null, falls back to the non-archived manager from initialThreads", () => {
    const threads = [
      makeThreadEntry({ id: "th-resolved-mgr", role: "manager", status: "resolved" }),
    ];
    const initialRunState: RunState = makeRunState({ status: "completed" });

    const { result } = renderHook(
      () =>
        useRunActivity({
          projectId: PROJECT_ID,
          epicId: EPIC_ID,
          initialRunState,
          initialThreads: threads,
          // activeThreadId not specified (equivalent to null)
        }),
      { wrapper },
    );

    // RunState.thread_id is null, activeThreadId is null
    // → resolved (non-archived) manager from initialThreads is used as fallback
    expect(result.current.state.activeTrialId).toBe("th-resolved-mgr");
  });
});

// ============================================================
// 36. Fix 3: excluding archived managers in applyTreeInit
//
// Regression verification for the fix:
//   Even when threads.yaml is re-sorted and an archived old trial ends up at the end,
//   the manager node must not be overwritten with the archived manager's id.
// ============================================================

describe("36: applyTreeInit — archived managers are excluded from manager node resolution", () => {
  it("manager node is not overwritten with archived id even when archived old trial is at the end", () => {
    // Order: old trial (archived) → new trial (active)
    const threads: ThreadEntry[] = [
      makeThreadEntry({ id: "trial-1", role: "manager", status: "archived" }),
      makeThreadEntry({ id: "trial-2", role: "manager", status: "active" }),
    ];
    const state = applyActions([{ type: "INIT", threads }]);
    // archived trial-1 is excluded and active trial-2 is chosen
    expect(state.treeState.manager?.threadId).toBe("trial-2");
  });

  it("manager node keeps the active id even in reverse order (new active first, old archived last)", () => {
    // Case where threads.yaml is re-sorted with archived at the end
    const threadsAscending: ThreadEntry[] = [
      makeThreadEntry({ id: "trial-2", role: "manager", status: "active" }),
      makeThreadEntry({ id: "trial-1", role: "manager", status: "archived" }),
    ];
    const state = applyActions([{ type: "INIT", threads: threadsAscending }]);
    // active trial-2 is chosen and is not overwritten by archived trial-1 at the end
    expect(state.treeState.manager?.threadId).toBe("trial-2");
  });

  it("manager node is not created when all managers are archived", () => {
    const threads: ThreadEntry[] = [
      makeThreadEntry({ id: "trial-1", role: "manager", status: "archived" }),
      makeThreadEntry({ id: "trial-2", role: "manager", status: "archived" }),
    ];
    const state = applyActions([{ type: "INIT", threads }]);
    // Only archived entries → manager node is null
    expect(state.treeState.manager).toBeNull();
  });

  it("when old manager (active) becomes archived on re-INIT, existing node is preserved and maintains live state", () => {
    // First INIT: trial-1 is active
    const threads1: ThreadEntry[] = [
      makeThreadEntry({ id: "trial-1", role: "manager", status: "active" }),
    ];
    let state = applyActions([{ type: "INIT", threads: threads1 }, { type: "RUN_STARTED" }]);
    const turnEv: ManagerTurnStartedEvent = {
      ...BASE_EVENT,
      type: "manager_turn_started",
      turn: 0,
    };
    state = runActivityReducer(state, { type: "MANAGER_TURN_STARTED", event: turnEv });
    expect(state.treeState.manager?.status).toBe("thinking");
    expect(state.treeState.manager?.isStreaming).toBe(true);

    // INIT after creating a new trial: trial-1 is archived, trial-2 is active
    const threads2: ThreadEntry[] = [
      makeThreadEntry({ id: "trial-1", role: "manager", status: "archived" }),
      makeThreadEntry({ id: "trial-2", role: "manager", status: "active" }),
    ];
    state = runActivityReducer(state, { type: "INIT", threads: threads2 });

    // trial-2 is created as the new manager node (trial-1 is excluded as archived)
    // Live state (thinking/isStreaming) is inherited from the existing node (non-destructive reconcile)
    expect(state.treeState.manager?.threadId).toBe("trial-2");
  });

  it("INIT after SET_ACTIVE_TRIAL_ID does not overwrite managerThreadId with archived manager", () => {
    // epic.active_thread_id = trial-2 is already confirmed
    let state = applyActions([{ type: "SET_ACTIVE_TRIAL_ID", threadId: "trial-2" }]);
    expect(state.activeTrialId).toBe("trial-2");

    // INIT lists old trial (archived) at the end
    const threads: ThreadEntry[] = [
      makeThreadEntry({ id: "trial-2", role: "manager", status: "active" }),
      makeThreadEntry({ id: "trial-1", role: "manager", status: "archived" }),
    ];
    state = runActivityReducer(state, { type: "INIT", threads });

    // managerThreadId remains trial-2 as set by SET_ACTIVE_TRIAL_ID
    expect(state.activeTrialId).toBe("trial-2");
    // treeState.manager is also not overwritten by archived trial-1
    expect(state.treeState.manager?.threadId).toBe("trial-2");
  });
});

// ============================================================
// 37. Fix 1 (hardening): resolve active manager from live threads even with a stale activeThreadId prop
//
// Verifies at the reducer level that even if the layout RSC is stale during in-app navigation,
// the active manager is resolved from the threads.list query (after invalidation) so the composer appears.
// ============================================================

describe("37: active manager can be resolved from INIT (live threads) even with a stale activeThreadId prop", () => {
  it("after a stale activeThreadId (old trial id) is passed, INIT (with new thread) syncs manager to new id", () => {
    // EpicShell's activeThreadId is stale = still the old trial's id
    let state = applyActions([{ type: "SET_ACTIVE_TRIAL_ID", threadId: "th-old" }]);
    expect(state.activeTrialId).toBe("th-old");

    // Latest thread list returned by live query after threads.list invalidate
    // old trial = archived, new trial = active
    const liveThreads: ThreadEntry[] = [
      makeThreadEntry({ id: "th-old", role: "manager", status: "archived" }),
      makeThreadEntry({ id: "th-new", role: "manager", status: "active" }),
    ];
    // INIT is dispatched when threads.list invalidate → useQuery data arrives
    state = runActivityReducer(state, { type: "INIT", threads: liveThreads });

    // archived th-old is excluded and active th-new is resolved as the manager node
    expect(state.treeState.manager?.threadId).toBe("th-new");
  });

  it("composer visibility: isActiveTrial=true when manager node's threadId matches the currently displayed thread", () => {
    // Currently displaying new trial B's URL (threadId = "th-new")
    // INIT from live threads resolves manager.threadId to "th-new"
    const liveThreads: ThreadEntry[] = [
      makeThreadEntry({ id: "th-old", role: "manager", status: "archived" }),
      makeThreadEntry({ id: "th-new", role: "manager", status: "active" }),
    ];
    const state = applyActions([{ type: "INIT", threads: liveThreads }]);

    // If manager node is "th-new", comparison with currently displayed thread "th-new" is true
    const managerThreadId = state.treeState.manager?.threadId ?? state.activeTrialId;
    expect(managerThreadId).toBe("th-new");
    // If currently displaying old trial "th-old", result is false (no composer = correct)
    expect(managerThreadId === "th-old").toBe(false);
  });
});

// ============================================================
// run_preparing lifecycle tests
// ============================================================

describe("RUN_PREPARING — preparing phase before Manager starts", () => {
  it("RUN_PREPARING sets runStatus to 'preparing' from the waiting default", () => {
    const state = applyActions([{ type: "RUN_PREPARING" }]);
    expect(state.runStatus).toBe("preparing");
  });

  it("RUN_PREPARING clears runError from a previous failed run", () => {
    const state = applyActions([
      { type: "RUN_FAILED", error: "previous error" },
      { type: "RUN_PREPARING" },
    ]);
    expect(state.runStatus).toBe("preparing");
    expect(state.runError).toBeNull();
  });

  it("RUN_PREPARING clears yourTurn", () => {
    const state = applyActions([{ type: "YOUR_TURN", threadId: "mgr" }, { type: "RUN_PREPARING" }]);
    expect(state.runStatus).toBe("preparing");
    expect(state.yourTurn).toBeNull();
  });

  it("RUN_STARTED after RUN_PREPARING transitions to 'running'", () => {
    const state = applyActions([{ type: "RUN_PREPARING" }, { type: "RUN_STARTED" }]);
    expect(state.runStatus).toBe("running");
  });

  it("RUN_FAILED after RUN_PREPARING transitions to 'error'", () => {
    const state = applyActions([
      { type: "RUN_PREPARING" },
      { type: "RUN_FAILED", error: "index build failed" },
    ]);
    expect(state.runStatus).toBe("error");
    expect(state.runError).toBe("index build failed");
  });

  it("toRunActivityAction maps run_preparing event to RUN_PREPARING action", () => {
    const event = {
      type: "run_preparing" as const,
      project_id: "proj1",
      epic_id: "epic1",
      run_id: "run-1",
    };
    const action = toRunActivityAction(event);
    expect(action).toEqual({ type: "RUN_PREPARING" });
  });
});

// ============================================================
// 15. Agent-state tree is scoped to the active trial (P2)
//     Archived / inactive trials' workers + evaluators must not linger.
// ============================================================

describe("Agent-state tree scoping to active trial", () => {
  it("excludes an archived trial's worker + evaluator on INIT", () => {
    const threads: ThreadEntry[] = [
      // Archived trial A and its agents.
      makeThreadEntry({ id: "mgr-A", role: "manager", status: "archived" }),
      makeThreadEntry({
        id: "w-A",
        role: "worker",
        task: "T1",
        parent_thread_id: "mgr-A",
        status: "resolved",
      }),
      makeThreadEntry({
        id: "eval-A",
        role: "evaluator",
        task: "T1",
        parent_thread_id: "w-A",
        status: "resolved",
      }),
      // Active trial B (no agents yet).
      makeThreadEntry({ id: "mgr-B", role: "manager", status: "active" }),
    ];
    const state = applyActions([
      { type: "SET_ACTIVE_TRIAL_ID", threadId: "mgr-B" },
      { type: "INIT", threads },
    ]);
    expect(state.treeState.workers["w-A"]).toBeUndefined();
    expect(state.treeState.evaluators["eval-A"]).toBeUndefined();
    expect(Object.keys(state.treeState.workers)).toHaveLength(0);
    expect(Object.keys(state.treeState.evaluators)).toHaveLength(0);
    expect(state.treeState.taskToWorker.T1).toBeUndefined();
  });

  it("keeps the active trial's worker + evaluator on INIT", () => {
    const threads: ThreadEntry[] = [
      makeThreadEntry({ id: "mgr-B", role: "manager", status: "active" }),
      makeThreadEntry({ id: "w-B", role: "worker", task: "T1", parent_thread_id: "mgr-B" }),
      makeThreadEntry({
        id: "eval-B",
        role: "evaluator",
        task: "T1",
        parent_thread_id: "w-B",
        status: "resolved",
      }),
    ];
    const state = applyActions([
      { type: "SET_ACTIVE_TRIAL_ID", threadId: "mgr-B" },
      { type: "INIT", threads },
    ]);
    expect(state.treeState.workers["w-B"]).toBeDefined();
    expect(state.treeState.workers["w-B"].parentManagerId).toBe("mgr-B");
    expect(state.treeState.evaluators["eval-B"]).toBeDefined();
    expect(state.treeState.taskToWorker.T1).toBe("w-B");
  });

  it("drops all worker/evaluator nodes when no active trial remains", () => {
    // Only an archived trial exists (e.g. the sole trial was archived → active_thread_id cleared).
    const threads: ThreadEntry[] = [
      makeThreadEntry({ id: "mgr-A", role: "manager", status: "archived" }),
      makeThreadEntry({
        id: "w-A",
        role: "worker",
        task: "T1",
        parent_thread_id: "mgr-A",
        status: "resolved",
      }),
    ];
    const state = applyActions([
      { type: "SET_ACTIVE_TRIAL_ID", threadId: null },
      { type: "INIT", threads },
    ]);
    expect(Object.keys(state.treeState.workers)).toHaveLength(0);
    expect(Object.keys(state.treeState.evaluators)).toHaveLength(0);
  });

  it("prunes a previous trial's live worker when SET_ACTIVE_TRIAL_ID switches trials", () => {
    // Trial A is active and has a live (delegated) worker, then the user switches to trial B.
    const delegation: DelegationEvent = {
      ...BASE_EVENT,
      type: "delegation",
      items: [{ task_id: "T1", repo: "r", title: "task one" }],
    } as DelegationEvent;
    const state = applyActions([
      { type: "SET_ACTIVE_TRIAL_ID", threadId: "mgr-A" },
      // INIT creates the manager node so DELEGATION (which no-ops without one) applies.
      { type: "INIT", threads: [makeThreadEntry({ id: "mgr-A", role: "manager" })] },
      { type: "DELEGATION", event: delegation },
    ]);
    // The pending worker belongs to trial A.
    expect(state.treeState.workers["pending-T1"].parentManagerId).toBe("mgr-A");

    // Switch active trial to B — A's worker must be pruned.
    const after = runActivityReducer(state, {
      type: "SET_ACTIVE_TRIAL_ID",
      threadId: "mgr-B",
    });
    expect(after.treeState.workers["pending-T1"]).toBeUndefined();
    expect(Object.keys(after.treeState.workers)).toHaveLength(0);
  });

  it("clears the tree on SET_ACTIVE_TRIAL_ID(null) with no following INIT", () => {
    // Trial A is active with a live worker, then it is archived mid-session:
    // active_thread_id → null arrives via SET_ACTIVE_TRIAL_ID(null) with no
    // INIT to reconcile. The lingering worker must be cleared immediately.
    const delegation: DelegationEvent = {
      ...BASE_EVENT,
      type: "delegation",
      items: [{ task_id: "T1", repo: "r", title: "task one" }],
    } as DelegationEvent;
    const state = applyActions([
      { type: "SET_ACTIVE_TRIAL_ID", threadId: "mgr-A" },
      { type: "INIT", threads: [makeThreadEntry({ id: "mgr-A", role: "manager" })] },
      { type: "DELEGATION", event: delegation },
    ]);
    expect(state.treeState.workers["pending-T1"]).toBeDefined();

    const after = runActivityReducer(state, {
      type: "SET_ACTIVE_TRIAL_ID",
      threadId: null,
    });
    expect(Object.keys(after.treeState.workers)).toHaveLength(0);
    expect(Object.keys(after.treeState.evaluators)).toHaveLength(0);
  });
});

// ============================================================
// P4: attribution split — activeTrialId (composer) vs currentRun (your-turn banner)
// ============================================================

describe("P4: reviewer run attribution — marker on the reviewer thread, composer on the trial", () => {
  it("REST restore of a parked reviewer run: marker + currentRun on the reviewer thread, activeTrialId on the trial", () => {
    const initialRunState: RunState = makeRunState({
      status: "waiting",
      thread_id: "rev-1",
      role: "reviewer",
    });

    const { result } = renderHook(
      () =>
        useRunActivity({
          projectId: PROJECT_ID,
          epicId: EPIC_ID,
          initialRunState,
          activeThreadId: "trial-1",
        }),
      { wrapper },
    );

    // Composer stays on the manager trial; the your-turn marker belongs to the
    // reviewer conversation the run actually rides on (the misattribution bug).
    expect(result.current.state.activeTrialId).toBe("trial-1");
    expect(result.current.state.yourTurn).toEqual({ threadId: "rev-1" });
    expect(result.current.state.currentRun).toEqual({ threadId: "rev-1", role: "reviewer" });
  });

  it("reducer: YOUR_TURN does NOT attribute the marker to the trial thread", () => {
    let state = applyActions([{ type: "SET_ACTIVE_TRIAL_ID", threadId: "trial-1" }]);
    state = runActivityReducer(state, { type: "YOUR_TURN", threadId: "rev-1" });
    expect(state.yourTurn).toEqual({ threadId: "rev-1" });
    expect(state.activeTrialId).toBe("trial-1");
  });

  it("reducer: YOUR_TURN keeps the known role when the thread matches", () => {
    let state = applyActions([{ type: "SET_CURRENT_RUN", threadId: "rev-1", role: "reviewer" }]);
    state = runActivityReducer(state, { type: "YOUR_TURN", threadId: "rev-1" });
    expect(state.currentRun).toEqual({ threadId: "rev-1", role: "reviewer" });
  });

  it("reducer: YOUR_TURN resets the role when the thread changed (unknown until the REST refresh)", () => {
    let state = applyActions([{ type: "SET_CURRENT_RUN", threadId: "trial-1", role: "manager" }]);
    state = runActivityReducer(state, { type: "YOUR_TURN", threadId: "rev-1" });
    expect(state.currentRun).toEqual({ threadId: "rev-1", role: null });
  });

  it("reducer: SET_CURRENT_RUN is attribution only — runStatus and the marker are untouched", () => {
    let state = applyActions([{ type: "RUN_STARTED" }]);
    state = runActivityReducer(state, {
      type: "SET_CURRENT_RUN",
      threadId: "rev-1",
      role: "reviewer",
    });
    expect(state.runStatus).toBe("running");
    expect(state.yourTurn).toBeNull();
    expect(state.currentRun).toEqual({ threadId: "rev-1", role: "reviewer" });
  });

  it("SSE your_turn triggers a role refresh from REST (reviewer wording without reload)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve(
            makeRunState({ status: "waiting", thread_id: "rev-1", role: "reviewer" }),
          ),
      }),
    );

    const { result } = renderHook(
      () => useRunActivity({ projectId: PROJECT_ID, epicId: EPIC_ID, activeThreadId: "trial-1" }),
      { wrapper },
    );

    const es = MockEventSource.instances[0];
    await act(async () => {
      es.emit(
        "your_turn",
        JSON.stringify({
          type: "your_turn",
          project_id: PROJECT_ID,
          epic_id: EPIC_ID,
          run_id: "run-1",
          thread_id: "rev-1",
        }),
      );
      await new Promise((resolve) => setTimeout(resolve, 10));
    });

    expect(result.current.state.yourTurn).toEqual({ threadId: "rev-1" });
    expect(result.current.state.currentRun).toEqual({ threadId: "rev-1", role: "reviewer" });
    // The composer never migrates to the reviewer thread.
    expect(result.current.state.activeTrialId).toBe("trial-1");
  });
});
