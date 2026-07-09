"use client";

/**
 * EpicRunContext — context that subscribes to SSE exactly once from EpicShell
 * and supplies { activityState, setPausePending, project, epic, projectId, epicId }
 * to all child components.
 *
 * Double EventSource is prohibited: only EpicShell may call useRunActivity.
 * Children read treeState etc. via useEpicRun().
 */

import { createContext, useContext } from "react";
import type { Epic, Project } from "@/lib/api/endpoints";
import type { RunActivityState } from "@/lib/sse/use-run-activity";

export interface EpicRunContextValue {
  projectId: string;
  epicId: string;
  project: Project | null;
  epic: Epic | null;
  activityState: RunActivityState;
  setPausePending: (v: boolean) => void;
  clearLiveBuffer: (threadId: string) => void;
  /**
   * Mobile only: collapse the epic header + tab bar while the user scrolls down
   * a conversation (scroll up restores them). Desktop ignores this — the CSS
   * classes it drives only apply below md. Called from ThreadChatInner's scroll.
   */
  setMobileChromeHidden: (v: boolean) => void;
}

const EpicRunContext = createContext<EpicRunContextValue | null>(null);

export function EpicRunProvider({
  value,
  children,
}: {
  value: EpicRunContextValue;
  children: React.ReactNode;
}) {
  return <EpicRunContext.Provider value={value}>{children}</EpicRunContext.Provider>;
}

export function useEpicRun(): EpicRunContextValue {
  const ctx = useContext(EpicRunContext);
  if (!ctx) {
    throw new Error("useEpicRun must be used within EpicRunProvider (inside EpicShell)");
  }
  return ctx;
}
