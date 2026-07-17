"use client";

import { useEffect, useRef } from "react";
import type { RunEvent } from "@/lib/api/endpoints";

/**
 * Type utility that derives the set of `type` literals from the RunEvent union.
 * By enumerating as a keyed map of Record<RunEventType, true>,
 * adding a new member to the RunEvent union causes a compile error on missing keys,
 * forcing compile-time detection of forgotten addEventListener registrations.
 */
type RunEventType = RunEvent["type"];

/** A single event received from EventSource */
export interface SseMessage<T = unknown> {
  type: string;
  data: T;
}

export interface UseEventStreamOptions<T = unknown> {
  /** SSE endpoint URL (null/undefined to skip connection) */
  url: string | null | undefined;
  /** Handler for received events */
  onMessage: (msg: SseMessage<T>) => void;
  /** Optional callback when connection is established. Called after the EventSource open event. First connection only. */
  onOpen?: () => void;
  /**
   * Optional callback on reconnection. Called on the second and subsequent open events.
   * Because the backend resends the token backfill on each reconnection,
   * use this callback to clear the live buffer and prevent duplicates.
   */
  onReconnect?: () => void;
  /** Optional callback on connection error */
  onError?: (ev: Event) => void;
  /** Auto-reconnect interval in ms (default 3000) */
  retryMs?: number;
  /**
   * Close the stream once the page has been hidden this long, and reconnect
   * (through the normal onReconnect path) when it becomes visible again.
   * Chrome caps HTTP/1.1 at 6 concurrent connections PER ORIGIN ACROSS TABS,
   * so a few background tabs each holding 2-3 EventSources starve every
   * fetch in every tab — hidden tabs must release their sockets.
   * Default 30000; pass 0 to keep the stream open while hidden.
   */
  hiddenGraceMs?: number;
}

/**
 * Generic SSE hook based on EventSource.
 * - Parses `event: <type>` + `data: <json>` format and passes it to the handler.
 * - Does not connect when url is null/undefined.
 * - Auto-closes when the component unmounts.
 * - Delegates to the browser's built-in auto-reconnect while EventSource is in CONNECTING state.
 *   Manual exponential backoff reconnect (capped at 30 seconds) is triggered only when CLOSED (browser gave up).
 *   Resets retryCount on successful connection.
 * - Suspends (closes) the stream after the page has been hidden for hiddenGraceMs
 *   and reconnects on visibility — hidden tabs must not hold sockets from the
 *   per-origin connection pool (6 for HTTP/1.1, shared across tabs).
 */
export function useEventStream<T = unknown>({
  url,
  onMessage,
  onOpen,
  onReconnect,
  onError,
  retryMs = 3000,
  hiddenGraceMs = 30_000,
}: UseEventStreamOptions<T>): void {
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;
  const onOpenRef = useRef(onOpen);
  onOpenRef.current = onOpen;
  const onReconnectRef = useRef(onReconnect);
  onReconnectRef.current = onReconnect;
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  useEffect(() => {
    if (!url) return;

    let es: EventSource | undefined;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let hiddenTimer: ReturnType<typeof setTimeout> | null = null;
    let closed = false;
    // True while the stream is intentionally down because the page is hidden.
    let suspended = false;
    // Distinguishes first connection from reconnection. false on first open, true thereafter.
    let hasOpened = false;
    // Counter for exponential backoff. Reset on successful connection.
    let retryCount = 0;
    const maxRetryMs = 30_000;
    const suspendEnabled = hiddenGraceMs > 0;

    /** Parse ev.data as JSON and forward to onMessage; silently drops malformed data. */
    function parseAndDispatch(ev: MessageEvent, eventType: string): void {
      let parsed: T;
      try {
        parsed = JSON.parse(ev.data) as T;
      } catch {
        return;
      }
      onMessageRef.current({ type: eventType, data: parsed });
    }

    function connect() {
      if (closed || suspended) return;
      es = new EventSource(url as string);

      // On connection established: call onOpen for the first connection, onReconnect for reconnections.
      // Because the backend resends the token backfill on each reconnection,
      // clear the live buffer in onReconnect to prevent double rendering.
      // Reset retryCount on successful connection to reinitialize backoff.
      es.addEventListener("open", (() => {
        retryCount = 0;
        if (!hasOpened) {
          hasOpened = true;
          onOpenRef.current?.();
        } else {
          onReconnectRef.current?.();
        }
      }) as EventListener);

      // Backend SSE format: `event: <type>\ndata: <json>\n\n`
      // The generic message event (unnamed) is not used.
      // Types without a registered named event listener are ignored even when keep-alive frames arrive.
      // All real frames are covered by the named event listeners, so there is no impact on consumers.

      // Named event listeners for cases where the backend sends `event: run_started` etc.
      //
      // B1 recurrence prevention: enumerate all members of the RunEvent union as a keyed map of Record<RunEventType, true>.
      // Adding a new member to the RunEvent union causes a compile error on missing keys,
      // forcing compile-time detection of forgotten addEventListener registrations.
      // (token_usage / budget_exceeded are UsageStream-only and not part of RunEvent, so they are handled separately)
      const _runEventMap: Record<RunEventType, true> = {
        run_preparing: true,
        run_started: true,
        run_completed: true,
        run_failed: true,
        run_stopped: true,
        run_paused: true,
        run_resumed: true,
        task_update: true,
        worker_started: true,
        worker_completed: true,
        worker_failed: true,
        eval_result: true,
        token: true,
        tool_call: true,
        tool_result: true,
        diff_update: true,
        manager_turn_started: true,
        manager_message: true,
        delegation: true,
        evaluator_started: true,
        pause_effective: true,
        your_turn: true,
        your_turn_ended: true,
        user_message_committed: true,
        sensitive_file_written: true,
        epic_merged: true,
      };
      // UsageStream-only events (not part of RunEvent)
      const _usageEventTypes = ["token_usage", "budget_exceeded"] as const;

      // Project-level lifecycle events (user status toggle + arbiter merge)
      const _projectEventTypes = ["epic_status_changed", "epic_merge_progress"] as const;

      const eventTypes: string[] = [
        ...Object.keys(_runEventMap),
        ..._usageEventTypes,
        ..._projectEventTypes,
      ];

      for (const eventType of eventTypes) {
        es.addEventListener(eventType, ((ev: MessageEvent) => {
          parseAndDispatch(ev, eventType);
        }) as EventListener);
      }

      es.onerror = (ev) => {
        onErrorRef.current?.(ev);
        // The browser auto-reconnects EventSource while readyState===CONNECTING.
        // To avoid duplication, only trigger manual backoff reconnection when CLOSED (browser gave up).
        if (closed || suspended) return;
        if (es && es.readyState === EventSource.CLOSED) {
          const delay = Math.min(retryMs * 2 ** retryCount, maxRetryMs);
          retryCount += 1;
          es.close();
          retryTimer = setTimeout(connect, delay);
        }
      };
    }

    // Hidden-tab suspension: release the socket after a grace period so
    // parked tabs cannot starve the per-origin connection pool; quick tab
    // switches within the grace window keep the stream (no backfill churn).
    // Resuming goes through connect() → the open event fires onReconnect,
    // so consumers dedupe the backend's backfill exactly like any reconnect.
    function suspend() {
      hiddenTimer = null;
      suspended = true;
      if (retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
      es?.close();
    }

    function onVisibilityChange() {
      if (closed) return;
      if (document.hidden) {
        hiddenTimer ??= setTimeout(suspend, hiddenGraceMs);
      } else {
        if (hiddenTimer) {
          clearTimeout(hiddenTimer);
          hiddenTimer = null;
        }
        if (suspended) {
          suspended = false;
          retryCount = 0;
          connect();
        }
      }
    }

    if (suspendEnabled) {
      document.addEventListener("visibilitychange", onVisibilityChange);
      // A tab opened in the background (e.g. ctrl+click) starts hidden and
      // never fired visibilitychange — start its grace countdown now.
      if (document.hidden) hiddenTimer = setTimeout(suspend, hiddenGraceMs);
    }

    connect();

    return () => {
      closed = true;
      if (suspendEnabled) document.removeEventListener("visibilitychange", onVisibilityChange);
      if (retryTimer) clearTimeout(retryTimer);
      if (hiddenTimer) clearTimeout(hiddenTimer);
      es?.close();
    };
  }, [url, retryMs, hiddenGraceMs]);
}
