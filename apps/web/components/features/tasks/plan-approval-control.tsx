"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Icon } from "@/components/icon";
import type { TasksResponse } from "@/lib/api/endpoints";
import {
  ApiError,
  approvePlan,
  extractDetail,
  getEpic,
  postMessage,
  revokePlanApproval,
} from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { useT } from "@/lib/i18n/provider";

/**
 * Always-available plan-approval lever on the tasks page.
 *
 * The conversation banner appears only when the active-trial thread is open
 * AND the cached snapshot says "unapproved" — when any of that goes sideways
 * (missed SSE, stale cache, wrong thread) the user is left staring at a
 * parked Manager with no way to act.  This control renders whenever a plan
 * exists, directly next to the approval-state datum, and works from the
 * backend's current truth: approve when unapproved, revoke when approved.
 *
 * Approving also posts the same i18n wake message the banner posts (the
 * approval record alone never wakes a parked agent — turn-end semantics).
 * If the wake cannot be delivered (e.g. a reviewer run is executing) the
 * approval still stands; the user is told to reply in the conversation.
 */
export function PlanApprovalControl({
  projectId,
  epicId,
  tasksFile,
}: {
  projectId: string;
  epicId: string;
  tasksFile: TasksResponse;
}) {
  const t = useT();
  const qc = useQueryClient();
  const invalidateTasks = () =>
    qc.invalidateQueries({ queryKey: queryKeys.tasks.get(projectId, epicId) });

  const approve = useMutation({
    mutationFn: (tasksHash: string) => approvePlan(projectId, epicId, tasksHash),
    onSuccess: async () => {
      invalidateTasks();
      try {
        const epic = await getEpic(projectId, epicId);
        await postMessage(projectId, epicId, epic.active_thread_id ?? "manager", {
          content: t("conversation.planApprovedMessage"),
          role: "user",
        });
      } catch {
        // The approval is recorded either way — only the wake failed.
        toast.info(t("tasks.approvedButWakeFailed"));
      }
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        invalidateTasks();
        toast.info(t("conversation.planStaleNotice"));
        return;
      }
      toast.error(t("conversation.planApproveFailed"), {
        description: extractDetail(err) ?? (err instanceof Error ? err.message : String(err)),
      });
    },
  });

  const revoke = useMutation({
    mutationFn: () => revokePlanApproval(projectId, epicId),
    onSuccess: () => {
      invalidateTasks();
      toast.info(t("tasks.approvalRevoked"));
    },
    onError: (err) => {
      toast.error(t("tasks.revokeFailed"), {
        description: extractDetail(err) ?? (err instanceof Error ? err.message : String(err)),
      });
    },
  });

  if ((tasksFile.tasks ?? []).length === 0) return null;

  if (!tasksFile.plan_approved) {
    return (
      <button
        type="button"
        data-testid="tasks-approve-plan-btn"
        disabled={approve.isPending}
        onClick={() => approve.mutate(tasksFile.plan_hash)}
        className="flex shrink-0 items-center gap-1.5 rounded px-2.5 py-1 font-mono text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white disabled:cursor-not-allowed disabled:opacity-50"
        style={{
          border: "1px solid color-mix(in oklab, var(--color-light) 40%, transparent)",
          color: "var(--color-light)",
          backgroundColor: "color-mix(in oklab, var(--color-light) 10%, transparent)",
        }}
      >
        <Icon name="check" className="text-[13px]" aria-hidden />
        {approve.isPending ? t("conversation.approvingPlan") : t("conversation.approvePlan")}
      </button>
    );
  }

  return (
    <button
      type="button"
      data-testid="tasks-revoke-approval-btn"
      disabled={revoke.isPending}
      onClick={() => revoke.mutate()}
      className="flex shrink-0 items-center gap-1.5 rounded px-2.5 py-1 font-mono text-[11px] text-on-surface-variant transition-colors hover:text-on-surface focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white disabled:cursor-not-allowed disabled:opacity-50"
      style={{ border: "1px solid var(--color-outline-variant)" }}
    >
      <Icon name="undo" className="text-[13px]" aria-hidden />
      {t("tasks.revokeApproval")}
    </button>
  );
}
