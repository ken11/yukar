"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Icon } from "@/components/icon";
import { Button } from "@/components/ui/button";
import { FormDialog } from "@/components/ui/form-dialog";
import { ApiError, createThread, extractDetail } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { useT } from "@/lib/i18n/provider";

/**
 * Modal for starting a new manager conversation session.
 *
 * Two variants (a *trial* is a branch+worktree line of work; a *session* is a
 * fresh conversation attached to it):
 * - "new" (default): start a NEW trial — archive the active trial and branch off
 *   the default branch (ordinal-suffixed branch name).
 *   Request: createThread(role:"manager", archive_active:true).
 * - "sameBranch": CONTINUE the current trial (same branch + worktree) with a
 *   fresh conversation. The previous conversation is archived (kept as history)
 *   but the worktree is preserved.
 *   Request: createThread(role:"manager", same_branch:true).
 *
 * Flow:
 * 1. A **single request** atomically archives the previous conversation and
 *    creates the new one.  409 → run is active → inline error, abort.
 * 2. Navigate to the created thread and invalidate the threads list + epic detail.
 */
export function NewThreadModal({
  projectId,
  epicId,
  variant = "new",
  compact = false,
}: {
  projectId: string;
  epicId: string;
  variant?: "new" | "sameBranch";
  /** compact = quiet full-width ghost row (persistent sidebar); default = a
   *  filled/outline button (mobile drawer, archived banner). */
  compact?: boolean;
}) {
  const t = useT();
  const router = useRouter();
  const qc = useQueryClient();
  const [isOpen, setIsOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [title, setTitle] = useState("");

  const isSameBranch = variant === "sameBranch";
  const label = isSameBranch ? t("common.continueBranch") : t("common.newTrial");
  const description = isSameBranch
    ? t("common.continueBranchDescription")
    : t("common.newTrialDescription");
  const buttonIcon = isSameBranch ? "edit_note" : "add_comment";

  const mutation = useMutation({
    mutationFn: async () => {
      // "new": archive_active atomically archives the active trial and forks a new branch.
      // "sameBranch": same_branch continues the current trial with a fresh conversation.
      // If title is empty, omit it and let the backend auto-number it as "Trial N".
      const newThread = await createThread(
        projectId,
        epicId,
        isSameBranch
          ? { role: "manager", archive_active: false, same_branch: true, title: title.trim() || "" }
          : {
              role: "manager",
              archive_active: true,
              same_branch: false,
              title: title.trim() || "",
            },
      );
      return newThread;
    },
    onSuccess: (newThread) => {
      // Invalidate threads.list to refresh the left-pane thread list.
      qc.invalidateQueries({
        queryKey: queryKeys.threads.list(projectId, epicId),
      });
      // Invalidate epics.detail so EpicShell picks up the new active_thread_id.
      qc.invalidateQueries({
        queryKey: queryKeys.epics.detail(projectId, epicId),
      });
      setIsOpen(false);
      setError(null);
      setTitle("");
      router.refresh();
      router.push(`/projects/${projectId}/epics/${epicId}/threads/${newThread.id}`);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        // Surface the backend's real reason (active run vs. no active trial vs.
        // dangling active_thread_id) instead of a fixed "stop the run" guess.
        setError(extractDetail(err) ?? t("common.archiveStopFirst"));
      } else {
        setError(err instanceof Error ? err.message : "Failed to create session");
      }
    },
  });

  return (
    <FormDialog
      open={isOpen}
      onOpenChange={(v) => {
        setIsOpen(v);
        if (!v) {
          setError(null);
          setTitle("");
          mutation.reset();
        }
      }}
      trigger={
        <Button
          variant={compact ? "ghost" : isSameBranch ? "secondary" : "primary"}
          size="sm"
          data-testid={isSameBranch ? "continue-branch-btn" : "new-thread-btn"}
          className={compact ? "w-full justify-start" : undefined}
        >
          <Icon name={buttonIcon} className="text-[16px]" />
          {label}
        </Button>
      }
      title={label}
      description={description}
      error={error}
      submitLabel={
        <>
          <Icon name="rocket_launch" className="text-[16px]" />
          {label}
        </>
      }
      submitPendingLabel={
        <>
          <Icon name="rocket_launch" className="text-[16px]" />
          {t("common.creating")}
        </>
      }
      submitDisabled={false}
      isPending={mutation.isPending}
      onSubmit={() => mutation.mutate()}
    >
      {/* Optional title input — if submitted empty, the backend auto-numbers it as "Trial N" */}
      <div className="flex flex-col gap-1.5">
        <label htmlFor="trial-title" className="text-label-sm text-on-surface-variant">
          {t("common.trialTitle")}
        </label>
        <input
          id="trial-title"
          data-testid="trial-title-input"
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder={t("common.trialTitlePlaceholder")}
          className="w-full rounded border px-3 py-2 text-body-sm text-on-surface placeholder:text-outline focus:outline-none focus:ring-1 focus:ring-white"
          style={{
            backgroundColor: "var(--color-surface-container)",
            borderColor: "var(--color-outline-variant)",
          }}
          disabled={mutation.isPending}
        />
      </div>
    </FormDialog>
  );
}
