"use client";

import { AssistantRuntimeProvider, useExternalStoreRuntime } from "@assistant-ui/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useEpicRun } from "@/components/chrome/epic-run-context";
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
import { PlanApprovalBanner } from "./plan-approval-banner";

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
  threadListToggle,
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
  /** Mobile-only thread-list toggle button, rendered at the head of the role bar (md:hidden inside) */
  threadListToggle?: React.ReactNode;
}) {
  const t = useT();
  const [value, setValue] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);

  // Mobile reading mode: scrolling down the history collapses the epic header +
  // tab bar (EpicShell applies the classes below md only); scrolling up restores them.
  const { setMobileChromeHidden } = useEpicRun();
  const lastScrollTopRef = useRef(0);
  // Programmatic scrolls (auto-scroll to the latest message) must not collapse
  // the chrome — only user gestures should. The auto-scroll effect bumps this
  // deadline before calling scrollIntoView.
  const suppressChromeHideUntilRef = useRef(0);
  const handleHistoryScroll = useCallback(
    (e: React.UIEvent<HTMLDivElement>) => {
      const el = e.currentTarget;
      const delta = el.scrollTop - lastScrollTopRef.current;
      lastScrollTopRef.current = el.scrollTop;
      if (performance.now() < suppressChromeHideUntilRef.current) return;
      if (Math.abs(delta) < 8) return;
      if (delta > 0 && el.scrollTop > 48) {
        setMobileChromeHidden(true);
      } else if (delta < 0) {
        setMobileChromeHidden(false);
      }
    },
    [setMobileChromeHidden],
  );
  // Restore the chrome when leaving the conversation (e.g. switching to the tasks tab)
  useEffect(() => () => setMobileChromeHidden(false), [setMobileChromeHidden]);

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
    // Smooth scroll can emit events for up to ~1s — don't let it collapse the mobile chrome
    suppressChromeHideUntilRef.current = performance.now() + 1000;
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, streamTextLen]);

  // Focus the composer when entering awaiting state.
  // Also handles isAwaitingInput (runStatus-based) to cover the period while awaitingInput is null.
  useEffect(() => {
    if (awaitingInput || isAwaitingInput) {
      composerRef.current?.focus();
    }
  }, [awaitingInput, isAwaitingInput]);

  // Auto-grow the composer with its content (capped by max-h; CSS min-height wins on desktop).
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-measures on every input; `value` is the trigger, not a read dependency
  useEffect(() => {
    const el = composerRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [value]);

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
        {/* Thread role badge — datum row, not a card.
            Mobile: doubles as the single merged bar (thread-list toggle + role icon + effort);
            the role LABEL is hidden there because the toggle already shows the trial title. */}
        {thread && (
          <div
            className="flex shrink-0 items-center gap-2 px-2 py-1 md:gap-3 md:px-6 md:py-2"
            style={{ borderBottom: "1px solid var(--color-outline-variant)" }}
          >
            {threadListToggle}
            <Icon
              name={roleIcon[thread.role] ?? "chat"}
              className="shrink-0 text-[14px] text-on-surface-variant"
              aria-hidden
            />
            <span className="address hidden text-on-surface-variant md:inline">
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

        {/* Plan approval (P2) — explicit, snapshot-bound. Rendered next to the
            awaiting banner; only the active trial can approve (composer owner). */}
        {isActiveTrial && projectId && epicId && (
          <PlanApprovalBanner projectId={projectId} epicId={epicId} onSendMessage={onSendMessage} />
        )}

        {/* Chat history */}
        <div
          className="flex-1 overflow-y-auto px-4 py-4 md:px-6 md:py-6"
          onScroll={handleHistoryScroll}
          role="log"
          aria-live="polite"
          aria-label="Conversation"
        >
          <div className="mx-auto max-w-[var(--measure-read)] space-y-3 md:space-y-8">
            {allMessages.map((msg, i) => {
              // Mobile groups consecutive same-role messages under one attribution
              // header (desktop keeps a header per bubble — see MessageRow).
              const prev = i > 0 ? allMessages[i - 1] : undefined;
              const grouped =
                !!prev &&
                prev.role === msg.role &&
                msg.id !== "__awaiting__" &&
                prev.id !== "__awaiting__";
              return (
                <MessageRow
                  key={msg.id ?? i}
                  msg={msg}
                  roleLabel={roleLabel[threadRole] ?? threadRole}
                  role={threadRole}
                  grouped={grouped}
                />
              );
            })}

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
            <div className="mx-auto max-w-[var(--measure-read)] px-4 py-2.5 md:px-6 md:py-4">
              {/* Recess surface + shadow datum.
                  Mobile: single row (auto-growing textarea + icon send button).
                  Desktop (md:): column layout with the labelled send button below. */}
              <div
                className="flex items-end md:flex-col md:items-stretch"
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
                  rows={1}
                  className="max-h-40 min-w-0 flex-1 resize-none bg-transparent px-3 pt-2.5 pb-2 text-body-md text-on-surface placeholder:text-outline focus:outline-none md:px-4 md:pt-3 md:min-h-[80px]"
                  aria-label={t("conversation.composerPlaceholder")}
                />
                <div className="flex shrink-0 items-center justify-end gap-3 p-1.5 md:px-3 md:pb-3 md:pt-0">
                  <button
                    type="button"
                    onClick={handleSend}
                    disabled={!value.trim() || isSending}
                    aria-label={isSending ? t("conversation.sending") : t("conversation.send")}
                    className="flex min-h-[40px] min-w-[40px] items-center justify-center gap-1.5 rounded px-2 py-2 text-body-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-40 md:min-h-[32px] md:min-w-0 md:px-3 md:py-1.5"
                    style={{
                      backgroundColor: "var(--color-primary)",
                      color: "var(--color-on-primary)",
                    }}
                  >
                    <Icon name="send" className="text-[18px] md:text-[15px]" aria-hidden />
                    <span className="hidden md:inline">
                      {isSending ? t("conversation.sending") : t("conversation.send")}
                    </span>
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
