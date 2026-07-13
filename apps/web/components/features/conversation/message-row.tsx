"use client";

import { Icon } from "@/components/icon";
import { cn } from "@/lib/cn";
import type { KickoffView } from "@/lib/conversation/kickoff";
import { useT } from "@/lib/i18n/provider";
import { KickoffBlock } from "./kickoff-block";
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

/** HH:MM — seconds are noise in a conversation. */
export function formatTime(d: Date | undefined): string {
  if (!d) return "";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// ---------------------------------------------------------------------------
// Message rows
// ---------------------------------------------------------------------------

/**
 * UserMessage — bordered block (minimal border, subtle rounding). Left-aligned, within reading width.
 * The turn-0 kickoff renders structured (title / criteria / contract) with the
 * host boilerplate folded.
 */
export function UserMessage({
  msg,
  time,
  grouped,
  kickoff,
}: {
  msg: import("@assistant-ui/react").ThreadMessageLike;
  time: string;
  /** Same role as the previous item — the attribution header renders once per role run. */
  grouped?: boolean;
  /** Parsed kickoff view (turn-0 host prompt) + its raw text for the fold. */
  kickoff?: { view: KickoffView; raw: string } | null;
}) {
  const t = useT();
  return (
    <div className="flex flex-col gap-1" data-testid="user-message">
      <RoleAttribution
        agentRole="user"
        label={t("conversation.userRole")}
        time={time}
        className={grouped ? "hidden" : undefined}
      />
      <div
        className="rounded px-3 py-2.5 md:px-4 md:py-3"
        style={{
          border: "1px solid var(--color-outline-variant)",
          backgroundColor: "transparent",
        }}
      >
        {kickoff ? (
          <KickoffBlock view={kickoff.view} raw={kickoff.raw} />
        ) : (
          <MessageContent content={msg.content} isUser />
        )}
      </div>
    </div>
  );
}

/**
 * AgentMessage — understated block on the surface-container surface. Subtle rounding.
 * While streaming, shows a leading token caret (1px cyan, blinking).
 * `peak` is the terrain's high ground: the parked question/report addressed to
 * the user sheds the container surface and gets the larger composed setting.
 */
export function AgentMessage({
  msg,
  roleLabel,
  role,
  time,
  isStreaming,
  grouped,
  peak,
}: {
  msg: import("@assistant-ui/react").ThreadMessageLike;
  roleLabel: string;
  role: string;
  time: string;
  /** Same role as the previous item — the attribution header renders once per role run. */
  grouped?: boolean;
  isStreaming: boolean;
  peak?: boolean;
}) {
  return (
    <div className="flex flex-col gap-1" data-testid="agent-message">
      <RoleAttribution
        agentRole={role}
        label={roleLabel}
        time={time}
        className={grouped ? "hidden" : undefined}
      />
      <div
        className={cn(
          peak ? "peak-in rounded px-0 py-1" : "rounded px-3 py-2.5 md:px-4 md:py-3",
          // Tokens are flowing — the streaming bubble breathes (reduced-motion: off)
          isStreaming && "msg-breathe",
        )}
        style={peak ? undefined : { backgroundColor: "var(--color-surface-container)" }}
      >
        <MessageContent content={msg.content} peak={peak} />
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
  peak,
  kickoff,
}: {
  msg: import("@assistant-ui/react").ThreadMessageLike;
  roleLabel: string;
  role: string;
  /** Same role as the previous item — the attribution header renders once per role run. */
  grouped?: boolean;
  /** Terrain high ground — parked question/report addressed to the user. */
  peak?: boolean;
  /** Structured turn-0 kickoff (user messages only). */
  kickoff?: { view: KickoffView; raw: string } | null;
}) {
  const isUser = msg.role === "user";
  const isStreaming = msg.status?.type === "running";
  const time = formatTime(msg.createdAt);

  if (isUser) {
    return <UserMessage msg={msg} time={time} grouped={grouped} kickoff={kickoff} />;
  }
  return (
    <AgentMessage
      msg={msg}
      roleLabel={roleLabel}
      role={role}
      time={time}
      isStreaming={isStreaming}
      grouped={grouped}
      peak={peak}
    />
  );
}
