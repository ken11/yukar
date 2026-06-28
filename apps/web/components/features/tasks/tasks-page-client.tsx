"use client";

import { useQuery } from "@tanstack/react-query";
import { useEpicRun } from "@/components/chrome/epic-run-context";
import { RunCostBadge } from "@/components/features/usage/run-cost-badge";
import type { RunState, Task, TasksFile } from "@/lib/api/endpoints";
import { getRunState, getTasks } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { TaskList } from "./task-list";

interface TasksPageClientProps {
  projectId: string;
  epicId: string;
  initialTasksFile: TasksFile;
}

export function TasksPageClient({ projectId, epicId, initialTasksFile }: TasksPageClientProps) {
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
        <div className="flex items-center gap-3">
          <p className="text-body-sm text-on-surface-variant">
            {done}/{total} completed
          </p>
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
