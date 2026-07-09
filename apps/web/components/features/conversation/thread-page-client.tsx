"use client";

import { useEffect, useRef, useState } from "react";
import { useEpicRun } from "@/components/chrome/epic-run-context";
import { Icon } from "@/components/icon";
import type { Message, ThreadEntry } from "@/lib/api/endpoints";
import { cn } from "@/lib/cn";
import { useThreadMessages } from "@/lib/hooks/use-thread-messages";
import { useT } from "@/lib/i18n/provider";
import { isAgentActive, selectThreadLiveState } from "@/lib/sse/use-run-activity";
import { computeIsActiveTrial } from "@/lib/thread-utils";
import { ThreadChatInner } from "./thread-chat-inner";
import { ThreadListPane } from "./thread-list-pane";

// ---------------------------------------------------------------------------
// Outer component: data fetching + SSE
// ---------------------------------------------------------------------------

interface ThreadPageClientProps {
  projectId: string;
  epicId: string;
  threadId: string;
  thread: ThreadEntry | null;
  initialThreads: ThreadEntry[];
  initialMessages: Message[];
}

export function ThreadPageClient({
  projectId,
  epicId,
  threadId,
  thread,
  initialThreads,
  initialMessages,
}: ThreadPageClientProps) {
  const { messages, sendMessage, isSending } = useThreadMessages({
    projectId,
    epicId,
    threadId,
    initialMessages,
  });

  // Read from EpicShell's single SSE subscription via context (double EventSource is forbidden)
  const { activityState, clearLiveBuffer } = useEpicRun();

  // RunState.manager_thread is used as activityState.managerThreadId.
  // Updated via SSE but also works with the "manager" fallback during REST restoration.
  const managerThreadId = activityState.managerThreadId ?? "manager";

  // Get the live buffer for thread_id
  const liveState = selectThreadLiveState(activityState, threadId);

  // isRunning: run is active and the agent for this thread is operating
  const isRunning = isAgentActive(activityState, threadId) || liveState.isRunning;
  const runFailed = activityState.runStatus === "error";
  const runError = activityState.runError;

  // Awaiting input:
  // Show the banner even when awaitingInput is null if runStatus === "awaiting_input",
  // checking thread association while also accounting for the state before SSE replay arrives.
  const awaitingInput =
    activityState.awaitingInput &&
    (threadId === activityState.awaitingInput.threadId || threadId === managerThreadId)
      ? activityState.awaitingInput
      : null;

  // Banner is controlled by runStatus (also shown during the SSE/REST waiting period when awaitingInput is null).
  // ask_user is manager-thread-only, so it is not shown on worker threads.
  const isAwaitingInput =
    activityState.runStatus === "awaiting_input" && threadId === managerThreadId;

  // Bug4: Clear the live buffer when the authoritative REST data arrives
  const prevMsgCountRef = useRef(messages.length);
  useEffect(() => {
    const prev = prevMsgCountRef.current;
    prevMsgCountRef.current = messages.length;
    if (messages.length > prev && liveState.streamState.done) {
      clearLiveBuffer(threadId);
    }
  }, [messages.length, liveState.streamState.done, clearLiveBuffer, threadId]);

  // Archived threads do not show the composer.
  const isArchived = thread?.status === "archived";
  // Only the active trial shows the composer.
  // "Active trial" = viewing the thread pointed to by managerThreadId and not archived.
  // Even role=manager threads that do not match managerThreadId are read-only.
  // The sole path for showing the composer is activityState.managerThreadId (activeThreadId → SET_MANAGER_THREAD_ID).
  // applyTreeInit's archived exclusion is a fix for tree display nodes and is unrelated to the composer.
  const isActiveTrial = computeIsActiveTrial(threadId, managerThreadId, isArchived);

  // Mobile: open/close state of the thread list panel
  const [mobileListOpen, setMobileListOpen] = useState(false);
  const t = useT();

  return (
    <div className="flex h-full overflow-hidden">
      {/*
       * Left pane: thread list
       * PC (md and above): always visible (flex)
       * Mobile: shown as overlay only when mobileListOpen is true
       */}

      {/* Mobile: overlay backdrop */}
      {mobileListOpen && (
        <div
          className="fixed inset-0 z-20 bg-black/50 md:hidden"
          onClick={() => setMobileListOpen(false)}
          aria-hidden="true"
        />
      )}

      {/* Thread list panel */}
      <div
        className={cn(
          // PC: always shown as a normal flex item
          "hidden md:flex",
          // Mobile: fixed overlay sliding in from the left (the mobile top bar is
          // hidden on epic detail routes, so the overlay starts at the very top)
          mobileListOpen && "fixed inset-x-0 bottom-0 top-0 z-30 flex",
        )}
        style={mobileListOpen ? { paddingBottom: "env(safe-area-inset-bottom)" } : undefined}
      >
        <ThreadListPane
          projectId={projectId}
          epicId={epicId}
          currentThreadId={threadId}
          initialThreads={initialThreads}
          onClose={() => setMobileListOpen(false)}
        />
      </div>

      {/* Right pane: chat (full width on mobile) */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {/*
         * Mobile thread-list toggle: rendered INSIDE ThreadChatInner's role bar
         * (single merged bar) instead of a dedicated bar, to keep mobile chrome flat.
         */}
        <ThreadChatInner
          threadListToggle={
            <button
              type="button"
              onClick={() => setMobileListOpen((v) => !v)}
              className="flex min-w-0 shrink items-center gap-1.5 rounded px-1.5 py-1 text-body-sm text-on-surface-variant transition-colors hover:bg-surface-container hover:text-on-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-inset md:hidden"
              style={{ minHeight: "40px" }}
              aria-label={
                mobileListOpen
                  ? t("conversation.closeThreadList")
                  : t("conversation.openThreadList")
              }
              aria-expanded={mobileListOpen}
            >
              <Icon name={mobileListOpen ? "close" : "menu"} className="text-[18px]" aria-hidden />
              <span className="truncate text-[12px]">
                {thread?.title ?? t("conversation.manager")}
              </span>
            </button>
          }
          thread={thread}
          messages={messages}
          streamState={liveState.streamState}
          isRunning={isRunning}
          runFailed={runFailed}
          runError={runError}
          awaitingInput={awaitingInput}
          isAwaitingInput={isAwaitingInput}
          onSendMessage={sendMessage}
          isSending={isSending}
          isActiveTrial={isActiveTrial}
          isArchived={isArchived}
          projectId={projectId}
          epicId={epicId}
        />
      </div>
    </div>
  );
}
