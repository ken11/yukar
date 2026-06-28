"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import type { Message } from "@/lib/api/endpoints";
import { getThreadMessages, postMessage } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { strandsMessagesToThreadMessageLikes } from "@/lib/assistant-ui/runtime";

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

  const { data: rawMessages = initialMessages } = useQuery({
    queryKey: queryKeys.threads.messages(projectId, epicId, threadId),
    queryFn: () => getThreadMessages(projectId, epicId, threadId),
    initialData: initialMessages,
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
