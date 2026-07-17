/**
 * EventSource mock tests for the use-event-stream hook
 */

import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useEventStream } from "../lib/sse/use-event-stream";

// EventSource mock
class MockEventSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;

  url: string;
  readyState = 1;
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

  // Hidden-tab suspension: parked tabs must release their sockets — Chrome's
  // per-origin connection pool (6 for HTTP/1.1) is shared ACROSS tabs, and a
  // few background tabs holding SSE starve every fetch in every tab.
  describe("hidden-tab suspension", () => {
    /** Stub document.visibilityState/hidden and fire visibilitychange. */
    function setVisibility(hidden: boolean) {
      Object.defineProperty(document, "hidden", { value: hidden, configurable: true });
      Object.defineProperty(document, "visibilityState", {
        value: hidden ? "hidden" : "visible",
        configurable: true,
      });
      document.dispatchEvent(new Event("visibilitychange"));
    }

    beforeEach(() => {
      vi.useFakeTimers();
    });

    afterEach(() => {
      vi.useRealTimers();
      setVisibility(false);
    });

    it("closes the stream after the page stays hidden past the grace period", () => {
      renderHook(() =>
        useEventStream({ url: "/api/events", onMessage: () => {}, hiddenGraceMs: 30_000 }),
      );
      const es = MockEventSource.instances[0];
      const closeSpy = vi.fn();
      es.close = closeSpy;

      setVisibility(true);
      vi.advanceTimersByTime(29_999);
      expect(closeSpy).not.toHaveBeenCalled();
      vi.advanceTimersByTime(1);
      expect(closeSpy).toHaveBeenCalledOnce();
    });

    it("a quick tab switch within the grace period keeps the stream open", () => {
      renderHook(() =>
        useEventStream({ url: "/api/events", onMessage: () => {}, hiddenGraceMs: 30_000 }),
      );
      const es = MockEventSource.instances[0];
      const closeSpy = vi.fn();
      es.close = closeSpy;

      setVisibility(true);
      vi.advanceTimersByTime(10_000);
      setVisibility(false);
      vi.advanceTimersByTime(60_000);

      expect(closeSpy).not.toHaveBeenCalled();
      expect(MockEventSource.instances).toHaveLength(1); // no second connection either
    });

    it("reconnects when the page becomes visible again, firing onReconnect", () => {
      const onOpen = vi.fn();
      const onReconnect = vi.fn();
      renderHook(() =>
        useEventStream({
          url: "/api/events",
          onMessage: () => {},
          onOpen,
          onReconnect,
          hiddenGraceMs: 30_000,
        }),
      );
      MockEventSource.instances[0].emit("open", ""); // first open
      expect(onOpen).toHaveBeenCalledOnce();

      setVisibility(true);
      vi.advanceTimersByTime(30_000); // suspend fires
      expect(MockEventSource.instances).toHaveLength(1);

      setVisibility(false);
      expect(MockEventSource.instances).toHaveLength(2); // new connection
      MockEventSource.instances[1].emit("open", "");
      // Resume goes through the reconnect path → consumers dedupe backfill.
      expect(onReconnect).toHaveBeenCalledOnce();
      expect(onOpen).toHaveBeenCalledOnce();
    });

    it("hiddenGraceMs: 0 disables suspension entirely", () => {
      renderHook(() =>
        useEventStream({ url: "/api/events", onMessage: () => {}, hiddenGraceMs: 0 }),
      );
      const es = MockEventSource.instances[0];
      const closeSpy = vi.fn();
      es.close = closeSpy;

      setVisibility(true);
      vi.advanceTimersByTime(600_000);

      expect(closeSpy).not.toHaveBeenCalled();
    });

    it("a tab mounted while hidden starts its grace countdown immediately", () => {
      Object.defineProperty(document, "hidden", { value: true, configurable: true });
      Object.defineProperty(document, "visibilityState", {
        value: "hidden",
        configurable: true,
      });
      renderHook(() =>
        useEventStream({ url: "/api/events", onMessage: () => {}, hiddenGraceMs: 30_000 }),
      );
      const es = MockEventSource.instances[0];
      const closeSpy = vi.fn();
      es.close = closeSpy;

      vi.advanceTimersByTime(30_000);
      expect(closeSpy).toHaveBeenCalledOnce();
    });

    it("suspension cancels a pending backoff retry (no reconnect while hidden)", () => {
      // The backoff delay is capped at 30s internally, so pick retry > grace
      // WITHIN that cap: retry due at +20s, suspension at +10s must cancel it.
      renderHook(() =>
        useEventStream({
          url: "/api/events",
          onMessage: () => {},
          retryMs: 20_000,
          hiddenGraceMs: 10_000,
        }),
      );
      const es = MockEventSource.instances[0];
      es.readyState = MockEventSource.CLOSED; // browser gave up → manual backoff path
      es.onerror?.(new Event("error")); // schedules a retry at +20s

      setVisibility(true);
      vi.advanceTimersByTime(10_000); // suspend fires, must clear the pending retry
      vi.advanceTimersByTime(600_000); // nothing may reconnect while hidden
      expect(MockEventSource.instances).toHaveLength(1);
    });

    it("resume resets the backoff counter (first retry after resume uses the base delay)", () => {
      renderHook(() =>
        useEventStream({
          url: "/api/events",
          onMessage: () => {},
          retryMs: 20_000,
          hiddenGraceMs: 10_000,
        }),
      );
      // Inflate retryCount to 1: error → CLOSED → backoff scheduled at +20s…
      const es1 = MockEventSource.instances[0];
      es1.readyState = MockEventSource.CLOSED;
      es1.onerror?.(new Event("error"));
      // …then hide; suspend (at +10s, before the retry) cancels it.
      setVisibility(true);
      vi.advanceTimersByTime(10_000);
      expect(MockEventSource.instances).toHaveLength(1);

      // Resume: reconnect once, immediately.
      setVisibility(false);
      expect(MockEventSource.instances).toHaveLength(2);

      // A new failure after resume must back off from the BASE delay again —
      // without the retryCount reset the delay would be 40s (capped 30s) and
      // this retry would not fire at +20s.
      const es2 = MockEventSource.instances[1];
      es2.readyState = MockEventSource.CLOSED;
      es2.onerror?.(new Event("error"));
      vi.advanceTimersByTime(20_000);
      expect(MockEventSource.instances).toHaveLength(3);
    });

    it("unmount while suspended cleans up without reconnecting", () => {
      const { unmount } = renderHook(() =>
        useEventStream({ url: "/api/events", onMessage: () => {}, hiddenGraceMs: 30_000 }),
      );
      setVisibility(true);
      vi.advanceTimersByTime(30_000);
      unmount();
      setVisibility(false);
      expect(MockEventSource.instances).toHaveLength(1);
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
