"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useEpicRun } from "@/components/chrome/epic-run-context";
import { roleIcon } from "@/components/features/conversation/message-row";
import { ThreadTreePanel } from "@/components/features/epics/thread-tree-panel";
import { NewThreadModal } from "@/components/features/threads/new-thread-modal";
import { Icon } from "@/components/icon";
import type { ThreadEntry } from "@/lib/api/endpoints";
import { ApiError, archiveThread, extractDetail, listThreads } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { useT } from "@/lib/i18n/provider";

// ---------------------------------------------------------------------------
// Left pane: thread list
// ---------------------------------------------------------------------------

export function ThreadListPane({
  projectId,
  epicId,
  currentThreadId,
  initialThreads,
  onClose,
  variant = "drawer",
}: {
  projectId: string;
  epicId: string;
  currentThreadId: string;
  initialThreads: ThreadEntry[];
  /** Mobile: callback to close the panel when a thread is selected */
  onClose?: () => void;
  /**
   * "drawer" (default) = fixed 240px column with its own right border (mobile
   * overlay). "sidebar" = fills its parent, no border (the persistent desktop
   * EpicSidebar draws the .edge-v boundary itself).
   */
  variant?: "drawer" | "sidebar";
}) {
  const t = useT();
  const { activityState } = useEpicRun();

  // Subscribe the list — invalidation causes new trials to appear immediately
  const { data: threads = initialThreads } = useQuery({
    queryKey: queryKeys.threads.list(projectId, epicId),
    queryFn: () => listThreads(projectId, epicId),
    initialData: initialThreads,
    staleTime: 30_000,
  });

  // The thread list shows user-facing conversation threads: manager trials, any
  // human "user" threads, and reviewer conversations (the user replies to the
  // Reviewer here). Worker / evaluator / arbiter threads are system-generated and
  // already fully represented — with live status and hierarchy — in the Agent
  // State tree (ThreadTreePanel) below, so listing them here is redundant. They
  // remain reachable via the Agent State tree links and by URL.
  const listed = threads.filter(
    (t) => t.role === "manager" || t.role === "user" || t.role === "reviewer",
  );
  const activeThreads = listed.filter((t) => t.status !== "archived");
  const archivedThreads = listed.filter((t) => t.status === "archived");

  const isSidebar = variant === "sidebar";

  return (
    <nav
      aria-label="Threads"
      className={cn(
        "flex h-full flex-col overflow-y-auto bg-surface-container-low",
        isSidebar ? "w-full" : "w-[240px] shrink-0",
      )}
      style={isSidebar ? undefined : { borderRight: "1px solid var(--color-outline-variant)" }}
    >
      {/* New trial + continue-on-branch. Sidebar: a quiet "Trials" label +
          full-width ghost rows (no competing filled button, no hard border).
          Drawer (mobile): the original filled/outline buttons. */}
      {isSidebar ? (
        <div className="flex shrink-0 flex-col px-2 pt-3">
          <p className="px-2 pb-1 font-mono text-[10px] uppercase tracking-wider text-outline">
            {t("common.trialsSection")}
          </p>
          <NewThreadModal projectId={projectId} epicId={epicId} compact />
          <NewThreadModal projectId={projectId} epicId={epicId} variant="sameBranch" compact />
        </div>
      ) : (
        <div
          className="flex shrink-0 flex-col gap-1.5 px-3 py-2"
          style={{ borderBottom: "1px solid var(--color-outline-variant)" }}
        >
          <NewThreadModal projectId={projectId} epicId={epicId} />
          <NewThreadModal projectId={projectId} epicId={epicId} variant="sameBranch" />
        </div>
      )}

      {/* Active thread list */}
      <div className="flex flex-col">
        {activeThreads.map((thread) => (
          <ThreadRow
            key={thread.id}
            thread={thread}
            projectId={projectId}
            epicId={epicId}
            currentThreadId={currentThreadId}
            threads={threads}
            threadRoleIcon={roleIcon}
            onClose={onClose}
          />
        ))}
      </div>

      {/* Archived trials (understated separate section) */}
      {archivedThreads.length > 0 && (
        <div
          className="mt-auto flex flex-col"
          style={{ borderTop: "1px solid var(--color-outline-variant)" }}
        >
          <p className="px-4 py-2 font-mono text-[10px] uppercase tracking-wider text-outline">
            {t("common.archiveArchivedSection")}
          </p>
          {archivedThreads.map((thread) => (
            <ThreadRow
              key={thread.id}
              thread={thread}
              projectId={projectId}
              epicId={epicId}
              currentThreadId={currentThreadId}
              threads={threads}
              threadRoleIcon={roleIcon}
              onClose={onClose}
              dimmed
            />
          ))}
        </div>
      )}

      {/* Live agent tree */}
      <section
        className="p-3"
        style={{ borderTop: "1px solid var(--color-outline-variant)" }}
        aria-label={t("conversation.agentStateHeading")}
      >
        <ThreadTreePanel
          treeState={activityState.treeState}
          projectId={projectId}
          epicId={epicId}
        />
      </section>
    </nav>
  );
}

// ---------------------------------------------------------------------------
// ThreadRow — individual thread row
// ---------------------------------------------------------------------------

function ThreadRow({
  thread,
  projectId,
  epicId,
  currentThreadId,
  threads,
  threadRoleIcon,
  onClose,
  dimmed,
}: {
  thread: ThreadEntry;
  projectId: string;
  epicId: string;
  currentThreadId: string;
  /** Full thread list (used to resolve the navigation target after archiving) */
  threads: ThreadEntry[];
  threadRoleIcon: Record<string, string>;
  onClose?: () => void;
  dimmed?: boolean;
}) {
  const t = useT();
  const qc = useQueryClient();
  const router = useRouter();
  const { activityState } = useEpicRun();
  const href = `/projects/${projectId}/epics/${epicId}/threads/${thread.id}`;
  const isActive = thread.id === currentThreadId;
  const isArchived = thread.status === "archived";
  const isManagerThread = thread.role === "manager";
  const [archiveError, setArchiveError] = useState<string | null>(null);

  const archiveMutation = useMutation({
    mutationFn: () => archiveThread(projectId, epicId, thread.id),
    onSuccess: () => {
      setArchiveError(null);
      qc.invalidateQueries({
        queryKey: queryKeys.threads.list(projectId, epicId),
      });
      // Even on successful archive, epic.active_thread_id may change,
      // so invalidate epics.detail to refresh EpicShell's liveEpic.
      qc.invalidateQueries({
        queryKey: queryKeys.epics.detail(projectId, epicId),
      });

      // If the currently viewed thread was just archived, navigate away immediately to
      // prevent a flash where the stale prop incorrectly sets isActiveTrial=true and shows the composer.
      if (thread.id === currentThreadId) {
        // Destination: activeTrialId (≠ self) → first non-archived manager → otherwise router.refresh only
        const mgrId = activityState.activeTrialId;
        const destination =
          mgrId && mgrId !== thread.id
            ? mgrId
            : (threads.find(
                (th) => th.role === "manager" && th.status !== "archived" && th.id !== thread.id,
              )?.id ?? null);

        if (destination) {
          router.push(`/projects/${projectId}/epics/${epicId}/threads/${destination}`);
          return; // push re-fetches the RSC, so refresh is not needed
        }
      }

      // Also re-fetch the layout RSC (once only, not polling).
      router.refresh();
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        // Show the backend's real reason (active run, etc.) rather than a fixed
        // guess (DESIGN.md: window.alert is forbidden — use an inline banner).
        setArchiveError(extractDetail(err) ?? t("common.archiveStopFirst"));
      } else {
        setArchiveError(err instanceof Error ? err.message : "Archive failed");
      }
    },
  });

  return (
    <div className="flex flex-col">
      <div className="group relative flex items-center">
        <Link
          href={href}
          onClick={onClose}
          className={cn(
            "flex min-w-0 flex-1 items-center gap-2.5 px-4 py-2.5 text-body-sm transition-colors",
            "min-h-[44px]",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-inset",
            dimmed ? "opacity-40" : undefined,
            isActive
              ? "font-medium text-on-surface"
              : "text-on-surface-variant hover:bg-surface-container hover:text-on-surface",
          )}
          style={
            isActive
              ? {
                  backgroundColor: "var(--color-surface-container-highest)",
                  boxShadow: "inset 2px 0 0 0 var(--color-on-surface)",
                }
              : undefined
          }
          aria-current={isActive ? "page" : undefined}
        >
          <Icon
            name={threadRoleIcon[thread.role] ?? "chat"}
            className="shrink-0 text-[14px] text-on-surface-variant"
            aria-hidden
          />
          <span className="min-w-0 truncate">{thread.title}</span>
        </Link>

        {/* Archive button — shown on hover for active manager trial rows only */}
        {isManagerThread && !isArchived && (
          <button
            type="button"
            onClick={(e) => {
              e.preventDefault();
              setArchiveError(null);
              archiveMutation.mutate();
            }}
            disabled={archiveMutation.isPending}
            aria-label={t("common.archive")}
            title={t("common.archive")}
            className={cn(
              "absolute right-2 flex h-6 w-6 shrink-0 items-center justify-center rounded transition-opacity",
              "text-outline hover:text-on-surface-variant",
              "opacity-0 group-hover:opacity-100 focus-visible:opacity-100",
              "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white",
              archiveMutation.isPending && "opacity-50",
            )}
          >
            <Icon
              name={archiveMutation.isPending ? "hourglass_empty" : "archive"}
              className="text-[14px]"
              aria-hidden
            />
          </button>
        )}
      </div>

      {/* Archive 409 inline error banner */}
      {archiveError && (
        <div
          className="mx-3 mb-1 flex items-start gap-2 rounded px-2 py-1.5 text-[11px] text-on-surface-variant"
          style={{ backgroundColor: "var(--color-surface-container-high)" }}
        >
          <span className="min-w-0 flex-1">{archiveError}</span>
          <button
            type="button"
            onClick={() => setArchiveError(null)}
            className="shrink-0 text-outline hover:text-on-surface focus-visible:outline-none"
            aria-label="Dismiss"
          >
            ×
          </button>
        </div>
      )}
    </div>
  );
}
