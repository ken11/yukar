"use client";

import { useQuery } from "@tanstack/react-query";
import { useEpicRun } from "@/components/chrome/epic-run-context";
import { RunCostBadge } from "@/components/features/usage/run-cost-badge";
import type { RunState, Task, TasksResponse } from "@/lib/api/endpoints";
import { getRunState, getTasks } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { useT } from "@/lib/i18n/provider";
import { PlanApprovalControl } from "./plan-approval-control";
import { TaskList } from "./task-list";

interface TasksPageClientProps {
  projectId: string;
  epicId: string;
  initialTasksFile: TasksResponse;
}

export function TasksPageClient({ projectId, epicId, initialTasksFile }: TasksPageClientProps) {
  const t = useT();
  const { data: tasksFile = initialTasksFile } = useQuery({
    queryKey: queryKeys.tasks.get(projectId, epicId),
    queryFn: () => getTasks(projectId, epicId),
    initialData: initialTasksFile,
    staleTime: 30_000,
  });

  // Read from EpicShell's single SSE subscription via context (double SSE is forbidden)
  const { activityState } = useEpicRun();
  const isRunning = activityState.runStatus === "running" || activityState.runStatus === "paused";

  // Obtain run_id from the TanStack Query cache (already patched by useRunActivity)
  const { data: runState } = useQuery<RunState>({
    queryKey: queryKeys.runState.get(projectId, epicId),
    queryFn: () => getRunState(projectId, epicId),
    staleTime: 60_000,
  });

  const tasks: Task[] = tasksFile.tasks ?? [];
  const done = tasksFile.progress?.done ?? tasks.filter((t) => t.status === "done").length;
  const total = tasksFile.progress?.total ?? tasks.length;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;

  return (
    <div className="p-6">
      <div className="mb-6 flex items-start justify-between">
        {/* flex-wrap: on narrow (mobile) widths the approval control wraps
            below the counters instead of overflowing the row. */}
        <div className="flex flex-wrap items-center gap-3">
          <p className="text-body-sm text-on-surface-variant">
            {done}/{total} completed
          </p>
          {/* Plan-approval state — a datum, not a card. Approval binds to the
              current plan snapshot: any plan change reverts this to unapproved. */}
          {tasks.length > 0 && (
            <span
              data-testid="plan-approval-status"
              className="flex items-center gap-1.5 font-mono text-[11px]"
              style={{
                color: tasksFile.plan_approved
                  ? "var(--color-light)"
                  : "var(--color-on-surface-variant)",
              }}
            >
              <span
                className="h-1.5 w-1.5 shrink-0 rounded-full"
                style={{
                  backgroundColor: tasksFile.plan_approved
                    ? "var(--color-light)"
                    : "var(--color-outline)",
                }}
                aria-hidden
              />
              {tasksFile.plan_approved ? t("tasks.planApproved") : t("tasks.planNotApproved")}
            </span>
          )}
          {/* Always-available approval lever — works from backend truth even
              when the conversation banner's conditions fail to line up. */}
          <PlanApprovalControl projectId={projectId} epicId={epicId} tasksFile={tasksFile} />
          {runState?.run_id && (
            <RunCostBadge
              projectId={projectId}
              epicId={epicId}
              runId={runState.run_id}
              enabled={isRunning}
            />
          )}
        </div>
        {total > 0 && (
          <div className="flex items-center gap-3">
            {/* progress bar — cyan is the single semantic color for progress */}
            <div className="h-1 w-40 overflow-hidden rounded-full bg-surface-container-highest">
              <div
                className="h-full rounded-full transition-all"
                style={{ width: `${pct}%`, backgroundColor: "var(--color-running)" }}
              />
            </div>
            <span className="data">{pct}%</span>
          </div>
        )}
      </div>

      <TaskList tasks={tasks} />
    </div>
  );
}
