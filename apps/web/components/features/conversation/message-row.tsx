"use client";

import { Icon } from "@/components/icon";
import { cn } from "@/lib/cn";
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
  className,
}: {
  agentRole: string;
  label: string;
  time: string;
  className?: string;
}) {
  return (
    <div className={cn("mb-2 flex items-center gap-2", className)}>
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
  grouped,
}: {
  msg: import("@assistant-ui/react").ThreadMessageLike;
  time: string;
  /** Same role as the previous message — mobile hides the attribution header (desktop keeps it) */
  grouped?: boolean;
}) {
  const t = useT();
  return (
    <div className="flex flex-col gap-1" data-testid="user-message">
      <RoleAttribution
        agentRole="user"
        label={t("conversation.userRole")}
        time={time}
        className={grouped ? "hidden md:flex" : undefined}
      />
      <div
        className="rounded px-3 py-2.5 md:px-4 md:py-3"
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
  grouped,
}: {
  msg: import("@assistant-ui/react").ThreadMessageLike;
  roleLabel: string;
  role: string;
  time: string;
  isStreaming: boolean;
  /** Same role as the previous message — mobile hides the attribution header (desktop keeps it) */
  grouped?: boolean;
}) {
  return (
    <div className="flex flex-col gap-1" data-testid="agent-message">
      <RoleAttribution
        agentRole={role}
        label={roleLabel}
        time={time}
        className={grouped ? "hidden md:flex" : undefined}
      />
      <div
        className="rounded px-3 py-2.5 md:px-4 md:py-3"
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

// ---------------------------------------------------------------------------
// Unified message dispatcher
// ---------------------------------------------------------------------------

export function MessageRow({
  msg,
  roleLabel,
  role,
  grouped,
}: {
  msg: import("@assistant-ui/react").ThreadMessageLike;
  roleLabel: string;
  role: string;
  /** Same role as the previous message — mobile hides the attribution header (desktop keeps it) */
  grouped?: boolean;
}) {
  const isUser = msg.role === "user";
  const isStreaming = msg.status?.type === "running";
  const time = msg.createdAt ? msg.createdAt.toLocaleTimeString() : "";

  if (isUser) {
    return <UserMessage msg={msg} time={time} grouped={grouped} />;
  }
  return (
    <AgentMessage
      msg={msg}
      roleLabel={roleLabel}
      role={role}
      time={time}
      isStreaming={isStreaming}
      grouped={grouped}
    />
  );
}
