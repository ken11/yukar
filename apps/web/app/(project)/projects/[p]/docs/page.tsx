import { ProjectDocsClient } from "@/components/features/project-docs/project-docs-client";
import { getProjectDoc, listProjectDocs } from "@/lib/api/endpoints";
import { isDefined } from "@/lib/type-guards";

export default async function ProjectDocsPage({ params }: { params: Promise<{ p: string }> }) {
  const { p } = await params;

  const filenames = await listProjectDocs(p).catch(() => [] as string[]);
  const docs = await Promise.all(filenames.map((f) => getProjectDoc(p, f).catch(() => null)));
  const allDocs = docs.filter(isDefined);

  return <ProjectDocsClient projectId={p} initialDocs={allDocs} />;
}
