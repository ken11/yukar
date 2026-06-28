"use client";

import { useSelectedLayoutSegments } from "next/navigation";
import { ProjectEventStreamProvider } from "@/lib/sse/project-event-stream-context";
import { ProjectHeader } from "./project-header";
import { ProjectTabBar } from "./project-tab-bar";

interface ProjectChromeShellProps {
  projectId: string;
  projectName: string;
  children: React.ReactNode;
}

/**
 * ProjectChromeShell — chrome container scoped to a project.
 *
 * Epic route detection: if the first segment of useSelectedLayoutSegments() is "epics"
 * and further segments follow (= epic detail route), header/tabs are not rendered.
 * /projects/[p]/epics (board index) does show tabs.
 *
 * Placing ProjectEventStreamProvider here allows ProjectHeader
 * (useProjectNotifications) and EpicsBoardClient → MergeProgressPanel
 * (useMergeProgress) to share the same EventSource.
 */
export function ProjectChromeShell({ projectId, projectName, children }: ProjectChromeShellProps) {
  const segments = useSelectedLayoutSegments();

  // segments examples:
  //   / (overview)       → []
  //   /epics             → ["epics"]
  //   /epics/[e]/...     → ["epics", "ep-1", "threads", "manager"]
  //   /settings          → ["settings"]
  //
  // epic detail route = "epics" followed by epicId segment
  const isEpicDetail = segments[0] === "epics" && segments.length > 1;

  if (isEpicDetail) {
    // On the epic route, render only children without header/tabs.
    // fix 1: propagate h-full so EpicShell's flex-col layout can occupy the viewport height.
    // ProjectEventStreamProvider is not needed (the epic detail page uses a separate SSE via use-run-activity)
    return <div className="h-full">{children}</div>;
  }

  return (
    // Non-epic route: manage scroll with overflow-y-auto (because the parent became overflow-hidden)
    <ProjectEventStreamProvider projectId={projectId}>
      <div className="flex h-full flex-col overflow-y-auto">
        <ProjectHeader projectId={projectId} projectName={projectName} />
        <ProjectTabBar projectId={projectId} />
        {/* 40px void — design-language §spacing/grid */}
        <div aria-hidden className="h-[var(--spacing-void,40px)]" />
        {children}
      </div>
    </ProjectEventStreamProvider>
  );
}
