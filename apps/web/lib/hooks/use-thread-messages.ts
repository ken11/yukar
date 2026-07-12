"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { toast } from "sonner";
import type { Message } from "@/lib/api/endpoints";
import { extractDetail, getThreadMessages, postMessage } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { strandsMessagesToThreadMessageLikes } from "@/lib/assistant-ui/runtime";
import { useT } from "@/lib/i18n/provider";

interface UseThreadMessagesOptions {
  projectId: string;
  epicId: string;
  threadId: string;
  initialMessages: Message[];
}

interface UseThreadMessagesResult {
  messages: import("@assistant-ui/react").ThreadMessageLike[];
  sendMessage: (content: string) => void;
  isSending: boolean;
}

/**
 * Manages REST fetch (initial + refetch on send) and send mutation for a thread.
 * Returns converted ThreadMessageLike[] for assistant-ui.
 */
export function useThreadMessages({
  projectId,
  epicId,
  threadId,
  initialMessages,
}: UseThreadMessagesOptions): UseThreadMessagesResult {
  const qc = useQueryClient();
  const t = useT();

  const { data: rawMessages = initialMessages } = useQuery({
    queryKey: queryKeys.threads.messages(projectId, epicId, threadId),
    // MERGE the fetch result with the existing cache by message_id instead of
    // replacing it wholesale: the mount-time revalidation (initialDataUpdatedAt
    // 0) can resolve AFTER an optimistic send / SSE user_message_committed
    // appended newer entries, and a plain replace would roll those bubbles
    // back. Fetch result is the base; cache-only ids are kept at the tail —
    // the same shape as the parked-thread sync in use-run-activity.
    queryFn: async () => {
      const fetched = await getThreadMessages(projectId, epicId, threadId);
      const cached = qc.getQueryData<Message[]>(
        queryKeys.threads.messages(projectId, epicId, threadId),
      );
      if (!cached) return fetched;
      const known = new Set(fetched.map((m) => m.message_id));
      return [...fetched, ...cached.filter((m) => !known.has(m.message_id))];
    },
    initialData: initialMessages,
    // The RSC snapshot is captured at navigation time and can predate messages
    // an in-flight run writes moments later (e.g. an SPA transition onto a
    // fresh reviewer thread whose run reports seconds after — the reviewer
    // attribution fix depends on this). The turn-end SSE invalidation
    // (manager_message) no-ops while this query is not in the cache yet, so a
    // "fresh" initialData would pin the stale empty snapshot for the global
    // staleTime. Mark the initialData as already stale: the mount revalidates
    // once, then the SSE invalidation path owns freshness.
    initialDataUpdatedAt: 0,
  });

  const messages = strandsMessagesToThreadMessageLikes(rawMessages);

  const sendMutation = useMutation({
    mutationFn: (content: string) =>
      postMessage(projectId, epicId, threadId, { content, role: "user" }),
    onSuccess: (newMessage) => {
      // Active manager threads return a synthetic ack with message_id=-1: the message
      // body is NOT yet persisted. The canonical entry arrives via SSE
      // `user_message_committed` which calls setQueryData with the real id. Merging
      // the synthetic ack would create a duplicate that SSE dedup cannot remove
      // (different id: -1 vs the real id). Skip the cache merge for synthetic acks and
      // let SSE patch be the sole writer — consistent with the "no router.refresh()"
      // invariant (architecture.md §2.2 / §3.3).
      if (newMessage.message_id < 0) return;

      // Non-manager (ad-hoc) threads: POST returns the real persisted id. Merge into
      // the cache with dedup so we never call invalidateQueries (which would race with
      // concurrent SSE setQueryData calls and wipe optimistic updates).
      qc.setQueryData(
        queryKeys.threads.messages(projectId, epicId, threadId),
        (prev: Message[] | undefined) => {
          if (!prev) return prev;
          if (prev.some((m) => m.message_id === newMessage.message_id)) return prev;
          return [...prev, newMessage];
        },
      );
    },
    onError: (err) => {
      // A failed send must never be silent: the composer AND programmatic
      // senders (e.g. the plan-approval banner's wake message) route through
      // here, and a swallowed 409 (reviewer run active, budget reached, …)
      // leaves the user believing the agent was notified when it was not.
      toast.error(t("conversation.sendMessageFailed"), {
        description: extractDetail(err) ?? (err instanceof Error ? err.message : String(err)),
      });
    },
  });

  const sendMessage = useCallback(
    (content: string) => {
      sendMutation.mutate(content);
    },
    [sendMutation],
  );

  return {
    messages,
    sendMessage,
    isSending: sendMutation.isPending,
  };
}
