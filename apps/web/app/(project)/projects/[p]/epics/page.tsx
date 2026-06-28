import { EpicsBoardClient } from "@/components/features/epics/epics-board-client";
import type { Epic } from "@/lib/api/endpoints";
import { listEpics } from "@/lib/api/endpoints";

export default async function EpicsBoardPage({ params }: { params: Promise<{ p: string }> }) {
  const { p } = await params;
  const initialEpics = await listEpics(p).catch(() => [] as Epic[]);

  return <EpicsBoardClient projectId={p} initialEpics={initialEpics} />;
}
