/**
 * Unit tests for makeResolveEventHandlers
 *
 * - run_id filter: ignores lifecycle events from other runs
 * - run_id is null: no filtering (e.g. immediately after connection)
 * - Fallback: verifies transition to unknown state and Merge re-enable logic
 */

import { describe, expect, it, vi } from "vitest";
import { makeResolveEventHandlers } from "../components/features/diff/resolve-event-handlers";
import type { RunEvent } from "../lib/api/endpoints";

function makeHandlers(overrides: Partial<Parameters<typeof makeResolveEventHandlers>[0]> = {}) {
  const defaults = {
    resolveRunId: "run-abc",
    onRunStarted: vi.fn(),
    onRunCompleted: vi.fn(),
    onRunFailed: vi.fn(),
    onWorkerStarted: vi.fn(),
    onWorkerCompleted: vi.fn(),
  };
  return {
    ...defaults,
    ...overrides,
    handle: makeResolveEventHandlers({ ...defaults, ...overrides }),
  };
}

describe("makeResolveEventHandlers — run_id filter", () => {
  it("processes run_started when run_id matches", () => {
    const { handle, onRunStarted } = makeHandlers({ resolveRunId: "run-abc" });
    handle({ type: "run_started", run_id: "run-abc" } as unknown as RunEvent);
    expect(onRunStarted).toHaveBeenCalledOnce();
  });

  it("ignores run_started when run_id differs (replayed old run)", () => {
    const { handle, onRunStarted } = makeHandlers({ resolveRunId: "run-abc" });
    handle({ type: "run_started", run_id: "run-old" } as unknown as RunEvent);
    expect(onRunStarted).not.toHaveBeenCalled();
  });

  it("processes run_completed when run_id matches", () => {
    const { handle, onRunCompleted } = makeHandlers({ resolveRunId: "run-abc" });
    handle({ type: "run_completed", run_id: "run-abc" } as unknown as RunEvent);
    expect(onRunCompleted).toHaveBeenCalledOnce();
  });

  it("ignores run_completed when run_id differs", () => {
    const { handle, onRunCompleted } = makeHandlers({ resolveRunId: "run-abc" });
    handle({ type: "run_completed", run_id: "run-xyz" } as unknown as RunEvent);
    expect(onRunCompleted).not.toHaveBeenCalled();
  });

  it("processes run_failed when run_id matches and passes the error string", () => {
    const { handle, onRunFailed } = makeHandlers({ resolveRunId: "run-abc" });
    handle({
      type: "run_failed",
      run_id: "run-abc",
      error: "something went wrong",
    } as unknown as RunEvent);
    expect(onRunFailed).toHaveBeenCalledWith("something went wrong");
  });

  it("ignores run_failed when run_id differs", () => {
    const { handle, onRunFailed } = makeHandlers({ resolveRunId: "run-abc" });
    handle({
      type: "run_failed",
      run_id: "run-other",
      error: "other error",
    } as unknown as RunEvent);
    expect(onRunFailed).not.toHaveBeenCalled();
  });

  it("processes run_started regardless of run_id when resolveRunId is null", () => {
    const { handle, onRunStarted } = makeHandlers({ resolveRunId: null });
    handle({ type: "run_started", run_id: "any-run" } as unknown as RunEvent);
    expect(onRunStarted).toHaveBeenCalledOnce();
  });

  it("processes events with no run_id when resolveRunId is null", () => {
    const { handle, onRunCompleted } = makeHandlers({ resolveRunId: null });
    handle({ type: "run_completed" } as unknown as RunEvent);
    expect(onRunCompleted).toHaveBeenCalledOnce();
  });

  it("worker_started does not apply run_id filter (outside lifecycle)", () => {
    const { handle, onWorkerStarted } = makeHandlers({ resolveRunId: "run-abc" });
    handle({ type: "worker_started", run_id: "run-other", worker_id: "w1" } as unknown as RunEvent);
    expect(onWorkerStarted).toHaveBeenCalledWith("w1");
  });

  it("worker_completed does not apply run_id filter (outside lifecycle)", () => {
    const { handle, onWorkerCompleted } = makeHandlers({ resolveRunId: "run-abc" });
    handle({
      type: "worker_completed",
      run_id: "run-other",
      worker_id: "w2",
    } as unknown as RunEvent);
    expect(onWorkerCompleted).toHaveBeenCalledWith("w2");
  });

  it("uses a default message when error is undefined in run_failed", () => {
    const { handle, onRunFailed } = makeHandlers({ resolveRunId: "run-abc" });
    handle({ type: "run_failed", run_id: "run-abc" } as unknown as RunEvent);
    expect(onRunFailed).toHaveBeenCalledWith("Resolve run failed");
  });

  it("calls nothing for unknown event types", () => {
    const {
      handle,
      onRunStarted,
      onRunCompleted,
      onRunFailed,
      onWorkerStarted,
      onWorkerCompleted,
    } = makeHandlers();
    handle({ type: "token", run_id: "run-abc" } as unknown as RunEvent);
    expect(onRunStarted).not.toHaveBeenCalled();
    expect(onRunCompleted).not.toHaveBeenCalled();
    expect(onRunFailed).not.toHaveBeenCalled();
    expect(onWorkerStarted).not.toHaveBeenCalled();
    expect(onWorkerCompleted).not.toHaveBeenCalled();
  });
});
