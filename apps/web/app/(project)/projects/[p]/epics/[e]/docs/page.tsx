import { DocsPageClient } from "@/components/features/editor/docs-page-client";
import type { DocResponse } from "@/lib/api/endpoints";
import { getEpicDoc, getProjectDoc, listEpicDocs, listProjectDocs } from "@/lib/api/endpoints";
import { isDefined } from "@/lib/type-guards";

export default async function DocsPage({ params }: { params: Promise<{ p: string; e: string }> }) {
  const { p, e } = await params;

  // Fetch project docs
  const projectFilenames = await listProjectDocs(p).catch(() => [] as string[]);
  const epicFilenames = await listEpicDocs(p, e).catch(() => [] as string[]);

  // Fetch content for first doc of each scope (lazy-load rest in client)
  const projectDocs = await Promise.all(
    projectFilenames.map((f) => getProjectDoc(p, f).catch(() => null)),
  );
  const epicDocs = await Promise.all(
    epicFilenames.map((f) => getEpicDoc(p, e, f).catch(() => null)),
  );

  const allDocs: Array<DocResponse & { scope: "project" | "epic" }> = [
    ...projectDocs.filter(isDefined).map((d) => ({ ...d, scope: "project" as const })),
    ...epicDocs.filter(isDefined).map((d) => ({ ...d, scope: "epic" as const })),
  ];

  return <DocsPageClient projectId={p} epicId={e} initialDocs={allDocs} />;
}
