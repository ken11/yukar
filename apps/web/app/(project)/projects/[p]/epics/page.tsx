import { redirect } from "next/navigation";

/**
 * The epic board merged into the project overview (/projects/[p]).
 * This route survives as a redirect so old links and bookmarks keep working.
 */
export default async function EpicsBoardPage({ params }: { params: Promise<{ p: string }> }) {
  const { p } = await params;
  redirect(`/projects/${p}`);
}
