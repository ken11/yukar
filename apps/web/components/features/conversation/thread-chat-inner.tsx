"use client";

import { AssistantRuntimeProvider, useExternalStoreRuntime } from "@assistant-ui/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { NewThreadModal } from "@/components/features/threads/new-thread-modal";
import { Icon } from "@/components/icon";
import type { ThreadEntry } from "@/lib/api/endpoints";
import {
  buildYukarAdapter,
  streamStateIsEmpty,
  streamStateTextLength,
} from "@/lib/assistant-ui/runtime";
import { useT } from "@/lib/i18n/provider";
import { ManagerEffortControl } from "./manager-effort-control";
import { MessageRow, roleIcon } from "./message-row";

// ---------------------------------------------------------------------------
// Right pane: chat
// ---------------------------------------------------------------------------

export function ThreadChatInner({
  thread,
  messages,
  streamState,
  isRunning,
  runFailed,
  runError,
  awaitingInput,
  isAwaitingInput,
  onSendMessage,
  isSending,
  isActiveTrial,
  isArchived,
  projectId,
  epicId,
}: {
  thread: ThreadEntry | null;
  messages: import("@assistant-ui/react").ThreadMessageLike[];
  streamState: import("@/lib/assistant-ui/runtime").StreamState;
  isRunning: boolean;
  runFailed: boolean;
  runError: string | null;
  awaitingInput: { threadId: string; question: string } | null;
  /** True when runStatus === "awaiting_input". Used to show the banner even when awaitingInput is null (waiting for SSE replay). */
  isAwaitingInput?: boolean;
  onSendMessage: (content: string) => void;
  isSending: boolean;
  /** True for the active trial (non-archived thread matching managerThreadId) — shows the composer */
  isActiveTrial: boolean;
  /** True for an archived thread — hides the composer */
  isArchived?: boolean;
  projectId?: string;
  epicId?: string;
}) {
  const t = useT();
  const [value, setValue] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);

  const handleAdapterSend = useCallback(
    async (content: string) => {
      onSendMessage(content);
    },
    [onSendMessage],
  );

  const adapter = useMemo(
    () =>
      buildYukarAdapter({
        messages,
        streamState,
        isRunning,
        onSendMessage: handleAdapterSend,
        awaitingInput,
      }),
    [messages, streamState, isRunning, handleAdapterSend, awaitingInput],
  );

  const runtime = useExternalStoreRuntime(adapter);

  // auto-scroll: the body only references bottomRef, so Biome would flag messages.length / streamTextLen
  // as "unused excess deps inside the effect", but they are intentionally included to re-trigger
  // scrolling each time the content grows.
  const streamTextLen = streamStateTextLength(streamState);
  // biome-ignore lint/correctness/useExhaustiveDependencies: scroll triggers on content length changes
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, streamTextLen]);

  // Focus the composer when entering awaiting state.
  // Also handles isAwaitingInput (runStatus-based) to cover the period while awaitingInput is null.
  useEffect(() => {
    if (awaitingInput || isAwaitingInput) {
      composerRef.current?.focus();
    }
  }, [awaitingInput, isAwaitingInput]);

  const allMessages = adapter.messages ?? [];

  const roleLabel: Record<string, string> = {
    manager: t("conversation.manager"),
    worker: `${t("conversation.worker")}${thread?.repo ? ` · ${thread.repo}` : ""}`,
    evaluator: t("conversation.evaluator"),
    reviewer: t("conversation.reviewer"),
    user: t("conversation.userRole"),
  };

  const threadRole = thread?.role ?? "manager";

  // The composer is shown for the active manager trial OR a (non-archived) reviewer
  // conversation — both are threads the user can reply to (backend post_message).
  const canCompose = isActiveTrial || (thread?.role === "reviewer" && !isArchived);

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      handleSend();
    }
  }

  function handleSend() {
    if (!value.trim() || isSending) return;
    onSendMessage(value.trim());
    setValue("");
  }

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div className="flex h-full flex-col overflow-hidden">
        {/* Thread role badge — datum row, not a card */}
        {thread && (
          <div
            className="flex shrink-0 items-center gap-3 px-6 py-2"
            style={{ borderBottom: "1px solid var(--color-outline-variant)" }}
          >
            <Icon
              name={roleIcon[thread.role] ?? "chat"}
              className="shrink-0 text-[14px] text-on-surface-variant"
              aria-hidden
            />
            <span className="address text-on-surface-variant">
              {roleLabel[thread.role] ?? thread.role}
            </span>
            <div className="ml-auto flex items-center gap-3">
              {isArchived && <span className="font-mono text-[10px] text-outline">archived</span>}
              {isRunning && !isArchived && (
                <span
                  className="data flex items-center gap-1.5"
                  style={{ color: "var(--color-light)" }}
                >
                  <span
                    className="h-1.5 w-1.5 shrink-0 rounded-full"
                    style={{ backgroundColor: "var(--color-light)" }}
                    aria-hidden
                  />
                  {t("conversation.running")}
                </span>
              )}
              {isActiveTrial && projectId && epicId && (
                <ManagerEffortControl projectId={projectId} epicId={epicId} />
              )}
            </div>
          </div>
        )}

        {/* Run failure banner — warm ▲ + concise cause */}
        {runFailed && (
          <div
            className="shrink-0 flex items-start gap-3 px-6 py-3"
            role="alert"
            style={{
              borderBottom: "1px solid color-mix(in oklab, var(--color-error) 20%, transparent)",
            }}
          >
            <Icon name="warning" className="mt-0.5 shrink-0 text-[16px] text-error" aria-hidden />
            <div>
              <p
                className="font-mono text-[12px] font-medium"
                style={{ color: "var(--color-error)" }}
              >
                ▲ {t("conversation.runFailedTitle")}
              </p>
              {runError && <p className="mt-0.5 data">{runError}</p>}
            </div>
          </div>
        )}

        {/* Awaiting approval banner — neutral annotation */}
        {/* Also shown when isAwaitingInput (runStatus-based) is set: covers the SSE-waiting period when awaitingInput is null */}
        {!runFailed && (awaitingInput || isAwaitingInput) && (
          <div
            className="shrink-0 flex items-center gap-2 px-6 py-2"
            role="status"
            aria-live="polite"
            style={{
              borderBottom: "1px solid color-mix(in oklab, var(--color-light) 20%, transparent)",
            }}
          >
            <span
              className="h-1.5 w-1.5 shrink-0 rounded-full"
              style={{ backgroundColor: "var(--color-light)" }}
              aria-hidden
            />
            <p className="font-mono text-[11px]" style={{ color: "var(--color-light)" }}>
              {t("conversation.awaitingBanner")}
            </p>
          </div>
        )}

        {/* Chat history */}
        <div
          className="flex-1 overflow-y-auto px-6 py-6"
          role="log"
          aria-live="polite"
          aria-label="Conversation"
        >
          <div className="mx-auto max-w-[var(--measure-read)] space-y-8">
            {allMessages.map((msg, i) => (
              <MessageRow
                key={msg.id ?? i}
                msg={msg}
                roleLabel={roleLabel[threadRole] ?? threadRole}
                role={threadRole}
              />
            ))}

            {/* Zero content + running placeholder */}
            {allMessages.length === 0 && isRunning && (
              <div
                className="flex items-center gap-2 px-4 py-3 font-mono text-[12px] text-on-surface-variant"
                role="status"
                aria-live="polite"
              >
                <span
                  className="h-1.5 w-1.5 shrink-0 rounded-full"
                  style={{ backgroundColor: "var(--color-light)" }}
                  aria-hidden
                />
                {t("conversation.streamingLive")}
              </div>
            )}

            {/* First token not yet received (waiting for model response) */}
            {allMessages.length > 0 &&
              isRunning &&
              !streamState.done &&
              streamStateIsEmpty(streamState) && (
                <div
                  className="flex items-center gap-2 px-4 py-3 font-mono text-[12px] text-on-surface-variant"
                  role="status"
                  aria-live="polite"
                >
                  <Icon name="hourglass_empty" className="shrink-0 text-[14px]" aria-hidden />
                  {t("conversation.waiting")}
                </div>
              )}

            <div ref={bottomRef} aria-hidden />
          </div>
        </div>

        {/* Composer — shown only for the active trial. archived / old trial / worker / evaluator get a read-only banner */}
        {isArchived ? (
          <div className="shrink-0" style={{ borderTop: "1px solid var(--color-outline-variant)" }}>
            <div className="mx-auto max-w-[var(--measure-read)] px-6 py-4">
              <div
                data-testid="thread-archived-banner"
                className="flex items-start gap-2.5 px-4 py-3 font-mono text-[12px] text-on-surface-variant"
                style={{
                  border: "1px solid var(--color-outline-variant)",
                  borderRadius: "4px",
                  backgroundColor: "var(--color-surface-container-lowest)",
                }}
                role="note"
              >
                <Icon name="archive" className="mt-0.5 shrink-0 text-[14px]" aria-hidden />
                <span>{t("common.archiveReadOnly")}</span>
              </div>
              {projectId && epicId && (
                <div className="mt-3">
                  <NewThreadModal projectId={projectId} epicId={epicId} />
                </div>
              )}
            </div>
          </div>
        ) : canCompose ? (
          <div className="shrink-0" style={{ borderTop: "1px solid var(--color-outline-variant)" }}>
            <div className="mx-auto max-w-[var(--measure-read)] px-6 py-4">
              {/* Recess surface + shadow datum */}
              <div
                className="flex flex-col"
                style={{
                  backgroundColor: "var(--color-surface-container-lowest)",
                  border: "1px solid var(--color-outline-variant)",
                  borderRadius: "4px",
                }}
              >
                <textarea
                  ref={composerRef}
                  data-testid="thread-composer"
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder={t("conversation.composerPlaceholder")}
                  rows={3}
                  className="flex-1 resize-none bg-transparent px-4 pt-3 pb-2 text-body-md text-on-surface placeholder:text-outline focus:outline-none"
                  aria-label={t("conversation.composerPlaceholder")}
                />
                <div className="flex items-center justify-end gap-3 px-3 pb-3">
                  <button
                    type="button"
                    onClick={handleSend}
                    disabled={!value.trim() || isSending}
                    className="flex items-center gap-1.5 rounded px-3 py-1.5 text-body-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-40"
                    style={{
                      backgroundColor: "var(--color-primary)",
                      color: "var(--color-on-primary)",
                      minHeight: "32px",
                    }}
                  >
                    <Icon name="send" className="text-[15px]" aria-hidden />
                    {isSending ? t("conversation.sending") : t("conversation.send")}
                  </button>
                </div>
              </div>
            </div>
          </div>
        ) : (
          <div className="shrink-0" style={{ borderTop: "1px solid var(--color-outline-variant)" }}>
            <div className="mx-auto max-w-[var(--measure-read)] px-6 py-4">
              <div
                data-testid="thread-readonly-banner"
                className="flex items-start gap-2.5 px-4 py-3 font-mono text-[12px] text-on-surface-variant"
                style={{
                  border: "1px solid var(--color-outline-variant)",
                  borderRadius: "4px",
                  backgroundColor: "var(--color-surface-container-lowest)",
                }}
                role="note"
              >
                <Icon name="lock" className="mt-0.5 shrink-0 text-[14px]" aria-hidden />
                <span>
                  {t("conversation.readonlyNote")}{" "}
                  <strong className="text-on-surface">
                    {t("conversation.readonlyManagerHint")}
                  </strong>{" "}
                  {t("conversation.readonlyManagerSuffix")}
                </span>
              </div>
            </div>
          </div>
        )}
      </div>
    </AssistantRuntimeProvider>
  );
}
