import { TasksPageClient } from "@/components/features/tasks/tasks-page-client";
import { getTasks } from "@/lib/api/endpoints";

export default async function TasksPage({ params }: { params: Promise<{ p: string; e: string }> }) {
  const { p, e } = await params;

  const tasksFile = await getTasks(p, e).catch(() => ({
    tasks: [],
    progress: { done: 0, total: 0 },
    plan_hash: "",
    approved_hash: null,
    plan_approved: false,
  }));

  return <TasksPageClient projectId={p} epicId={e} initialTasksFile={tasksFile} />;
}
