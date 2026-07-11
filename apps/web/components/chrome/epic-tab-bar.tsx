"use client";

/**
 * EpicTabBar — 4 tabs (Conversation / Tasks / Diff / Docs)
 *
 * Uses the generic TabBar; badges are fetched via client query.
 * design-language §EpicTabBar: LABEL uppercase 44px, active = white + white under-tick (current = white), count = .data mono.
 */

import { useQuery } from "@tanstack/react-query";
import { TabBar } from "@/components/ui/tab-bar";
import { getGitDiffSummary, getTasks } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { resolveActiveManagerThreadId } from "@/lib/epic-utils";
import { useT } from "@/lib/i18n/provider";
import { useEpicRun } from "./epic-run-context";

export function EpicTabBar() {
  const { projectId, epicId, epic, activityState } = useEpicRun();
  const t = useT();

  const base = `/projects/${projectId}/epics/${epicId}`;

  // Resolve the active trial id (activityState.activeTrialId takes priority).
  // Passing an empty array as the second argument to resolveActiveManagerThreadId is intentional:
  // threads are not fetched here — fall back only on epic.active_thread_id.
  // If threads are needed, add a useQuery at the call site.
  const managerThreadId = activityState.activeTrialId ?? resolveActiveManagerThreadId(epic, []);

  // Tasks badge: done/total
  const { data: tasksFile } = useQuery({
    queryKey: queryKeys.tasks.get(projectId, epicId),
    queryFn: () => getTasks(projectId, epicId),
    staleTime: 30_000,
  });

  const tasksDone =
    tasksFile?.progress?.done ?? tasksFile?.tasks?.filter((t) => t.status === "done").length ?? 0;
  const tasksTotal = tasksFile?.progress?.total ?? tasksFile?.tasks?.length ?? 0;

  // Diff badge: +added −removed
  const { data: diffSummary } = useQuery({
    queryKey: queryKeys.git.diffSummary(projectId, epicId, "working"),
    queryFn: () => getGitDiffSummary(projectId, epicId, "working"),
    staleTime: 30_000,
  });

  const diffAdded = diffSummary?.repos?.reduce((s, r) => s + (r.added ?? 0), 0) ?? 0;
  const diffRemoved = diffSummary?.repos?.reduce((s, r) => s + (r.deleted ?? 0), 0) ?? 0;
  const hasDiff = diffAdded > 0 || diffRemoved > 0;

  const items = [
    {
      href: `${base}/threads/${managerThreadId}`,
      label: t("epic.tabs.conversation"),
      segment: "threads",
    },
    {
      href: `${base}/tasks`,
      label: t("epic.tabs.tasks"),
      segment: "tasks",
      badge:
        tasksTotal > 0 ? (
          <span>
            {tasksDone}/{tasksTotal}
          </span>
        ) : undefined,
    },
    {
      href: `${base}/diff`,
      label: t("epic.tabs.diff"),
      segment: "diff",
      badge: hasDiff ? (
        <span>
          <span style={{ color: "var(--color-added)" }}>+{diffAdded}</span>{" "}
          <span style={{ color: "var(--color-removed)" }}>−{diffRemoved}</span>
        </span>
      ) : undefined,
    },
    {
      href: `${base}/docs`,
      label: t("epic.tabs.docs"),
      segment: "docs",
    },
  ];

  return (
    <div className="bg-surface">
      <TabBar items={items} />
    </div>
  );
}
