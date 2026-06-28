"use client";

import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { Icon } from "@/components/icon";
import { Button } from "@/components/ui/button";
import { FormDialog } from "@/components/ui/form-dialog";
import type { Epic } from "@/lib/api/endpoints";
import { createEpic } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { EFFORT_OPTIONS, type ManagerEffort } from "@/lib/effort";
import { useModalMutation } from "@/lib/hooks/use-modal-mutation";
import { useT } from "@/lib/i18n/provider";

interface NewEpicModalProps {
  projectId: string;
  /** Called on successful creation. Receives the newly created Epic as an argument. */
  onCreated?: (epic: Epic) => void;
}

export function NewEpicModal({ projectId, onCreated }: NewEpicModalProps) {
  const t = useT();
  const router = useRouter();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [acceptanceCriteria, setAcceptanceCriteria] = useState("");
  const [managerEffort, setManagerEffort] = useState<ManagerEffort>("high");

  // Store the creation result in a ref because the onSuccess callback can become a stale closure
  const createdEpicRef = useRef<Epic | null>(null);

  // #8: migrated to useModalMutation to unify post-success navigation, reset, and error display.
  const { isOpen, setOpen, error, setError, isPending, submit } = useModalMutation<void>({
    mutationFn: () =>
      createEpic(projectId, {
        title,
        description,
        acceptance_criteria: acceptanceCriteria,
        manager_effort: managerEffort,
      }).then((newEpic) => {
        // Save to ref before onSuccess fires
        createdEpicRef.current = newEpic;
        return newEpic;
      }),
    invalidateKeys: [queryKeys.epics.list(projectId)],
    onSuccess: () => {
      const newEpic = createdEpicRef.current;
      createdEpicRef.current = null;
      // Reset the form
      setTitle("");
      setDescription("");
      setAcceptanceCriteria("");
      setManagerEffort("high");
      if (newEpic) {
        if (onCreated) {
          onCreated(newEpic);
        } else {
          // active_thread_id is not yet determined right after creating a new Epic, so fall back to "manager"
          const managerSeg = newEpic.active_thread_id ?? "manager";
          router.push(`/projects/${projectId}/epics/${newEpic.id}/threads/${managerSeg}`);
        }
      }
    },
    fallbackError: "Failed to create epic",
  });

  return (
    <FormDialog
      open={isOpen}
      onOpenChange={(v) => {
        setOpen(v);
        if (!v) setError(null);
      }}
      trigger={
        <Button variant="primary" size="sm" data-testid="new-epic-btn">
          <Icon name="add" className="text-[16px]" />
          {t("common.newEpic")}
        </Button>
      }
      title="New Epic"
      description={t("epics.newEpicDialogDescription")}
      error={error}
      submitLabel={
        <>
          <Icon name="rocket_launch" className="text-[16px]" />
          Create Epic
        </>
      }
      submitPendingLabel={
        <>
          <Icon name="rocket_launch" className="text-[16px]" />
          Creating…
        </>
      }
      submitDisabled={!title}
      isPending={isPending}
      onSubmit={() => submit()}
    >
      <div className="space-y-4">
        <div>
          <label
            htmlFor="epic-title"
            className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
          >
            Title
          </label>
          <input
            id="epic-title"
            data-testid="epic-title-input"
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Refactor auth flow"
            className="w-full rounded border border-outline-variant bg-surface-container-lowest px-3 py-2 text-body-md text-on-surface placeholder:text-outline focus:border-[var(--color-light)] focus:outline-none focus:ring-1 focus:ring-[var(--color-light)]"
          />
        </div>
        <div>
          <label
            htmlFor="epic-description"
            className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
          >
            {t("epics.promptLabel")}
          </label>
          <textarea
            id="epic-description"
            data-testid="epic-description-input"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={t("epics.promptPlaceholder")}
            rows={5}
            className="w-full resize-none rounded border border-outline-variant bg-surface-container-lowest px-3 py-2 text-body-md text-on-surface placeholder:text-outline focus:border-[var(--color-light)] focus:outline-none focus:ring-1 focus:ring-[var(--color-light)]"
          />
          <p className="mt-1.5 text-[11px] text-outline">{t("epics.promptHint")}</p>
        </div>
        <div>
          <label
            htmlFor="epic-acceptance-criteria"
            className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
          >
            {t("epics.acceptanceCriteriaLabel")}
          </label>
          <textarea
            id="epic-acceptance-criteria"
            data-testid="epic-ac-input"
            value={acceptanceCriteria}
            onChange={(e) => setAcceptanceCriteria(e.target.value)}
            placeholder={t("epics.acceptanceCriteriaPlaceholder")}
            rows={3}
            className="w-full resize-none rounded border border-outline-variant bg-surface-container-lowest px-3 py-2 text-body-md text-on-surface placeholder:text-outline focus:border-[var(--color-light)] focus:outline-none focus:ring-1 focus:ring-[var(--color-light)]"
          />
          <p className="mt-1.5 text-[11px] text-outline">{t("epics.acceptanceCriteriaHint")}</p>
        </div>
        <div>
          <div className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline">
            {t("epics.effortLabel")}
          </div>
          <div className="flex items-center gap-1">
            {EFFORT_OPTIONS.map((opt) => {
              const isActive = managerEffort === opt.value;
              return (
                <button
                  key={opt.value}
                  type="button"
                  data-testid={`new-epic-effort-${opt.value}`}
                  onClick={() => setManagerEffort(opt.value)}
                  className="rounded border px-3 py-1.5 font-mono text-[12px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white"
                  style={
                    isActive
                      ? {
                          borderColor: "var(--color-light)",
                          color: "var(--color-light)",
                          backgroundColor:
                            "color-mix(in oklab, var(--color-light) 10%, transparent)",
                        }
                      : {
                          borderColor: "var(--color-outline-variant)",
                          color: "var(--color-on-surface-variant)",
                        }
                  }
                  aria-pressed={isActive}
                >
                  {t(opt.labelKey)}
                </button>
              );
            })}
          </div>
          <p className="mt-1.5 text-[11px] text-outline">{t("epics.effortHint")}</p>
        </div>
        <div className="rounded border border-outline-variant/30 bg-surface-container-lowest p-3 text-[11px] text-outline">
          <Icon name="info" className="mr-1 inline text-[13px]" />A branch name will be generated
          automatically (e.g.{" "}
          <code className="font-mono">
            yukar/ep-46-{title.toLowerCase().replace(/\s+/g, "-") || "epic-title"}
          </code>
          ). The worktree will be created when the agent first touches a repo.
        </div>
      </div>
    </FormDialog>
  );
}
