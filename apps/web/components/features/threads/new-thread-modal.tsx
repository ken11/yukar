"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Icon } from "@/components/icon";
import { Button } from "@/components/ui/button";
import { FormDialog } from "@/components/ui/form-dialog";
import { ApiError, createThread } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { useT } from "@/lib/i18n/provider";

/**
 * Modal for creating a new manager trial.
 *
 * Flow:
 * 1. A **single request** to createThread(role:"manager", archive_active:true) causes
 *    the server to atomically archive the current active trial and create a new one.
 *    - 409: run is active → show inline error and abort (original trial is preserved).
 * 2. Navigate to the created thread via router.push and invalidate the threads list.
 */
export function NewThreadModal({ projectId, epicId }: { projectId: string; epicId: string }) {
  const t = useT();
  const router = useRouter();
  const qc = useQueryClient();
  const [isOpen, setIsOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [title, setTitle] = useState("");

  const mutation = useMutation({
    mutationFn: async () => {
      // archive_active:true atomically archives the current active trial and creates a new one.
      // If title is empty, omit it and let the backend auto-number it as "Trial N".
      const newThread = await createThread(projectId, epicId, {
        role: "manager",
        archive_active: true,
        title: title.trim() || "",
      });
      return newThread;
    },
    onSuccess: (newThread) => {
      // Invalidate threads.list to refresh the left-pane thread list
      qc.invalidateQueries({
        queryKey: queryKeys.threads.list(projectId, epicId),
      });
      // Invalidate epics.detail to refresh EpicShell's liveEpic.active_thread_id.
      // EpicShell subscribes to epics.detail via useQuery and will re-fetch after invalidation
      // to get the latest active_thread_id (the new trial id).
      // This ensures the composer is correctly shown after in-app navigation even when the layout RSC is stale.
      qc.invalidateQueries({
        queryKey: queryKeys.epics.detail(projectId, epicId),
      });
      setIsOpen(false);
      setError(null);
      setTitle("");
      // router.refresh() also re-fetches the layout RSC (updating parent queries such as epics.list).
      // This is a one-time re-fetch after mutation, not polling.
      router.refresh();
      router.push(`/projects/${projectId}/epics/${epicId}/threads/${newThread.id}`);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        setError(t("common.archiveStopFirst"));
      } else {
        setError(err instanceof Error ? err.message : "Failed to create trial");
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
        <Button variant="primary" size="sm" data-testid="new-thread-btn">
          <Icon name="add_comment" className="text-[16px]" />
          {t("common.newTrial")}
        </Button>
      }
      title={t("common.newTrial")}
      description={t("common.newTrialDescription")}
      error={error}
      submitLabel={
        <>
          <Icon name="rocket_launch" className="text-[16px]" />
          {t("common.newTrial")}
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
