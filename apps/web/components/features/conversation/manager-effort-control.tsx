"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useEpicRun } from "@/components/chrome/epic-run-context";
import { getEpic, patchEpic } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { EFFORT_OPTIONS, type ManagerEffort } from "@/lib/effort";
import { useT } from "@/lib/i18n/provider";

interface ManagerEffortControlProps {
  projectId: string;
  epicId: string;
}

export function ManagerEffortControl({ projectId, epicId }: ManagerEffortControlProps) {
  const t = useT();
  const qc = useQueryClient();

  // Pass the RSC-provided epic as initialData to prevent flash and unnecessary fetches on first render.
  // Only used under EpicShell, so useEpicRun() is always valid.
  const { epic: contextEpic } = useEpicRun();

  const { data: epic } = useQuery({
    queryKey: queryKeys.epics.detail(projectId, epicId),
    queryFn: () => getEpic(projectId, epicId),
    // Seed with the RSC-provided epic to eliminate flash before the first fetch
    initialData: contextEpic ?? undefined,
  });

  const mutation = useMutation({
    mutationFn: (effort: ManagerEffort) => patchEpic(projectId, epicId, { manager_effort: effort }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.epics.detail(projectId, epicId) });
      qc.invalidateQueries({ queryKey: queryKeys.epics.list(projectId) });
    },
    onError: (err) => {
      // Notify mutation failure via toast to prevent silent revert
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(t("conversation.effortUpdateFailed"), { description: msg });
    },
  });

  const current: ManagerEffort = epic?.manager_effort ?? "high";

  return (
    <div className="flex items-center gap-1.5" title={t("conversation.effortTooltip")}>
      <span className="address text-on-surface-variant opacity-60">
        {t("conversation.effortLabel")}
      </span>
      <div
        className="flex items-center rounded"
        style={{
          border: "1px solid var(--color-outline-variant)",
          backgroundColor: "var(--color-surface-container-lowest)",
        }}
      >
        {EFFORT_OPTIONS.map((opt) => {
          const isActive = current === opt.value;
          return (
            <button
              key={opt.value}
              type="button"
              data-testid={`effort-btn-${opt.value}`}
              disabled={mutation.isPending}
              onClick={() => {
                if (!isActive) mutation.mutate(opt.value);
              }}
              className="px-2 py-0.5 font-mono text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white disabled:cursor-not-allowed disabled:opacity-50"
              style={
                isActive
                  ? {
                      color: "var(--color-light)",
                      backgroundColor: "color-mix(in oklab, var(--color-light) 12%, transparent)",
                    }
                  : { color: "var(--color-on-surface-variant)" }
              }
              aria-pressed={isActive}
            >
              {t(opt.labelKey)}
            </button>
          );
        })}
      </div>
    </div>
  );
}
