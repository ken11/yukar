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
  UserInputRequestedEvent,
  UserInputResolvedEvent,
} from "@/lib/api/endpoints";
import type { SseMessage } from "./use-event-stream";
import { useEventStream } from "./use-event-stream";

// ---- Type definitions ----

/**
 * Union of all event types that can arrive via the project-level SSE.
 * user_input_requested / user_input_resolved are the "your turn" signals (P4):
 * a conversation run parked in "waiting" / left it — used for live board badges.
 */
export type ProjectStreamEvent =
  | ProjectLifecycleEvent
  | EpicMergeProgressEvent
  | EpicStatusChangedEvent
  | EpicMergedEvent
  | SensitiveFileWrittenEvent
  | UserInputRequestedEvent
  | UserInputResolvedEvent;

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

  const url = `/api/projects/${projectId}/events`;

  const onMessage = useCallback(({ data }: SseMessage<unknown>) => {
    if (!isProjectStreamEvent(data)) return;
    const msg: SseMessage<ProjectStreamEvent> = { type: data.type, data };
    for (const handler of handlersRef.current) {
      handler(msg);
    }
  }, []);

  useEventStream<unknown>({
    url,
    onMessage,
  });

  const subscribe = useCallback((handler: MessageHandler) => {
    handlersRef.current.add(handler);
    return () => {
      handlersRef.current.delete(handler);
    };
  }, []);

  return <ProjectEventStreamContext value={{ subscribe }}>{children}</ProjectEventStreamContext>;
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
