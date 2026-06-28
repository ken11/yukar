/**
 * Epic layout (RSC) — renders EpicShell with initial data.
 * The source of truth for the active epic is params.e only (no store, no ?epic= param).
 */

import { EpicShell } from "@/components/chrome/epic-shell";
import { getEpic, getProject, getRunState, listThreads } from "@/lib/api/endpoints";

export default async function EpicLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ p: string; e: string }>;
}) {
  const { p, e } = await params;

  const [project, epic, initialRunState, initialThreads] = await Promise.all([
    getProject(p).catch(() => null),
    getEpic(p, e).catch(() => null),
    getRunState(p, e).catch(() => null),
    listThreads(p, e).catch(() => []),
  ]);

  return (
    <EpicShell
      projectId={p}
      epicId={e}
      project={project}
      epic={epic}
      initialRunState={initialRunState}
      initialThreads={initialThreads}
    >
      {children}
    </EpicShell>
  );
}
