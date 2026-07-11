import { EpicsBoardClient } from "@/components/features/epics/epics-board-client";
import type { EpicWithRunSummary } from "@/lib/api/endpoints";
import { listEpics } from "@/lib/api/endpoints";

export default async function EpicsBoardPage({ params }: { params: Promise<{ p: string }> }) {
  const { p } = await params;
  // include_completed=true — the board shows open and completed epics alike
  // (matches the client-side refetch in EpicsBoardClient).
  const initialEpics = await listEpics(p, true).catch(() => [] as EpicWithRunSummary[]);

  return <EpicsBoardClient projectId={p} initialEpics={initialEpics} />;
}
