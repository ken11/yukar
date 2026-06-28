import { ProjectReposClient } from "@/components/features/project-repos/project-repos-client";
import { getIndexStatus, listRepos } from "@/lib/api/endpoints";

export default async function ProjectReposPage({ params }: { params: Promise<{ p: string }> }) {
  const { p } = await params;

  const [repos, indexStatus] = await Promise.all([
    listRepos(p).catch(() => []),
    getIndexStatus(p).catch(() => ({ statuses: [] })),
  ]);

  return <ProjectReposClient projectId={p} initialRepos={repos} initialIndexStatus={indexStatus} />;
}
