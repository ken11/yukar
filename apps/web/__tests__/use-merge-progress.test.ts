/**
 * useMergeProgress — unit tests
 * Pattern: tested with a ProjectEventStreamProvider wrapper.
 */
import { act, renderHook } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectEventStreamProvider } from "../lib/sse/project-event-stream-context";
import { useMergeProgress } from "../lib/sse/use-merge-progress";

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

beforeEach(() => {
  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

/** Helper that provides ProjectEventStreamProvider as a wrapper */
function makeWrapper(projectId: string) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(ProjectEventStreamProvider, { projectId }, children);
  };
}

describe("useMergeProgress", () => {
  it("starts with null progress", () => {
    const { result } = renderHook(() => useMergeProgress("proj1"), {
      wrapper: makeWrapper("proj1"),
    });
    expect(result.current.progress).toBeNull();
  });

  it("does not crash when projectId is undefined", () => {
    // Verify that passing undefined to Provider does not crash.
    function Wrapper({ children }: { children: React.ReactNode }) {
      return React.createElement(
        ProjectEventStreamProvider,
        // biome-ignore lint/suspicious/noExplicitAny: for testing
        { projectId: undefined as any },
        children,
      );
    }
    const { result } = renderHook(() => useMergeProgress(undefined), { wrapper: Wrapper });
    expect(result.current.progress).toBeNull();
  });

  it("subscribes to project events SSE via Provider", () => {
    renderHook(() => useMergeProgress("proj1"), { wrapper: makeWrapper("proj1") });
    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0].url).toBe("/api/projects/proj1/events");
  });

  it("updates progress on epic_merge_progress event", () => {
    const { result } = renderHook(() => useMergeProgress("proj1"), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "epic_merge_progress",
        JSON.stringify({
          type: "epic_merge_progress",
          project_id: "proj1",
          epic_id: "EP-1",
          run_id: "run-arbiter-1",
          total: 3,
          completed: 1,
          current_epic_id: "EP-2",
          phase: "merging",
          results: [{ epic_id: "EP-1", status: "merged", detail: "", repos: [] }],
        }),
      );
    });

    const p = result.current.progress;
    expect(p).not.toBeNull();
    expect(p?.runId).toBe("run-arbiter-1");
    expect(p?.total).toBe(3);
    expect(p?.completed).toBe(1);
    expect(p?.currentEpicId).toBe("EP-2");
    expect(p?.phase).toBe("merging");
    expect(p?.results).toHaveLength(1);
    expect(p?.results?.[0].status).toBe("merged");
    expect(p?.isFinished).toBe(false);
  });

  it("drops unfinished progress and refetches the board on SSE reconnect", () => {
    // The project stream has no replay buffer — a "finished" published while
    // the connection was down never arrives, so an in-flight panel must not
    // stay stuck on stale progress after a reconnect.
    const onInvalidate = vi.fn();
    const { result } = renderHook(() => useMergeProgress("proj1", onInvalidate), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit("open", ""); // first open
      es.emit(
        "epic_merge_progress",
        JSON.stringify({
          type: "epic_merge_progress",
          project_id: "proj1",
          epic_id: "EP-1",
          run_id: "run-1",
          total: 3,
          completed: 1,
          current_epic_id: "EP-2",
          phase: "merging",
          results: [],
        }),
      );
    });
    expect(result.current.progress?.completed).toBe(1);

    act(() => {
      es.emit("open", ""); // reconnect — anything published in between is lost
    });
    expect(onInvalidate).toHaveBeenCalled();
    expect(result.current.progress).toBeNull();
  });

  it("keeps finished progress across a reconnect (results stay visible)", () => {
    const { result } = renderHook(() => useMergeProgress("proj1"), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit("open", "");
      es.emit(
        "epic_merge_progress",
        JSON.stringify({
          type: "epic_merge_progress",
          project_id: "proj1",
          epic_id: "EP-1",
          run_id: "run-1",
          total: 1,
          completed: 1,
          current_epic_id: null,
          phase: "finished",
          results: [{ epic_id: "EP-1", status: "merged", detail: "", repos: [] }],
        }),
      );
    });
    expect(result.current.progress?.isFinished).toBe(true);

    act(() => {
      es.emit("open", "");
    });
    expect(result.current.progress?.isFinished).toBe(true);
    expect(result.current.progress?.results).toHaveLength(1);
  });

  it("sets isFinished when phase is finished", () => {
    const { result } = renderHook(() => useMergeProgress("proj1"), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "epic_merge_progress",
        JSON.stringify({
          type: "epic_merge_progress",
          project_id: "proj1",
          epic_id: "EP-1",
          run_id: "run-1",
          total: 2,
          completed: 2,
          phase: "finished",
          results: [],
        }),
      );
    });

    expect(result.current.progress?.isFinished).toBe(true);
  });

  it("calls onInvalidate on epic_done phase", () => {
    const onInvalidate = vi.fn();
    renderHook(() => useMergeProgress("proj1", onInvalidate), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "epic_merge_progress",
        JSON.stringify({
          type: "epic_merge_progress",
          project_id: "proj1",
          epic_id: "EP-1",
          run_id: "run-1",
          total: 2,
          completed: 1,
          phase: "epic_done",
          results: [],
        }),
      );
    });

    expect(onInvalidate).toHaveBeenCalledOnce();
  });

  it("calls onInvalidate on finished phase", () => {
    const onInvalidate = vi.fn();
    renderHook(() => useMergeProgress("proj1", onInvalidate), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "epic_merge_progress",
        JSON.stringify({
          type: "epic_merge_progress",
          project_id: "proj1",
          epic_id: "EP-1",
          run_id: "run-1",
          total: 2,
          completed: 2,
          phase: "finished",
          results: [],
        }),
      );
    });

    expect(onInvalidate).toHaveBeenCalledOnce();
  });

  it("calls onInvalidate on epic_status_changed event", () => {
    const onInvalidate = vi.fn();
    renderHook(() => useMergeProgress("proj1", onInvalidate), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "epic_status_changed",
        JSON.stringify({
          type: "epic_status_changed",
          project_id: "proj1",
          epic_id: "EP-1",
          run_id: "",
          status: "completed",
        }),
      );
    });

    expect(onInvalidate).toHaveBeenCalledOnce();
  });

  it("calls onInvalidate on epic_merged event (merge fact recorded)", () => {
    const onInvalidate = vi.fn();
    renderHook(() => useMergeProgress("proj1", onInvalidate), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "epic_merged",
        JSON.stringify({
          type: "epic_merged",
          project_id: "proj1",
          epic_id: "EP-1",
          run_id: "",
          merged_at: "2026-07-11T00:00:00Z",
        }),
      );
    });

    expect(onInvalidate).toHaveBeenCalledOnce();
  });

  it("does not call onInvalidate on intermediate phases (resolving/merging)", () => {
    const onInvalidate = vi.fn();
    renderHook(() => useMergeProgress("proj1", onInvalidate), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "epic_merge_progress",
        JSON.stringify({
          type: "epic_merge_progress",
          project_id: "proj1",
          epic_id: "EP-1",
          run_id: "run-1",
          total: 2,
          completed: 0,
          phase: "resolving",
          results: [],
        }),
      );
    });

    expect(onInvalidate).not.toHaveBeenCalled();
  });

  it("ignores unrelated event types", () => {
    const { result } = renderHook(() => useMergeProgress("proj1"), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "run_completed",
        JSON.stringify({
          type: "run_completed",
          project_id: "proj1",
          epic_id: "EP-1",
          run_id: "run-1",
        }),
      );
    });

    expect(result.current.progress).toBeNull();
  });

  it("reset() clears progress to null", () => {
    const { result } = renderHook(() => useMergeProgress("proj1"), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "epic_merge_progress",
        JSON.stringify({
          type: "epic_merge_progress",
          project_id: "proj1",
          epic_id: "EP-1",
          run_id: "run-1",
          total: 1,
          completed: 1,
          phase: "finished",
          results: [],
        }),
      );
    });

    expect(result.current.progress).not.toBeNull();

    act(() => {
      result.current.reset();
    });

    expect(result.current.progress).toBeNull();
  });
});
