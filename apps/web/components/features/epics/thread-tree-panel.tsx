"use client";

/**
 * ThreadTreePanel — agent state tree (B1 integrated version)
 *
 * Design language compliance:
 *   - Rows, not cards. State shown as glyph + label. Cyan only while running.
 *   - LiveDot is a static cyan dot (light-live is consolidated into EpicScopeHeader).
 *   - ManagerNode / WorkerNode / EvaluatorNode border/bg use neutral tonal + single cyan accent,
 *     not secondary/primary (blue-family).
 *   - All copy goes through useT() (no hardcoded Japanese).
 */

import Link from "next/link";
import { Icon } from "@/components/icon";
import { cn } from "@/lib/cn";
import { useT } from "@/lib/i18n/provider";
import type {
  EvaluatorNodeState,
  ManagerNodeState,
  ThreadTreeState,
  WorkerNodeState,
} from "@/lib/sse/use-run-activity";

// ---------------------------------------------------------------------------
// Cyan static indicator (single point only — do not scatter or pulse)
// ---------------------------------------------------------------------------

function LiveDot({ active }: { active: boolean }) {
  if (!active) return null;
  return (
    <span
      className="h-1.5 w-1.5 shrink-0 rounded-full"
      style={{ backgroundColor: "var(--color-light)" }}
      aria-hidden
    />
  );
}

// ---------------------------------------------------------------------------
// State glyph helpers
// ---------------------------------------------------------------------------

/** State glyph (color is neutral rather than matching the label; warm color only for failures) */
function StateGlyph({ status }: { status: string }) {
  if (status === "completed" || status === "accepted") {
    return (
      <Icon name="check" className="shrink-0 text-[12px] text-on-surface-variant" aria-hidden />
    );
  }
  if (status === "failed" || status === "rejected") {
    return <Icon name="warning" className="shrink-0 text-[12px] text-error" />;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Manager node
// ---------------------------------------------------------------------------

function ManagerNode({
  node,
  projectId,
  epicId,
}: {
  node: ManagerNodeState;
  projectId: string;
  epicId: string;
}) {
  const t = useT();
  const statusLabelMap: Record<string, string> = {
    idle: t("conversation.statusIdle"),
    thinking: t("conversation.statusThinking"),
    delegating: t("conversation.statusDelegating"),
    completed: t("conversation.statusCompleted"),
  };

  const href = `/projects/${projectId}/epics/${epicId}/threads/${node.threadId}`;
  const statusText = statusLabelMap[node.status] ?? node.status;
  const isActive = node.status === "thinking" || node.status === "delegating";

  return (
    <Link
      href={href}
      className={cn(
        "group flex items-center gap-2 px-2 py-1.5 transition-colors",
        "min-h-[36px] rounded",
        "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white focus-visible:ring-inset",
        "hover:bg-surface-container-high",
      )}
    >
      <Icon
        name="manage_accounts"
        className="shrink-0 text-[13px] text-on-surface-variant"
        aria-hidden
      />
      <span className="data flex-1 truncate">{t("conversation.manager")}</span>
      <LiveDot active={node.isStreaming} />
      <StateGlyph status={node.status} />
      <span
        className="data shrink-0"
        style={isActive ? { color: "var(--color-light)" } : undefined}
      >
        {statusText}
      </span>
    </Link>
  );
}

// ---------------------------------------------------------------------------
// Worker node
// ---------------------------------------------------------------------------

function WorkerNode({
  node,
  projectId,
  epicId,
  children,
}: {
  node: WorkerNodeState;
  projectId: string;
  epicId: string;
  children?: React.ReactNode;
}) {
  const t = useT();
  const statusLabelMap: Record<string, string> = {
    pending: t("conversation.statusPending"),
    running: t("conversation.statusRunning"),
    completed: t("conversation.statusCompleted"),
    failed: t("conversation.statusFailed"),
  };

  const isPending = node.status === "pending";
  const href = isPending
    ? undefined
    : `/projects/${projectId}/epics/${epicId}/threads/${node.threadId}`;
  const statusText = statusLabelMap[node.status] ?? node.status;
  const isActive = node.status === "running";

  const rowEl = (
    <div
      className={cn(
        "flex items-center gap-2 px-2 py-1.5 transition-colors",
        "min-h-[36px] rounded",
        isPending && "opacity-50",
        !isPending && "group-hover:bg-surface-container-high cursor-pointer",
      )}
    >
      <Icon
        name={node.status === "failed" ? "warning" : "smart_toy"}
        className={cn(
          "shrink-0 text-[13px]",
          node.status === "failed" ? "text-error" : "text-on-surface-variant",
        )}
      />
      <div className="min-w-0 flex-1">
        <span className="data block truncate">{node.taskTitle ?? node.taskId ?? "worker"}</span>
        {node.repo && <span className="data block truncate opacity-60">{node.repo}</span>}
      </div>
      <LiveDot active={node.isStreaming} />
      <StateGlyph status={node.status} />
      <span
        className="data shrink-0"
        style={isActive ? { color: "var(--color-light)" } : undefined}
      >
        {statusText}
      </span>
    </div>
  );

  return (
    <div>
      {href ? (
        <Link
          href={href}
          className="group block focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white focus-visible:ring-inset rounded"
        >
          {rowEl}
        </Link>
      ) : (
        rowEl
      )}
      {children && (
        <div
          className="ml-4 mt-0.5 space-y-0.5 pl-3"
          style={{ borderLeft: "1px solid var(--color-outline-variant)" }}
        >
          {children}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Evaluator node
// ---------------------------------------------------------------------------

function EvaluatorNode({
  node,
  projectId,
  epicId,
}: {
  node: EvaluatorNodeState;
  projectId: string;
  epicId: string;
}) {
  const t = useT();
  const statusLabelMap: Record<string, string> = {
    evaluating: t("conversation.statusEvaluating"),
    accepted: t("conversation.statusAccepted"),
    rejected: t("conversation.statusRejected"),
  };

  const href = `/projects/${projectId}/epics/${epicId}/threads/${node.threadId}`;
  const statusText = statusLabelMap[node.status] ?? node.status;
  const isActive = node.status === "evaluating";

  return (
    <Link
      href={href}
      className={cn(
        "group flex items-center gap-2 px-2 py-1 transition-colors",
        "min-h-[32px] rounded",
        "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white focus-visible:ring-inset",
        "hover:bg-surface-container-high",
      )}
    >
      <Icon
        name="rate_review"
        className="shrink-0 text-[12px] text-on-surface-variant"
        aria-hidden
      />
      <span className="data flex-1 truncate">{t("conversation.evaluator")}</span>
      <LiveDot active={node.isStreaming} />
      <StateGlyph status={node.status} />
      <span
        className="data shrink-0"
        style={isActive ? { color: "var(--color-light)" } : undefined}
      >
        {statusText}
      </span>
    </Link>
  );
}

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

export interface ThreadTreePanelProps {
  treeState: ThreadTreeState;
  projectId: string;
  epicId: string;
  className?: string;
}

export function ThreadTreePanel({ treeState, projectId, epicId, className }: ThreadTreePanelProps) {
  const t = useT();
  const { manager, workers, evaluators } = treeState;

  // Map workerId → evaluator[].  Evaluators whose worker id does not resolve
  // to a live worker node are DIRECT evaluators (evaluator-only dispatch:
  // no worker ran — live events carry worker_id "", the REST ThreadEntry is
  // parented to the manager trial) and render as the manager's direct
  // children, siblings of the workers.
  const evalsByWorker: Record<string, EvaluatorNodeState[]> = {};
  const directEvals: EvaluatorNodeState[] = [];
  for (const ev of Object.values(evaluators)) {
    if (ev.workerId && workers[ev.workerId]) {
      if (!evalsByWorker[ev.workerId]) evalsByWorker[ev.workerId] = [];
      evalsByWorker[ev.workerId].push(ev);
    } else {
      directEvals.push(ev);
    }
  }

  const workerList = Object.values(workers);
  const hasContent = manager || workerList.length > 0 || directEvals.length > 0;

  if (!hasContent) return null;

  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <span className="data mb-1 block text-[10px] uppercase tracking-widest text-outline">
        {t("conversation.agentStateHeading")}
      </span>

      <div className="space-y-0.5">
        {/* Manager root */}
        {manager && <ManagerNode node={manager} projectId={projectId} epicId={epicId} />}

        {/* Workers (per task) + direct evaluators (evaluator-only dispatch) */}
        {(workerList.length > 0 || directEvals.length > 0) && (
          <div
            className="ml-4 space-y-0.5 pl-3"
            style={{ borderLeft: "1px solid var(--color-outline-variant)" }}
          >
            {workerList.map((worker) => (
              <WorkerNode key={worker.threadId} node={worker} projectId={projectId} epicId={epicId}>
                {evalsByWorker[worker.threadId]?.map((ev) => (
                  <EvaluatorNode key={ev.evalId} node={ev} projectId={projectId} epicId={epicId} />
                ))}
              </WorkerNode>
            ))}
            {directEvals.map((ev) => (
              <EvaluatorNode key={ev.evalId} node={ev} projectId={projectId} epicId={epicId} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
