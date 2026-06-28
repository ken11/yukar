"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ProjectLifecycleEvent, SensitiveFileWrittenEvent } from "@/lib/api/endpoints";
import { playChime } from "@/lib/audio/chime";
import { useT } from "@/lib/i18n/provider";
import { useProjectEventStream } from "./project-event-stream-context";

export interface Notification {
  id: string;
  epicId: string;
  type:
    | "run_started"
    | "run_completed"
    | "run_failed"
    | "run_stopped"
    | "run_paused"
    | "run_resumed"
    | "sensitive_file_written";
  message: string;
  ts: number;
  read: boolean;
}

export interface UseProjectNotificationsReturn {
  notifications: Notification[];
  unreadCount: number;
  markAllRead: () => void;
  clearAll: () => void;
}

let notifCounter = 0;
function nextId() {
  notifCounter += 1;
  return `notif-${notifCounter}`;
}

function makeMessage(
  event: ProjectLifecycleEvent | SensitiveFileWrittenEvent,
  epicId: string,
  t: (path: string) => string,
): string {
  const fmt = (key: string) => t(key).replace("{epicId}", epicId);
  switch (event.type) {
    case "run_started":
      return fmt("notifications.runStarted");
    case "run_completed":
      return fmt("notifications.runCompleted");
    case "run_failed":
      return fmt("notifications.runFailed");
    case "run_stopped":
      return fmt("notifications.runStopped");
    case "run_paused":
      return fmt("notifications.runPaused");
    case "run_resumed":
      return fmt("notifications.runResumed");
    case "sensitive_file_written": {
      const kindLabel = t(`notifications.sensitiveFileKind.${event.kind}`);
      return t("notifications.sensitiveFileWritten")
        .replace("{kind}", kindLabel)
        .replace("{name}", event.name);
    }
    default:
      return fmt("notifications.runEvent");
  }
}

/** Set of type literal strings for notification-worthy events */
const NOTIFICATION_TYPES = new Set([
  "run_started",
  "run_completed",
  "run_failed",
  "run_stopped",
  "run_paused",
  "run_resumed",
  "sensitive_file_written",
]);

/**
 * Subscribes to project-scoped SSE and accumulates lifecycle events
 * in an in-memory notification list.
 *
 * Demuxes from the single EventSource opened by ProjectEventStreamProvider
 * (placed in ProjectChromeShell). The projectId argument is retained for
 * backward compatibility but is ignored since the connection is managed by the Provider.
 *
 * Toast and chime are not triggered here but delegated externally via the onToast callback
 * (for SSR/hydration safety).
 */
export function useProjectNotifications(
  _projectId: string | undefined,
  onToast?: (notif: Notification) => void,
): UseProjectNotificationsReturn {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const onToastRef = useRef(onToast);
  onToastRef.current = onToast;

  // Keep the latest translator in a ref so the SSE callback (deps: [subscribe])
  // always formats messages with the current locale without re-subscribing.
  const t = useT();
  const tRef = useRef(t);
  tRef.current = t;

  const { subscribe } = useProjectEventStream();

  useEffect(() => {
    return subscribe(({ data }) => {
      // Process only notification-worthy events
      if (!NOTIFICATION_TYPES.has(data.type)) return;

      // type is in NOTIFICATION_TYPES = safe to treat as lifecycle or sensitive_file_written event
      const event = data as ProjectLifecycleEvent | SensitiveFileWrittenEvent;
      const epicId = (event as { epic_id?: string }).epic_id ?? "";

      const notif: Notification = {
        id: nextId(),
        epicId,
        type: event.type,
        message: makeMessage(event, epicId, tRef.current),
        ts: Date.now(),
        read: false,
      };

      setNotifications((prev) => [notif, ...prev].slice(0, 50));

      if (event.type === "run_completed" || event.type === "run_failed") {
        onToastRef.current?.(notif);
        playChime(event.type === "run_completed" ? "success" : "error");
      }
    });
  }, [subscribe]);

  const markAllRead = useCallback(() => {
    setNotifications((prev) => prev.map((n) => ({ ...n, read: true })));
  }, []);

  const clearAll = useCallback(() => {
    setNotifications([]);
  }, []);

  const unreadCount = notifications.filter((n) => !n.read).length;

  return { notifications, unreadCount, markAllRead, clearAll };
}
