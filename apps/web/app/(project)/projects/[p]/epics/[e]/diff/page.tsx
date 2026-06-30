import { DiffPageClient } from "@/components/features/diff/diff-page-client";
import { getEpic, getGitDiff } from "@/lib/api/endpoints";

export default async function DiffPage({ params }: { params: Promise<{ p: string; e: string }> }) {
  const { p, e } = await params;

  const epic = await getEpic(p, e).catch(() => null);

  // Load diff for each touched repo. "epic" (branch vs default) is the client's
  // default mode, so prefetch the same mode to seed react-query initialData.
  const repos = epic?.touched_repos?.length ? epic.touched_repos : [];

  const initialDiffs = await Promise.all(
    repos.map((repo) => getGitDiff(p, e, repo, "epic").catch(() => null)),
  );

  const validDiffs = initialDiffs.filter(Boolean);

  return (
    <DiffPageClient
      projectId={p}
      epicId={e}
      epic={epic}
      initialDiffs={validDiffs as NonNullable<(typeof validDiffs)[0]>[]}
    />
  );
}
