/**
 * Verification tests for finding[event-stream-double-dispatch]
 *
 * Hypothesis: a generic 'message' listener running in parallel with
 * named-event listeners double-dispatches onMessage for the same frame.
 *
 * Conclusion: this hook never registers a 'message' listener (it subscribes
 * only to named events; unnamed frames that have no `event:` field are
 * ignored). Therefore double dispatch cannot occur structurally.
 * Keep-alive signals are sent as SSE comments (`: ...`), which fire no
 * listener at all.
 *
 * This file contains characterization tests that lock the behavior
 * "only named events are dispatched". If a future implementation change
 * adds a 'message' listener, the unnamed-frame tests will fail and serve
 * as a safety net.
 */

import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useEventStream } from "../lib/sse/use-event-stream";

// ---------------------------------------------------------------------------
// Mock that faithfully reproduces WHATWG EventSource behavior
//
// Key spec points:
//   - Frame with `event: <type>` field → only the named listener fires;
//     the 'message' listener does NOT fire.
//   - Frame without `event:` field → only the 'message' listener fires.
//   - `: comment` → no listener fires (comments are silently ignored).
// ---------------------------------------------------------------------------
class WhatwgFaithfulEventSource {
  url: string;
  onerror: ((ev: Event) => void) | null = null;
  readyState: number;
  private listeners: Map<string, EventListener[]> = new Map();

  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;

  static instances: WhatwgFaithfulEventSource[] = [];

  constructor(url: string) {
    this.url = url;
    this.readyState = WhatwgFaithfulEventSource.CONNECTING;
    WhatwgFaithfulEventSource.instances.push(this);
  }

  addEventListener(type: string, handler: EventListener): void {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type)?.push(handler);
  }

  removeEventListener(type: string, handler: EventListener): void {
    const arr = this.listeners.get(type);
    if (arr) {
      const idx = arr.indexOf(handler);
      if (idx !== -1) arr.splice(idx, 1);
    }
  }

  /**
   * Reproduces a named event frame: `event: <type>\ndata: <json>\n\n`
   * Per WHATWG spec, only the named listener is called ('message' is not called).
   */
  emitNamed(type: string, data: string): void {
    const ev = { type, data } as MessageEvent;
    const handlers = this.listeners.get(type) ?? [];
    for (const h of handlers) h(ev);
    // Intentionally do not call the 'message' listener
  }

  /**
   * Reproduces an unnamed frame: `data: <json>\n\n` (no event: field)
   * Per WHATWG spec, only the 'message' listener is called.
   */
  emitUnnamed(data: string): void {
    const ev = { type: "message", data } as MessageEvent;
    const handlers = this.listeners.get("message") ?? [];
    for (const h of handlers) h(ev);
    // Do not call named listeners
  }

  /**
   * Reproduces an SSE comment: `: keep-alive\n\n`
   * No listener fires (comments are silently ignored per the EventSource spec).
   */
  emitComment(): void {
    // Comments are silently absorbed inside EventSource. Do nothing.
  }

  /**
   * Reproduces an error.
   * Sets readyState to the specified value and calls the onerror property handler.
   * The implementation assigns `es.onerror = ...` as a property, so we call
   * the property directly rather than via addEventListener.
   */
  emitError(readyState: number = WhatwgFaithfulEventSource.CLOSED): void {
    this.readyState = readyState;
    this.onerror?.(new Event("error"));
  }

  /**
   * Reproduces connection establishment.
   * Sets readyState to OPEN and calls all handlers registered via
   * addEventListener("open", ...) (the implementation registers open via addEventListener).
   */
  emitOpen(): void {
    this.readyState = WhatwgFaithfulEventSource.OPEN;
    const handlers = this.listeners.get("open") ?? [];
    const ev = new Event("open");
    for (const h of handlers) h(ev);
  }

  close(): void {
    this.readyState = WhatwgFaithfulEventSource.CLOSED;
  }
}

beforeEach(() => {
  WhatwgFaithfulEventSource.instances = [];
  vi.stubGlobal("EventSource", WhatwgFaithfulEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("finding[event-stream-double-dispatch]: mutual exclusivity of named event and message", () => {
  /**
   * Spec check: when a named event (`event: run_started`) arrives, the
   * 'message' listener is not called, so double dispatch cannot occur.
   * Confirms that onMessage is called exactly once via the named listener.
   */
  it("named event reaches onMessage exactly once (no double dispatch via 'message')", () => {
    const received: { type: string; data: unknown }[] = [];
    renderHook(() =>
      useEventStream({
        url: "/api/events",
        onMessage: (msg) => received.push(msg),
      }),
    );

    const es = WhatwgFaithfulEventSource.instances[0];
    es.emitNamed("run_started", JSON.stringify({ type: "run_started", project_id: "p" }));

    // No double dispatch → exactly once
    expect(received).toHaveLength(1);
    expect(received[0].type).toBe("run_started");
  });

  /**
   * Sending multiple named events consecutively matches the dispatch count.
   */
  it("sending n named events calls onMessage exactly n times", () => {
    const received: { type: string }[] = [];
    renderHook(() =>
      useEventStream({
        url: "/api/events",
        onMessage: (msg) => received.push(msg as { type: string }),
      }),
    );

    const es = WhatwgFaithfulEventSource.instances[0];
    const events = ["run_started", "task_update", "worker_completed", "run_completed"] as const;
    for (const t of events) {
      es.emitNamed(t, JSON.stringify({ type: t }));
    }

    expect(received).toHaveLength(events.length);
    for (let i = 0; i < events.length; i++) {
      expect(received[i].type).toBe(events[i]);
    }
  });

  /**
   * Characterizes that SSE comments (keep-alive) are silently absorbed by
   * EventSource and therefore do not fire onMessage.
   * In this mock emitComment() does nothing, which reflects the browser's
   * WHATWG-compliant behavior.
   */
  it("SSE comment (keep-alive) does not fire onMessage", () => {
    const received: unknown[] = [];
    renderHook(() =>
      useEventStream({
        url: "/api/events",
        onMessage: (msg) => received.push(msg),
      }),
    );

    const es = WhatwgFaithfulEventSource.instances[0];
    // Equivalent to `: keep-alive\n\n` — EventSource does not fire any listener
    es.emitComment();

    expect(received).toHaveLength(0);
  });

  /**
   * Unnamed frames (no event: field) are ignored because no 'message' listener
   * is registered, and onMessage is not called.
   * The backend never sends this format (only named events are used).
   * Keep-alive is sent as an SSE comment (`: ...`), which also fires no listener.
   */
  it("unnamed frame (data: only) is ignored and onMessage is not called", () => {
    const received: { type: string; data: unknown }[] = [];
    renderHook(() =>
      useEventStream({
        url: "/api/events",
        onMessage: (msg) => received.push(msg),
      }),
    );

    const es = WhatwgFaithfulEventSource.instances[0];
    es.emitUnnamed(JSON.stringify({ payload: "hello" }));

    expect(received).toHaveLength(0);
  });

  /**
   * Even when named events and unnamed frames are interleaved, only named
   * events are dispatched. Unnamed frames are ignored because no 'message'
   * listener exists.
   */
  it("when named events and unnamed frames are mixed, only named events are dispatched", () => {
    const received: { type: string }[] = [];
    renderHook(() =>
      useEventStream({
        url: "/api/events",
        onMessage: (msg) => received.push(msg as { type: string }),
      }),
    );

    const es = WhatwgFaithfulEventSource.instances[0];
    es.emitNamed("task_update", JSON.stringify({ type: "task_update" }));
    es.emitComment(); // keep-alive — no fire
    es.emitUnnamed(JSON.stringify({ payload: "ping" })); // unnamed → ignored
    es.emitNamed("run_completed", JSON.stringify({ type: "run_completed" }));

    // only named×2 are dispatched; unnamed×1 + comment×1 are ignored
    expect(received).toHaveLength(2);
    expect(received[0].type).toBe("task_update");
    expect(received[1].type).toBe("run_completed");
  });

  /**
   * Confirms that every named event type in the allow-list reaches onMessage.
   * If any type is omitted from registration and goes through the 'message'
   * listener instead, the type would change and be detectable.
   */
  it.each([
    "run_started",
    "run_completed",
    "run_failed",
    "run_stopped",
    "task_update",
    "worker_started",
    "worker_completed",
    "token",
    "diff_update",
    "manager_turn_started",
    "manager_message",
    "delegation",
    "evaluator_started",
    "pause_effective",
    "user_input_requested",
    "user_input_resolved",
    "token_usage",
    "epic_status_changed",
    "epic_merged",
    "epic_merge_progress",
  ] as const)("allow-list named event '%s' is dispatched once with the correct type", (eventType) => {
    const received: { type: string; data: unknown }[] = [];
    renderHook(() =>
      useEventStream({
        url: "/api/events",
        onMessage: (msg) => received.push(msg),
      }),
    );

    const es = WhatwgFaithfulEventSource.instances[0];
    es.emitNamed(eventType, JSON.stringify({ type: eventType }));

    expect(received).toHaveLength(1);
    expect(received[0].type).toBe(eventType);
  });
});

// ---------------------------------------------------------------------------
// Characterization tests for reconnect logic
//
// Target behavior:
//   - CONNECTING error → delegate to browser built-in retry (no manual reconnect)
//   - CLOSED error → manual backoff reconnect (initial retryMs=3000, cap 30000)
//   - open event resets retryCount (backoff returns to 3000)
// ---------------------------------------------------------------------------
describe("Reconnect logic: CLOSED/CONNECTING errors and exponential backoff", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  /**
   * A CONNECTING error delegates to the browser built-in retry, so no
   * new EventSource is created manually.
   */
  it("CONNECTING error does not trigger manual reconnect (delegated to browser)", () => {
    const onError = vi.fn();
    renderHook(() =>
      useEventStream({
        url: "/api/events",
        onMessage: () => {},
        onError,
      }),
    );

    const es = WhatwgFaithfulEventSource.instances[0];
    es.emitError(WhatwgFaithfulEventSource.CONNECTING);

    // No new EventSource is created even after sufficient time elapses
    vi.advanceTimersByTime(10_000);

    expect(WhatwgFaithfulEventSource.instances).toHaveLength(1);
    // onError callback must be called
    expect(onError).toHaveBeenCalledTimes(1);
  });

  /**
   * A CLOSED error triggers manual reconnect after the initial retryMs=3000ms.
   * Also confirms that reconnect has not yet happened at 2999ms.
   */
  it("CLOSED error triggers manual reconnect after backoff (3000ms)", () => {
    renderHook(() =>
      useEventStream({
        url: "/api/events",
        onMessage: () => {},
      }),
    );

    const es = WhatwgFaithfulEventSource.instances[0];
    es.emitError(WhatwgFaithfulEventSource.CLOSED);

    // Not yet reconnected at 2999ms
    vi.advanceTimersByTime(2999);
    expect(WhatwgFaithfulEventSource.instances).toHaveLength(1);

    // A new EventSource is created upon reaching 3000ms
    vi.advanceTimersByTime(1);
    expect(WhatwgFaithfulEventSource.instances).toHaveLength(2);
  });

  /**
   * Exponential backoff applies when consecutive CLOSED errors occur without
   * an intervening open. 1st: 3000ms, 2nd: 6000ms, 3rd: 12000ms.
   */
  it("exponential backoff: delay doubles on consecutive CLOSED errors without open", () => {
    renderHook(() =>
      useEventStream({
        url: "/api/events",
        onMessage: () => {},
      }),
    );

    // 1st CLOSED → instances=2 after 3000ms
    WhatwgFaithfulEventSource.instances[0].emitError(WhatwgFaithfulEventSource.CLOSED);
    vi.advanceTimersByTime(3000);
    expect(WhatwgFaithfulEventSource.instances).toHaveLength(2);

    // 2nd CLOSED → instances=3 after 6000ms (still 2 at 5999ms)
    WhatwgFaithfulEventSource.instances[1].emitError(WhatwgFaithfulEventSource.CLOSED);
    vi.advanceTimersByTime(5999);
    expect(WhatwgFaithfulEventSource.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(WhatwgFaithfulEventSource.instances).toHaveLength(3);

    // 3rd CLOSED → instances=4 after 12000ms
    WhatwgFaithfulEventSource.instances[2].emitError(WhatwgFaithfulEventSource.CLOSED);
    vi.advanceTimersByTime(11999);
    expect(WhatwgFaithfulEventSource.instances).toHaveLength(3);
    vi.advanceTimersByTime(1);
    expect(WhatwgFaithfulEventSource.instances).toHaveLength(4);
  });

  /**
   * An open event resets retryCount, so the next CLOSED error starts backoff
   * from the initial 3000ms again.
   *
   * Scenario:
   *   instances[0] open → onOpen called (hasOpened=true)
   *   instances[0] CLOSED → instances=2 after 3000ms
   *   instances[1] open → onReconnect called (2nd open) & retryCount reset
   *   instances[1] CLOSED → already reset, so 3000ms (not 6000ms)
   */
  it("open resets backoff: next CLOSED uses 3000ms not 6000ms", () => {
    const onOpen = vi.fn();
    const onReconnect = vi.fn();
    renderHook(() =>
      useEventStream({
        url: "/api/events",
        onMessage: () => {},
        onOpen,
        onReconnect,
      }),
    );

    // instances[0] first open → onOpen called, hasOpened=true
    WhatwgFaithfulEventSource.instances[0].emitOpen();
    expect(onOpen).toHaveBeenCalledTimes(1);

    // instances[0] CLOSED → instances=2 after 3000ms (retryCount=1)
    WhatwgFaithfulEventSource.instances[0].emitError(WhatwgFaithfulEventSource.CLOSED);
    vi.advanceTimersByTime(3000);
    expect(WhatwgFaithfulEventSource.instances).toHaveLength(2);

    // instances[1] open → retryCount reset & onReconnect called (2nd open)
    WhatwgFaithfulEventSource.instances[1].emitOpen();
    expect(onReconnect).toHaveBeenCalledTimes(1);

    // instances[1] CLOSED again → already reset, so 3000ms (retryCount=0 → delay=3000)
    WhatwgFaithfulEventSource.instances[1].emitError(WhatwgFaithfulEventSource.CLOSED);

    // Not yet reconnected at 2999ms (also proves it's not 6000ms)
    vi.advanceTimersByTime(2999);
    expect(WhatwgFaithfulEventSource.instances).toHaveLength(2);

    // Reaches 3000ms → instances=3
    vi.advanceTimersByTime(1);
    expect(WhatwgFaithfulEventSource.instances).toHaveLength(3);
  });

  /**
   * The backoff cap is 30000ms (30 seconds).
   * After the first open (onOpen fires on instances[0]), repeat CLOSED 5 times
   * to confirm the delay caps at 3000→6000→12000→24000→30000(cap).
   */
  it("backoff cap 30s: hits the cap after 5 CLOSED errors", () => {
    const onOpen = vi.fn();
    renderHook(() =>
      useEventStream({
        url: "/api/events",
        onMessage: () => {},
        onOpen,
      }),
    );

    // onOpen must be called on first open
    WhatwgFaithfulEventSource.instances[0].emitOpen();
    expect(onOpen).toHaveBeenCalledTimes(1);

    // retryCount was reset by open, so 1st CLOSED is 3000ms
    WhatwgFaithfulEventSource.instances[0].emitError(WhatwgFaithfulEventSource.CLOSED);
    vi.advanceTimersByTime(3000);
    expect(WhatwgFaithfulEventSource.instances).toHaveLength(2);

    // 2nd CLOSED → 6000ms
    WhatwgFaithfulEventSource.instances[1].emitError(WhatwgFaithfulEventSource.CLOSED);
    vi.advanceTimersByTime(6000);
    expect(WhatwgFaithfulEventSource.instances).toHaveLength(3);

    // 3rd CLOSED → 12000ms
    WhatwgFaithfulEventSource.instances[2].emitError(WhatwgFaithfulEventSource.CLOSED);
    vi.advanceTimersByTime(12000);
    expect(WhatwgFaithfulEventSource.instances).toHaveLength(4);

    // 4th CLOSED → 24000ms (before cap)
    WhatwgFaithfulEventSource.instances[3].emitError(WhatwgFaithfulEventSource.CLOSED);
    vi.advanceTimersByTime(23999);
    expect(WhatwgFaithfulEventSource.instances).toHaveLength(4);
    vi.advanceTimersByTime(1);
    expect(WhatwgFaithfulEventSource.instances).toHaveLength(5);

    // 5th CLOSED → 30000ms (cap; stops at 30000 not 48000)
    WhatwgFaithfulEventSource.instances[4].emitError(WhatwgFaithfulEventSource.CLOSED);
    vi.advanceTimersByTime(29999);
    expect(WhatwgFaithfulEventSource.instances).toHaveLength(5);
    vi.advanceTimersByTime(1);
    expect(WhatwgFaithfulEventSource.instances).toHaveLength(6);
  });
});
