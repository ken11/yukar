import { ThreadPageClient } from "@/components/features/conversation/thread-page-client";
import { getThreadMessages, listThreads } from "@/lib/api/endpoints";

export default async function ThreadPage({
  params,
}: {
  params: Promise<{ p: string; e: string; t: string }>;
}) {
  const { p, e, t } = await params;

  const [messages, threads] = await Promise.all([
    getThreadMessages(p, e, t).catch(() => []),
    listThreads(p, e).catch(() => []),
  ]);

  const thread = threads.find((th) => th.id === t) ?? threads[0] ?? null;

  return (
    <ThreadPageClient
      projectId={p}
      epicId={e}
      threadId={t}
      thread={thread}
      initialThreads={threads}
      initialMessages={messages}
    />
  );
}
