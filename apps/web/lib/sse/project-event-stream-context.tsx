"use client";

/**
 * ProjectEventStreamContext — Single-connection SSE Provider scoped to a project.
 *
 * Opens exactly one EventSource to /api/projects/{p}/events and manages
 * a set of onMessage callbacks as subscribers.
 * By mounting in ProjectChromeShell, this guarantees that multiple hooks
 * (useProjectNotifications / useMergeProgress) within the same project view
 * do not open duplicate connections to the same endpoint.
 */

import { createContext, useCallback, useContext, useRef } from "react";
import type {
  EpicMergedEvent,
  EpicMergeProgressEvent,
  EpicStatusChangedEvent,
  ProjectLifecycleEvent,
  SensitiveFileWrittenEvent,
  YourTurnEndedEvent,
  YourTurnEvent,
} from "@/lib/api/endpoints";
import type { SseMessage } from "./use-event-stream";
import { useEventStream } from "./use-event-stream";

// ---- Type definitions ----

/**
 * Union of all event types that can arrive via the project-level SSE.
 * your_turn / your_turn_ended are the "your turn" signals:
 * a conversation run parked in "waiting" / left it — used for live board badges.
 */
export type ProjectStreamEvent =
  | ProjectLifecycleEvent
  | EpicMergeProgressEvent
  | EpicStatusChangedEvent
  | EpicMergedEvent
  | SensitiveFileWrittenEvent
  | YourTurnEvent
  | YourTurnEndedEvent;

/** Guard: checks whether SSE data has the shape of a ProjectStreamEvent */
function isProjectStreamEvent(data: unknown): data is ProjectStreamEvent {
  return (
    data !== null &&
    typeof data === "object" &&
    "type" in data &&
    typeof (data as { type: unknown }).type === "string"
  );
}

type MessageHandler = (msg: SseMessage<ProjectStreamEvent>) => void;

interface ProjectEventStreamContextValue {
  /** Register a handler. The return value is an unsubscribe function. */
  subscribe: (handler: MessageHandler) => () => void;
  /**
   * Register a reconnect handler. The project stream has NO replay buffer on
   * the backend — events published while the connection was down (network
   * blip, hidden-tab suspension) are gone. Consumers holding SSE-accumulated
   * state must resync from REST here.
   */
  subscribeReconnect: (handler: () => void) => () => void;
}

const ProjectEventStreamContext = createContext<ProjectEventStreamContextValue | null>(null);

// ---- Provider ----

interface ProjectEventStreamProviderProps {
  projectId: string;
  children?: React.ReactNode;
}

/**
 * ProjectEventStreamProvider — Opens exactly one SSE connection per project.
 * By placing it in ProjectChromeShell, all hooks within the same project layout
 * share this single connection.
 */
export function ProjectEventStreamProvider({
  projectId,
  children,
}: ProjectEventStreamProviderProps) {
  const handlersRef = useRef<Set<MessageHandler>>(new Set());
  const reconnectHandlersRef = useRef<Set<() => void>>(new Set());

  const url = `/api/projects/${projectId}/events`;

  const onMessage = useCallback(({ data }: SseMessage<unknown>) => {
    if (!isProjectStreamEvent(data)) return;
    const msg: SseMessage<ProjectStreamEvent> = { type: data.type, data };
    for (const handler of handlersRef.current) {
      handler(msg);
    }
  }, []);

  // The backend has no replay for this stream: anything published while the
  // connection was down is lost. Fan the reconnect out so consumers with
  // SSE-accumulated state (merge progress) can resync from REST.
  const onReconnect = useCallback(() => {
    for (const handler of reconnectHandlersRef.current) {
      handler();
    }
  }, []);

  useEventStream<unknown>({
    url,
    onMessage,
    onReconnect,
  });

  const subscribe = useCallback((handler: MessageHandler) => {
    handlersRef.current.add(handler);
    return () => {
      handlersRef.current.delete(handler);
    };
  }, []);

  const subscribeReconnect = useCallback((handler: () => void) => {
    reconnectHandlersRef.current.add(handler);
    return () => {
      reconnectHandlersRef.current.delete(handler);
    };
  }, []);

  return (
    <ProjectEventStreamContext value={{ subscribe, subscribeReconnect }}>
      {children}
    </ProjectEventStreamContext>
  );
}

// ---- Consumer hook ----

/**
 * useProjectEventStream — Subscription hook for ProjectEventStreamProvider.
 * Throws an exception if used outside the Provider (to catch configuration mistakes).
 */
export function useProjectEventStream(): ProjectEventStreamContextValue {
  const ctx = useContext(ProjectEventStreamContext);
  if (!ctx) {
    throw new Error("useProjectEventStream must be used within <ProjectEventStreamProvider>");
  }
  return ctx;
}
