/**
 * EventSource mock tests for the use-event-stream hook
 */

import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useEventStream } from "../lib/sse/use-event-stream";

// EventSource mock
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

  removeEventListener(type: string, handler: EventListener) {
    const arr = this.listeners.get(type);
    if (arr) {
      const idx = arr.indexOf(handler);
      if (idx !== -1) arr.splice(idx, 1);
    }
  }

  emit(type: string, data: string) {
    const ev = { type, data } as MessageEvent;
    const handlers = this.listeners.get(type) ?? [];
    for (const h of handlers) h(ev);
  }

  close() {}
}

beforeEach(() => {
  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useEventStream", () => {
  it("does not create EventSource when url is null", () => {
    renderHook(() => useEventStream({ url: null, onMessage: () => {} }));
    expect(MockEventSource.instances).toHaveLength(0);
  });

  it("creates EventSource with the given URL", () => {
    renderHook(() => useEventStream({ url: "/api/test/events", onMessage: () => {} }));
    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0].url).toBe("/api/test/events");
  });

  it("calls onMessage with parsed event data for named event types", () => {
    const received: unknown[] = [];
    renderHook(() =>
      useEventStream({
        url: "/api/events",
        onMessage: (msg) => received.push(msg),
      }),
    );

    const es = MockEventSource.instances[0];
    es.emit(
      "task_update",
      JSON.stringify({ type: "task_update", task_id: "T1", status: "done", title: "" }),
    );

    expect(received).toHaveLength(1);
    expect((received[0] as { type: string }).type).toBe("task_update");
    expect((received[0] as { data: { task_id: string } }).data.task_id).toBe("T1");
  });

  it("ignores malformed JSON", () => {
    const received: unknown[] = [];
    renderHook(() =>
      useEventStream({
        url: "/api/events",
        onMessage: (msg) => received.push(msg),
      }),
    );

    const es = MockEventSource.instances[0];
    es.emit("task_update", "NOT_JSON");

    expect(received).toHaveLength(0);
  });

  it("closes EventSource on unmount", () => {
    const closeSpy = vi.fn();
    const { unmount } = renderHook(() =>
      useEventStream({ url: "/api/events", onMessage: () => {} }),
    );

    const es = MockEventSource.instances[0];
    es.close = closeSpy;

    unmount();
    expect(closeSpy).toHaveBeenCalledOnce();
  });

  it("calls onOpen when open event fires", () => {
    const onOpen = vi.fn();
    renderHook(() => useEventStream({ url: "/api/events", onMessage: () => {}, onOpen }));

    const es = MockEventSource.instances[0];
    es.emit("open", "");

    expect(onOpen).toHaveBeenCalledOnce();
  });

  it("calls onOpen with latest callback after re-render", () => {
    const firstOnOpen = vi.fn();
    const secondOnOpen = vi.fn();
    const { rerender } = renderHook(
      ({ cb }: { cb: () => void }) =>
        useEventStream({ url: "/api/events", onMessage: () => {}, onOpen: cb }),
      { initialProps: { cb: firstOnOpen } },
    );

    rerender({ cb: secondOnOpen });

    const es = MockEventSource.instances[0];
    es.emit("open", "");

    // Uses the ref pattern so the latest callback is called
    expect(firstOnOpen).not.toHaveBeenCalled();
    expect(secondOnOpen).toHaveBeenCalledOnce();
  });

  // Fix 1: onReconnect callback to prevent live buffer duplication on reconnect
  describe("reconnect: onOpen fires only on first open, onReconnect fires from the second open onwards", () => {
    it("onOpen is called and onReconnect is not called on the first open", () => {
      const onOpen = vi.fn();
      const onReconnect = vi.fn();
      renderHook(() =>
        useEventStream({ url: "/api/events", onMessage: () => {}, onOpen, onReconnect }),
      );

      const es = MockEventSource.instances[0];
      es.emit("open", "");

      expect(onOpen).toHaveBeenCalledOnce();
      expect(onReconnect).not.toHaveBeenCalled();
    });

    it("onReconnect is called and onOpen is not called on the second open", () => {
      const onOpen = vi.fn();
      const onReconnect = vi.fn();
      renderHook(() =>
        useEventStream({ url: "/api/events", onMessage: () => {}, onOpen, onReconnect }),
      );

      const es = MockEventSource.instances[0];
      es.emit("open", ""); // first open
      es.emit("open", ""); // reconnect

      expect(onOpen).toHaveBeenCalledOnce();
      expect(onReconnect).toHaveBeenCalledOnce();
    });

    it("onReconnect is called on the third open too (multiple reconnects)", () => {
      const onOpen = vi.fn();
      const onReconnect = vi.fn();
      renderHook(() =>
        useEventStream({ url: "/api/events", onMessage: () => {}, onOpen, onReconnect }),
      );

      const es = MockEventSource.instances[0];
      es.emit("open", ""); // first
      es.emit("open", ""); // 2nd
      es.emit("open", ""); // 3rd

      expect(onOpen).toHaveBeenCalledOnce();
      expect(onReconnect).toHaveBeenCalledTimes(2);
    });

    it("onOpen fires only on the first open even when onReconnect is not provided (backward compatible)", () => {
      const onOpen = vi.fn();
      renderHook(() => useEventStream({ url: "/api/events", onMessage: () => {}, onOpen }));

      const es = MockEventSource.instances[0];
      es.emit("open", ""); // first
      es.emit("open", ""); // reconnect — no onReconnect, confirm no error

      expect(onOpen).toHaveBeenCalledOnce(); // first open only
    });

    it("onReconnect references the latest callback (ref pattern)", () => {
      const first = vi.fn();
      const second = vi.fn();
      const { rerender } = renderHook(
        ({ cb }: { cb: () => void }) =>
          useEventStream({ url: "/api/events", onMessage: () => {}, onReconnect: cb }),
        { initialProps: { cb: first } },
      );

      rerender({ cb: second });

      const es = MockEventSource.instances[0];
      es.emit("open", ""); // first open (onOpen only, no reconnect yet)
      es.emit("open", ""); // reconnect — latest (second) is called

      expect(first).not.toHaveBeenCalled();
      expect(second).toHaveBeenCalledOnce();
    });
  });

  // B1: named event delivery test — verify that WHATWG EventSource named events reach onMessage
  // Existing tests skipped the transport layer and hit the reducer directly, so B1 was not detectable.
  describe("B1: named events reach onMessage (EventSource allow-list regression test)", () => {
    it.each([
      "manager_turn_started",
      "manager_message",
      "delegation",
      "evaluator_started",
      "pause_effective",
    ] as const)("named event '%s' reaches onMessage", (eventType) => {
      const received: { type: string; data: unknown }[] = [];
      renderHook(() =>
        useEventStream({
          url: "/api/events",
          onMessage: (msg) => received.push(msg),
        }),
      );

      const es = MockEventSource.instances[0];
      const payload = JSON.stringify({ type: eventType, project_id: "p", epic_id: "e" });
      es.emit(eventType, payload);

      expect(received).toHaveLength(1);
      expect(received[0].type).toBe(eventType);
    });

    it("legacy event run_started still reaches onMessage (allow-list destruction regression)", () => {
      const received: { type: string }[] = [];
      renderHook(() =>
        useEventStream({
          url: "/api/events",
          onMessage: (msg) => received.push(msg as { type: string }),
        }),
      );

      const es = MockEventSource.instances[0];
      es.emit(
        "run_started",
        JSON.stringify({ type: "run_started", project_id: "p", epic_id: "e" }),
      );

      expect(received).toHaveLength(1);
      expect(received[0].type).toBe("run_started");
    });
  });
});
