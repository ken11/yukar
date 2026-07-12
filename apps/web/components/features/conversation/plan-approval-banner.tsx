"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { toast } from "sonner";
import { Icon } from "@/components/icon";
import { ApiError, approvePlan, extractDetail, getTasks } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { useT } from "@/lib/i18n/provider";

/**
 * Plan-approval control (lifecycle redesign: snapshot-bound approval).
 *
 * Approval is an explicit user operation bound to a task-plan snapshot hash —
 * a chat reply no longer grants it.  Shown near the your-turn banner while
 * the active trial is displayed and the current plan is unapproved.  The click
 * records the approval (POST /plan/approval with the backend-computed hash we
 * merely echo) and then posts a short i18n user message through the existing
 * send path — that message is what wakes the parked agent.  A 409 means the
 * plan changed underneath us: refetch and tell the user to re-review.
 */
export function PlanApprovalBanner({
  projectId,
  epicId,
  onSendMessage,
}: {
  projectId: string;
  epicId: string;
  onSendMessage: (content: string) => void;
}) {
  const t = useT();
  const qc = useQueryClient();

  const { data: tasksFile } = useQuery({
    queryKey: queryKeys.tasks.get(projectId, epicId),
    queryFn: () => getTasks(projectId, epicId),
    staleTime: 30_000,
  });

  const mutation = useMutation({
    mutationFn: (tasksHash: string) => approvePlan(projectId, epicId, tasksHash),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.tasks.get(projectId, epicId) });
      // The approval record alone does not wake a parked agent — the message does.
      onSendMessage(t("conversation.planApprovedMessage"));
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        // Stale snapshot: the plan changed after it was rendered. Refetch so the
        // user reviews (and can approve) the updated plan.
        qc.invalidateQueries({ queryKey: queryKeys.tasks.get(projectId, epicId) });
        toast.info(t("conversation.planStaleNotice"));
        return;
      }
      toast.error(t("conversation.planApproveFailed"), {
        description: extractDetail(err) ?? (err instanceof Error ? err.message : String(err)),
      });
    },
  });

  // A NEW plan snapshot (different hash) is a new approval decision: drop the
  // previous mutation result so the button re-arms. Without this, isSuccess
  // from an earlier approval would keep the button dead after a re-plan until
  // a full reload. Same-hash double-clicks stay blocked (hash unchanged → no
  // reset → isPending/isSuccess still guard).
  const planHash = tasksFile?.plan_hash;
  const resetMutation = mutation.reset;
  const prevHashRef = useRef(planHash);
  useEffect(() => {
    if (prevHashRef.current === planHash) return;
    prevHashRef.current = planHash;
    resetMutation();
  }, [planHash, resetMutation]);

  // No plan yet (no tasks) or already approved → nothing to render.
  if (!tasksFile || tasksFile.plan_approved || (tasksFile.tasks ?? []).length === 0) {
    return null;
  }

  return (
    <div
      className="shrink-0 flex items-center gap-3 px-6 py-2"
      role="status"
      style={{
        borderBottom: "1px solid color-mix(in oklab, var(--color-light) 20%, transparent)",
      }}
    >
      <span
        className="h-1.5 w-1.5 shrink-0 rounded-full"
        style={{ backgroundColor: "var(--color-light)" }}
        aria-hidden
      />
      <p className="min-w-0 flex-1 font-mono text-[11px]" style={{ color: "var(--color-light)" }}>
        {t("conversation.planUnapprovedNote")}
      </p>
      <button
        type="button"
        data-testid="approve-plan-btn"
        // isSuccess keeps the button dead during the short window between the
        // approval landing and the refetched plan_approved hiding the banner —
        // a fast double-click would otherwise post the wake message twice.
        disabled={mutation.isPending || mutation.isSuccess}
        onClick={() => mutation.mutate(tasksFile.plan_hash)}
        className="flex shrink-0 items-center gap-1.5 rounded px-2.5 py-1 font-mono text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white disabled:cursor-not-allowed disabled:opacity-50"
        style={{
          border: "1px solid color-mix(in oklab, var(--color-light) 40%, transparent)",
          color: "var(--color-light)",
          backgroundColor: "color-mix(in oklab, var(--color-light) 10%, transparent)",
        }}
      >
        <Icon name="check" className="text-[13px]" aria-hidden />
        {mutation.isPending ? t("conversation.approvingPlan") : t("conversation.approvePlan")}
      </button>
    </div>
  );
}
