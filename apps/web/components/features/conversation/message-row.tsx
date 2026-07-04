"use client";

import { Icon } from "@/components/icon";
import { useT } from "@/lib/i18n/provider";
import { MessageContent } from "./message-content";

// ---------------------------------------------------------------------------
// Role attribution header
// ---------------------------------------------------------------------------

export const roleIcon: Record<string, string> = {
  manager: "manage_accounts",
  worker: "smart_toy",
  evaluator: "rate_review",
  arbiter: "balance",
  reviewer: "fact_check",
  user: "person",
};

export function RoleAttribution({
  agentRole,
  label,
  time,
}: {
  agentRole: string;
  label: string;
  time: string;
}) {
  return (
    <div className="mb-2 flex items-center gap-2">
      <Icon
        name={roleIcon[agentRole] ?? "chat"}
        className="shrink-0 text-[13px] text-on-surface-variant"
      />
      <span className="label text-[11px] font-medium uppercase tracking-[0.05em] text-on-surface-variant">
        {label}
      </span>
      {time && <span className="data ml-auto">{time}</span>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Message rows
// ---------------------------------------------------------------------------

/**
 * UserMessage — bordered block (minimal border, subtle rounding). Left-aligned, within reading width.
 */
export function UserMessage({
  msg,
  time,
}: {
  msg: import("@assistant-ui/react").ThreadMessageLike;
  time: string;
}) {
  const t = useT();
  return (
    <div className="flex flex-col gap-1" data-testid="user-message">
      <RoleAttribution agentRole="user" label={t("conversation.userRole")} time={time} />
      <div
        className="rounded px-4 py-3"
        style={{
          border: "1px solid var(--color-outline-variant)",
          backgroundColor: "transparent",
        }}
      >
        <MessageContent content={msg.content} />
      </div>
    </div>
  );
}

/**
 * AgentMessage — understated block on the surface-container surface. Subtle rounding.
 * While streaming, shows a leading token caret (1px cyan, blinking).
 */
export function AgentMessage({
  msg,
  roleLabel,
  role,
  time,
  isStreaming,
}: {
  msg: import("@assistant-ui/react").ThreadMessageLike;
  roleLabel: string;
  role: string;
  time: string;
  isStreaming: boolean;
}) {
  return (
    <div className="flex flex-col gap-1" data-testid="agent-message">
      <RoleAttribution agentRole={role} label={roleLabel} time={time} />
      <div
        className="rounded px-4 py-3"
        style={{ backgroundColor: "var(--color-surface-container)" }}
      >
        <MessageContent content={msg.content} />
        {isStreaming && (
          <span
            className="ml-0.5 mt-1 inline-block h-[1em] w-px align-text-bottom"
            style={{
              backgroundColor: "var(--color-light)",
              animation: "light-pulse 0.9s ease-in-out infinite",
            }}
            aria-hidden
          />
        )}
      </div>
    </div>
  );
}

/**
 * AwaitingMessage — synthetic message shown when the Manager is waiting for your approval or answer.
 * Neutral annotation (not warm) + pending_actions glyph instead of a lock.
 */
export function AwaitingMessage({
  msg,
  t,
}: {
  msg: import("@assistant-ui/react").ThreadMessageLike;
  t: (k: string) => string;
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="mb-2 flex items-center gap-2">
        <span
          className="h-1.5 w-1.5 shrink-0 rounded-full"
          style={{ backgroundColor: "var(--color-light)" }}
          aria-hidden="true"
        />
        <span
          className="label text-[11px] font-medium uppercase tracking-[0.05em]"
          style={{ color: "var(--color-light)" }}
        >
          {t("conversation.awaitingInput")}
        </span>
      </div>
      <div
        className="rounded px-4 py-3"
        style={{
          border: "1px solid color-mix(in oklab, var(--color-light) 20%, transparent)",
          backgroundColor: "var(--color-surface-container)",
        }}
      >
        <MessageContent content={msg.content} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Unified message dispatcher
// ---------------------------------------------------------------------------

export function MessageRow({
  msg,
  roleLabel,
  role,
}: {
  msg: import("@assistant-ui/react").ThreadMessageLike;
  roleLabel: string;
  role: string;
}) {
  const t = useT();
  const isUser = msg.role === "user";
  const isStreaming = msg.status?.type === "running";
  const isAwaiting = msg.id === "__awaiting__";
  const time = msg.createdAt ? msg.createdAt.toLocaleTimeString() : "";

  if (isAwaiting) {
    return <AwaitingMessage msg={msg} t={t} />;
  }
  if (isUser) {
    return <UserMessage msg={msg} time={time} />;
  }
  return (
    <AgentMessage
      msg={msg}
      roleLabel={roleLabel}
      role={role}
      time={time}
      isStreaming={isStreaming}
    />
  );
}
