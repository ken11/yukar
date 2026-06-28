/**
 * Epic index — redirects to the active manager (or first thread if none) when threads exist.
 * Shows EmptyState when there are no threads.
 */

import { redirect } from "next/navigation";
import { EmptyState } from "@/components/ui/empty-state";
import { getEpic, listThreads } from "@/lib/api/endpoints";
import { resolveActiveManagerThreadId } from "@/lib/epic-utils";
import { getDictionary } from "@/lib/i18n/dictionary";
import { getLocale } from "@/lib/i18n/locale";

export default async function EpicIndexPage({
  params,
}: {
  params: Promise<{ p: string; e: string }>;
}) {
  const { p, e } = await params;

  const [threads, epic, locale] = await Promise.all([
    listThreads(p, e).catch(() => []),
    getEpic(p, e).catch(() => null),
    getLocale(),
  ]);

  const t = getDictionary(locale);

  if (threads.length > 0) {
    // Prefer the active manager trial (epic.active_thread_id > status=active > fallback)
    const activeThreads = threads.filter((t) => t.status !== "archived");
    if (activeThreads.length > 0) {
      const managerId = resolveActiveManagerThreadId(epic, threads);
      redirect(`/projects/${p}/epics/${e}/threads/${managerId}`);
    } else {
      // All threads are archived; redirect to the first one
      redirect(`/projects/${p}/epics/${e}/threads/${threads[0].id}`);
    }
  }

  // No threads — show EmptyState
  return (
    <div className="mx-auto max-w-[var(--measure-read)]">
      <EmptyState address={`${e} ／ ${t.empty.noThreadsYet}`} message={t.empty.noThreadsMessage} />
    </div>
  );
}
