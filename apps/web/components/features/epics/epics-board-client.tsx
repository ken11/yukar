"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useCallback, useState } from "react";
import { Icon } from "@/components/icon";
import { EmptyState } from "@/components/ui/empty-state";
import { StatusBadge } from "@/components/ui/status-badge";
import type { Epic } from "@/lib/api/endpoints";
import { ApiError, listEpics, startMerge } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { isTerminalStatus } from "@/lib/epic-utils";
import { useT } from "@/lib/i18n/provider";
import { MergeProgressPanel } from "./merge-progress-panel";
import { NewEpicModal } from "./new-epic-modal";
import { useCloseEpic, useReopenEpic } from "./use-close-epic";

/**
 * Filter values for the board.
 * Default ("all") HIDES closed only — merged is included in "all".
 * closed/merged only appear in isolation when their chip is explicitly selected.
 */
type FilterValue =
  | "all"
  | "planned"
  | "in_progress"
  | "in_review"
  | "completed"
  | "failed"
  | "closed"
  | "merged";

interface EpicsBoardClientProps {
  projectId: string;
  initialEpics: Epic[];
}

/** epics that are "mergeable" = have a branch and are not already closed/merged */
function isMergeable(e: Epic): boolean {
  // #5: unify closed/merged detection with isTerminalStatus()
  return !!e.branch && !isTerminalStatus(e.status);
}

/**
 * EpicsBoardClient — board index for /projects/[p]/epics.
 * Receives initialEpics from RSC and live-updates via TanStack Query.
 * Status filter runs on the client only.
 * Default view hides closed only; merged is shown in "all".
 * Multi-select mode launches an arbiter batch merge.
 */
export function EpicsBoardClient({ projectId, initialEpics }: EpicsBoardClientProps) {
  const t = useT();
  const qc = useQueryClient();

  // Fetch all records with include_closed=true (filtering is done on the client)
  const { data: epics = initialEpics } = useQuery({
    queryKey: queryKeys.epics.list(projectId),
    queryFn: () => listEpics(projectId, true),
    initialData: initialEpics,
    staleTime: 30_000,
  });

  const [filter, setFilter] = useState<FilterValue>("all");
  /** selection: ordered list of epic ids (order = merge order) */
  const [selected, setSelected] = useState<string[]>([]);
  const [mergeError, setMergeError] = useState<string | null>(null);
  const [mergeRunId, setMergeRunId] = useState<string | null>(null);

  const mergeMutation = useMutation({
    mutationFn: (epicIds: string[]) => startMerge(projectId, epicIds),
    onSuccess: (data) => {
      setSelected([]);
      setMergeRunId(data.run_id);
      setMergeError(null);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        setMergeError(t("epicsBoard.multiSelect.conflictError"));
      } else {
        setMergeError(err instanceof Error ? err.message : String(err));
      }
    },
  });

  const filterOptions: { value: FilterValue; labelKey: string }[] = [
    { value: "all", labelKey: "epicsBoard.filter.all" },
    { value: "planned", labelKey: "epic.status.planned" },
    { value: "in_progress", labelKey: "epic.status.running" },
    { value: "in_review", labelKey: "epic.status.in_review" },
    { value: "completed", labelKey: "epic.status.completed" },
    { value: "failed", labelKey: "epic.status.error" },
    { value: "closed", labelKey: "epic.status.closed" },
    { value: "merged", labelKey: "epic.status.merged" },
  ];

  const filtered =
    filter === "all"
      ? // "all" hides only closed; merged is included (merged = successful terminal state)
        epics.filter((e) => e.status !== "closed")
      : filter === "closed"
        ? epics.filter((e) => e.status === "closed")
        : filter === "merged"
          ? epics.filter((e) => e.status === "merged")
          : epics.filter((e) => e.status === filter);

  const toggleSelect = useCallback((epicId: string) => {
    setSelected((prev) =>
      prev.includes(epicId) ? prev.filter((id) => id !== epicId) : [...prev, epicId],
    );
  }, []);

  const clearSelection = useCallback(() => setSelected([]), []);

  const handleStartMerge = () => {
    setMergeError(null);
    mergeMutation.mutate(selected);
  };

  const isSelecting = selected.length > 0;

  // Invalidate when merge panel reports progress (callback passed down)
  const handleMergeInvalidate = useCallback(() => {
    qc.invalidateQueries({ queryKey: queryKeys.epics.list(projectId) });
  }, [qc, projectId]);

  return (
    <div className="px-4 pb-20 md:px-8 md:pb-16">
      {/* Header */}
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-[18px] font-semibold text-on-surface">{t("epicsBoard.title")}</h1>
        <div className="flex items-center gap-2">
          {/* multi-select hint */}
          {!isSelecting && (
            <span className="hidden font-mono text-[11px] text-outline md:block">
              {t("epicsBoard.multiSelect.selectHint")}
            </span>
          )}
          <NewEpicModal projectId={projectId} />
        </div>
      </div>

      {/* merge progress panel (shown when a run_id is active) */}
      {mergeRunId && (
        <MergeProgressPanel
          projectId={projectId}
          onInvalidate={handleMergeInvalidate}
          onDismiss={() => setMergeRunId(null)}
        />
      )}

      {/* Status filter */}
      <div className="mb-6 flex flex-wrap items-center gap-2">
        {filterOptions.map(({ value, labelKey }) => (
          <button
            key={value}
            type="button"
            data-testid={`epic-filter-${value}`}
            onClick={() => setFilter(value)}
            className={
              filter === value
                ? "data rounded border border-outline-variant bg-surface-container-highest px-3 py-1 uppercase text-on-surface"
                : "data rounded border border-outline-variant px-3 py-1 uppercase text-on-surface-variant transition-colors hover:text-on-surface"
            }
            style={
              filter === value
                ? { boxShadow: "inset 0 -2px 0 0 var(--color-on-surface)" }
                : undefined
            }
          >
            {t(labelKey)}
          </button>
        ))}
      </div>

      {/* Selection toolbar */}
      {isSelecting && (
        <div
          className="mb-4 flex flex-wrap items-center gap-3 rounded border border-outline-variant bg-surface-container px-4 py-3"
          data-testid="merge-toolbar"
        >
          <span className="font-mono text-[12px] text-on-surface-variant">
            {selected.length} selected
          </span>
          {mergeError && <span className="font-mono text-[11px] text-error">{mergeError}</span>}
          <div className="ml-auto flex items-center gap-2">
            <button
              type="button"
              onClick={clearSelection}
              disabled={mergeMutation.isPending}
              className="rounded border border-outline-variant px-3 py-1.5 text-body-sm text-on-surface-variant transition-colors hover:text-on-surface disabled:opacity-50"
            >
              {t("epicsBoard.multiSelect.cancel")}
            </button>
            <button
              type="button"
              data-testid="start-merge-btn"
              onClick={handleStartMerge}
              disabled={mergeMutation.isPending || selected.length === 0}
              className="flex items-center gap-1.5 rounded bg-primary px-3 py-1.5 text-body-sm font-medium text-on-primary transition-colors hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Icon name="merge" className="text-[16px]" />
              {mergeMutation.isPending
                ? t("epicsBoard.multiSelect.merging")
                : t("epicsBoard.multiSelect.mergeSelected")}
            </button>
          </div>
        </div>
      )}

      {/* Epic list */}
      {epics.length === 0 ? (
        <EmptyState
          address={`${projectId} ／ epics`}
          message={t("empty.noEpicsProject")}
          action={<NewEpicModal projectId={projectId} />}
        />
      ) : filtered.length === 0 ? (
        <EmptyState message={t("empty.noEpics")} />
      ) : (
        <div style={{ borderTop: "1px solid var(--edge-shadow)" }}>
          {filtered.map((epic) => (
            <EpicBoardRow
              key={epic.id}
              epic={epic}
              projectId={projectId}
              isSelected={selected.includes(epic.id)}
              onToggleSelect={isMergeable(epic) ? toggleSelect : undefined}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/** Row-based — .edge-h hairline, left-aligned (avoid card overuse) */
function EpicBoardRow({
  epic,
  projectId,
  isSelected,
  onToggleSelect,
}: {
  epic: Epic;
  projectId: string;
  isSelected: boolean;
  onToggleSelect?: (epicId: string) => void;
}) {
  const t = useT();
  const managerSeg = epic.active_thread_id ?? "manager";
  const href = `/projects/${projectId}/epics/${epic.id}/threads/${managerSeg}`;
  const isRunning = epic.status === "in_progress";
  const isClosed = epic.status === "closed";
  const isMerged = epic.status === "merged";

  const closeMutation = useCloseEpic(projectId);
  const reopenMutation = useReopenEpic(projectId);

  const statusForBadge = isClosed ? "closed" : isMerged ? "merged" : epic.status;

  return (
    <div
      data-testid={`epic-card-${epic.id}`}
      data-epic-status={epic.status}
      className={cn(
        "flex items-center gap-3 py-3 transition-colors hover:bg-surface-container md:gap-6 md:py-4",
      )}
      style={{
        borderBottom: "1px solid var(--edge-shadow)",
        paddingLeft: "16px",
        boxShadow: isRunning ? "inset 2px 0 0 0 var(--color-light)" : undefined,
        opacity: isClosed || isMerged ? 0.6 : undefined,
      }}
    >
      {/* Checkbox (mergeable epics only) */}
      {onToggleSelect ? (
        <button
          type="button"
          aria-label={isSelected ? `Deselect ${epic.id}` : `Select ${epic.id}`}
          onClick={(e) => {
            e.preventDefault();
            onToggleSelect(epic.id);
          }}
          className={cn(
            "flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors",
            isSelected
              ? "border-on-surface bg-on-surface"
              : "border-outline-variant hover:border-outline",
          )}
        >
          {isSelected && <Icon name="check" className="text-[11px] text-surface" />}
        </button>
      ) : (
        <span className="h-4 w-4 shrink-0" />
      )}

      {/* EP-id — fixed-width tabular (narrower on mobile) */}
      <Link href={href} className="contents" tabIndex={-1} aria-hidden>
        <span className="data w-14 shrink-0 md:w-20" style={{ letterSpacing: "0.04em" }}>
          {epic.id}
        </span>
      </Link>

      {/* Title + description — flex-1 */}
      <Link href={href} className="min-w-0 flex-1 focus-visible:outline-none">
        <span className="font-sans text-[14px] font-semibold text-on-surface">{epic.title}</span>
        {epic.description && (
          <p className="mt-0.5 truncate text-[12px] text-on-surface-variant">{epic.description}</p>
        )}
      </Link>

      {/* StatusBadge */}
      <StatusBadge status={statusForBadge as Parameters<typeof StatusBadge>[0]["status"]} />

      {/* Inline close / reopen action */}
      {isClosed ? (
        <button
          type="button"
          data-testid={`reopen-btn-${epic.id}`}
          onClick={() => reopenMutation.mutate(epic.id)}
          disabled={reopenMutation.isPending}
          title={t("epic.reopen")}
          className="shrink-0 rounded border border-outline-variant px-2 py-1 font-mono text-[11px] text-on-surface-variant transition-colors hover:text-on-surface disabled:opacity-50"
        >
          {t("epic.reopen")}
        </button>
      ) : !isMerged && epic.status !== "in_progress" ? (
        <button
          type="button"
          data-testid={`close-btn-${epic.id}`}
          onClick={() => closeMutation.mutate(epic.id)}
          disabled={closeMutation.isPending}
          title={t("epic.close")}
          className="shrink-0 rounded border border-outline-variant px-2 py-1 font-mono text-[11px] text-on-surface-variant transition-colors hover:text-on-surface disabled:opacity-50"
        >
          <Icon name="lock" className="text-[13px]" />
        </button>
      ) : null}

      {/* chevron */}
      <Link
        href={href}
        className="shrink-0 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white focus-visible:ring-inset"
        aria-label={epic.id}
      >
        <Icon name="chevron_right" className="text-[18px] text-on-surface-variant" />
      </Link>
    </div>
  );
}
