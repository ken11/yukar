"use client";

import Link from "next/link";
import { Icon } from "@/components/icon";
import { cn } from "@/lib/cn";
import { useT } from "@/lib/i18n/provider";
import type { ThreadTreeState } from "@/lib/sse/use-run-activity";

/**
 * AgentChips — the P3 strip's glanceable agent state (desktop only).
 *
 * One chip per Worker / Evaluator from the live tree state: who is at work
 * (cyan while running) and how each task ended, without opening a pane.
 * A chip links to its thread (read-only view); the mobile drawer keeps the
 * full ThreadTreePanel, so this renders nothing below md.
 */
export function AgentChips({
  treeState,
  projectId,
  epicId,
  currentThreadId,
}: {
  treeState: ThreadTreeState;
  projectId: string;
  epicId: string;
  currentThreadId: string;
}) {
  const t = useT();
  const workers = Object.values(treeState.workers);
  const evaluators = Object.values(treeState.evaluators);
  if (workers.length === 0 && evaluators.length === 0) return null;

  const workerStatus: Record<string, string> = {
    pending: t("conversation.statusPending"),
    running: t("conversation.statusRunning"),
    completed: t("conversation.statusCompleted"),
    failed: t("conversation.statusFailed"),
  };
  const evalStatus: Record<string, string> = {
    evaluating: t("conversation.statusEvaluating"),
    accepted: t("conversation.statusAccepted"),
    rejected: t("conversation.statusRejected"),
  };

  const chips = [
    ...workers.map((w) => ({
      key: `w:${w.threadId}`,
      threadId: w.status === "pending" ? null : w.threadId,
      icon: w.status === "failed" ? "warning" : "smart_toy",
      label: w.repo ? `${t("conversation.worker")} · ${w.repo}` : t("conversation.worker"),
      title: w.taskTitle ?? w.taskId ?? undefined,
      status: workerStatus[w.status] ?? w.status,
      live: w.status === "running",
      failed: w.status === "failed",
    })),
    ...evaluators.map((ev) => ({
      key: `e:${ev.evalId}`,
      threadId: ev.threadId,
      icon: ev.status === "rejected" ? "warning" : "rate_review",
      label: t("conversation.evaluator"),
      title: undefined as string | undefined,
      status: evalStatus[ev.status] ?? ev.status,
      live: ev.status === "evaluating",
      failed: ev.status === "rejected",
    })),
  ];

  return (
    <div
      data-testid="agent-chips"
      className="hidden min-w-0 flex-1 items-center gap-1.5 overflow-x-auto md:flex [&::-webkit-scrollbar]:hidden [-ms-overflow-style:none] [scrollbar-width:none]"
    >
      {chips.map((chip) => {
        const inner = (
          <>
            <Icon
              name={chip.icon}
              className={cn("shrink-0 text-[12px]", chip.failed && "text-error")}
              aria-hidden
            />
            <span className="data truncate">{chip.label}</span>
            {chip.live && (
              <span
                className="h-1.5 w-1.5 shrink-0 rounded-full"
                style={{ backgroundColor: "var(--color-light)" }}
                aria-hidden
              />
            )}
            <span
              className="data shrink-0"
              style={{
                color: chip.live
                  ? "var(--color-light)"
                  : chip.failed
                    ? "var(--color-error)"
                    : "var(--color-outline)",
              }}
            >
              {chip.status}
            </span>
          </>
        );
        const cls = "flex shrink-0 items-center gap-1.5 rounded border px-2 py-1 transition-colors";
        if (!chip.threadId) {
          return (
            <span
              key={chip.key}
              title={chip.title}
              className={cn(cls, "opacity-50")}
              style={{ borderColor: "var(--color-outline-variant)" }}
            >
              {inner}
            </span>
          );
        }
        const isCurrent = chip.threadId === currentThreadId;
        return (
          <Link
            key={chip.key}
            href={`/projects/${projectId}/epics/${epicId}/threads/${chip.threadId}`}
            title={chip.title}
            aria-current={isCurrent ? "page" : undefined}
            className={cn(
              cls,
              "hover:bg-surface-container focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white",
            )}
            style={{
              borderColor: isCurrent ? "var(--color-outline)" : "var(--color-outline-variant)",
            }}
          >
            {inner}
          </Link>
        );
      })}
    </div>
  );
}
