"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Icon } from "@/components/icon";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { StatusBadge } from "@/components/ui/status-badge";
import type { EpicArchiveResult, EpicWithRunSummary } from "@/lib/api/endpoints";
import { ApiError, archiveEpics, listEpics, startMerge } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { hasYourTurn } from "@/lib/epic-utils";
import { useT } from "@/lib/i18n/provider";
import { useProjectEventStream } from "@/lib/sse/project-event-stream-context";
import { MergeProgressPanel } from "./merge-progress-panel";
import { NewEpicModal } from "./new-epic-modal";
import { useCompleteEpic, useReopenEpic } from "./use-close-epic";

/**
 * Filter values for the board.
 * The epic status is a single user-owned bit (open ⇄ completed); "merged" is a
 * fact attribute (merged_at) — a merged epic can be either open or completed.
 * "all" shows everything.
 */
type FilterValue = "all" | "open" | "completed" | "merged";

interface EpicsBoardClientProps {
  projectId: string;
  initialEpics: EpicWithRunSummary[];
}

/** epics that are "mergeable" = open, have a branch, and no recorded merge fact */
function isMergeable(e: EpicWithRunSummary): boolean {
  return !!e.branch && e.status === "open" && !e.merged_at;
}

/** Locale keys for the archive failures the backend reports with a stable code. */
const ARCHIVE_ERROR_KEYS: Record<string, string> = {
  run_active: "epicsBoard.archive.errors.runActive",
  merge_active: "epicsBoard.archive.errors.mergeActive",
  dest_exists: "epicsBoard.archive.errors.destExists",
  not_found: "epicsBoard.archive.errors.notFound",
  invalid_id: "epicsBoard.archive.errors.notFound",
  worktree_failed: "epicsBoard.archive.errors.worktreeFailed",
};

/**
 * EpicsBoardClient — the epic list with complete/reopen, multi-select merge
 * (Arbiter) and multi-select archive.  Embedded on the project overview
 * (/projects/[p]); /projects/[p]/epics redirects there.
 * Receives initialEpics from RSC and live-updates via TanStack Query.
 * Status filter runs on the client only.
 */
export function EpicsBoardClient({ projectId, initialEpics }: EpicsBoardClientProps) {
  const t = useT();
  const qc = useQueryClient();

  // Fetch all records with include_completed=true (filtering is done on the client)
  const { data: epics = initialEpics } = useQuery({
    queryKey: queryKeys.epics.list(projectId),
    queryFn: () => listEpics(projectId, true),
    initialData: initialEpics,
    staleTime: 30_000,
  });

  // Live "your turn" badges: the project SSE streams the your-turn
  // signals (your_turn = a conversation run parked in "waiting",
  // your_turn_ended = the reply woke it). Patch the run_summary of the
  // affected epic in the list cache — badge only, no unread persistence.
  const { subscribe } = useProjectEventStream();
  useEffect(() => {
    return subscribe(({ data }) => {
      if (data.type !== "your_turn" && data.type !== "your_turn_ended") return;
      qc.setQueryData<EpicWithRunSummary[]>(queryKeys.epics.list(projectId), (prev) => {
        if (!prev) return prev;
        return prev.map((e) => {
          if (e.id !== data.epic_id) return e;
          if (data.type === "your_turn") {
            // The run parked — it is the user's turn on this epic.
            // NOTE: role / last_event_at are carried over from the previous
            // summary (the event carries neither) and may be stale until the
            // next REST refetch — acceptable while the board renders neither.
            return {
              ...e,
              run_summary: {
                role: "manager",
                ...(e.run_summary ?? {}),
                status: "waiting",
                run_id: data.run_id,
                thread_id: data.thread_id,
              },
            };
          }
          // your_turn_ended: only a waiting summary reverts to running —
          // a delayed signal must not fake execution after stop/error.
          if (e.run_summary?.status !== "waiting") return e;
          return { ...e, run_summary: { ...e.run_summary, status: "running" } };
        });
      });
    });
  }, [subscribe, qc, projectId]);

  const [filter, setFilter] = useState<FilterValue>("all");
  /** selection: ordered list of epic ids (order = merge order) */
  const [selected, setSelected] = useState<string[]>([]);
  const [actionError, setActionError] = useState<string | null>(null);
  const [mergeRunId, setMergeRunId] = useState<string | null>(null);
  const [archiveDialogOpen, setArchiveDialogOpen] = useState(false);

  const mergeMutation = useMutation({
    mutationFn: (epicIds: string[]) => startMerge(projectId, epicIds),
    onSuccess: (data) => {
      setSelected([]);
      setMergeRunId(data.run_id);
      setActionError(null);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        setActionError(t("epicsBoard.multiSelect.conflictError"));
      } else {
        setActionError(err instanceof Error ? err.message : String(err));
      }
    },
  });

  const describeArchiveError = (r: EpicArchiveResult): string => {
    const key = r.error_code ? ARCHIVE_ERROR_KEYS[r.error_code] : undefined;
    return `${r.epic_id}: ${key ? t(key) : (r.error ?? "")}`;
  };

  const archiveMutation = useMutation({
    mutationFn: (epicIds: string[]) => archiveEpics(projectId, epicIds),
    onSuccess: (results) => {
      setArchiveDialogOpen(false);
      const failed = results.filter((r) => !r.archived);
      // Drop only the epics this batch actually archived — failed ones stay
      // selected for retry, and selections made while the mutation was in
      // flight are preserved.
      setSelected((prev) =>
        prev.filter((id) => !results.some((r) => r.epic_id === id && r.archived)),
      );
      setActionError(
        failed.length > 0
          ? t("epicsBoard.archive.partialError").replace(
              "{errors}",
              failed.map(describeArchiveError).join(" / "),
            )
          : null,
      );
      qc.invalidateQueries({ queryKey: queryKeys.epics.list(projectId) });
    },
    onError: (err) => {
      setArchiveDialogOpen(false);
      setActionError(err instanceof Error ? err.message : String(err));
    },
  });

  const filterOptions: { value: FilterValue; labelKey: string }[] = [
    { value: "all", labelKey: "epicsBoard.filter.all" },
    { value: "open", labelKey: "epic.status.open" },
    { value: "completed", labelKey: "epic.status.completed" },
    { value: "merged", labelKey: "epic.status.merged" },
  ];

  const filtered =
    filter === "all"
      ? epics
      : filter === "merged"
        ? // merged is a fact attribute, not a status — filter on merged_at
          epics.filter((e) => !!e.merged_at)
        : epics.filter((e) => e.status === filter);

  const toggleSelect = useCallback((epicId: string) => {
    setSelected((prev) =>
      prev.includes(epicId) ? prev.filter((id) => id !== epicId) : [...prev, epicId],
    );
  }, []);

  const clearSelection = useCallback(() => setSelected([]), []);

  // A selection can outlive the list (live updates / another tab archiving an
  // epic remove rows without touching `selected`).  Every consumer — count,
  // guards, and the mutation payloads — must see only ids that still exist.
  const liveSelected = selected.filter((id) => epics.some((e) => e.id === id));

  const handleStartMerge = () => {
    setActionError(null);
    mergeMutation.mutate(liveSelected);
  };

  const isSelecting = liveSelected.length > 0;
  const selectedEpics = epics.filter((e) => liveSelected.includes(e.id));
  const allSelectedMergeable = selectedEpics.length > 0 && selectedEpics.every(isMergeable);

  // Invalidate when merge panel reports progress (callback passed down)
  const handleMergeInvalidate = useCallback(() => {
    qc.invalidateQueries({ queryKey: queryKeys.epics.list(projectId) });
  }, [qc, projectId]);

  return (
    <div className="pb-20 md:pb-16">
      {/* merge progress panel (shown when a run_id is active) */}
      {mergeRunId && (
        <MergeProgressPanel
          projectId={projectId}
          onInvalidate={handleMergeInvalidate}
          onDismiss={() => setMergeRunId(null)}
        />
      )}

      {/* Status filter + multi-select hint */}
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
        {!isSelecting && (
          <span className="ml-auto hidden font-mono text-[11px] text-outline md:block">
            {t("epicsBoard.multiSelect.selectHint")}
          </span>
        )}
      </div>

      {/* Selection toolbar */}
      {isSelecting && (
        <div
          className="mb-4 flex flex-wrap items-center gap-3 rounded border border-outline-variant bg-surface-container px-4 py-3"
          data-testid="merge-toolbar"
        >
          <span className="font-mono text-[12px] text-on-surface-variant">
            {t("epicsBoard.multiSelect.selectedCount").replace(
              "{count}",
              String(liveSelected.length),
            )}
          </span>
          {actionError && <span className="font-mono text-[11px] text-error">{actionError}</span>}
          <div className="ml-auto flex items-center gap-2">
            <button
              type="button"
              onClick={clearSelection}
              disabled={mergeMutation.isPending || archiveMutation.isPending}
              className="rounded border border-outline-variant px-3 py-1.5 text-body-sm text-on-surface-variant transition-colors hover:text-on-surface disabled:opacity-50"
            >
              {t("epicsBoard.multiSelect.cancel")}
            </button>
            <button
              type="button"
              data-testid="archive-selected-btn"
              onClick={() => setArchiveDialogOpen(true)}
              disabled={archiveMutation.isPending}
              className="flex items-center gap-1.5 rounded border border-outline-variant px-3 py-1.5 text-body-sm text-on-surface-variant transition-colors hover:text-on-surface disabled:opacity-50"
            >
              <Icon name="archive" className="text-[16px]" />
              {t("epicsBoard.archive.archiveSelected")}
            </button>
            <button
              type="button"
              data-testid="start-merge-btn"
              onClick={handleStartMerge}
              disabled={
                mergeMutation.isPending || archiveMutation.isPending || !allSelectedMergeable
              }
              title={
                allSelectedMergeable ? undefined : t("epicsBoard.multiSelect.mergeNeedsMergeable")
              }
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

      {/* Archive confirmation — irreversible from the UI, so always confirm.
          While the mutation is in flight the dialog stays up (Esc/overlay/X
          included) so the late onSuccess cannot surprise the user. */}
      <Dialog
        open={archiveDialogOpen}
        onOpenChange={(open) => {
          if (!open && archiveMutation.isPending) return;
          setArchiveDialogOpen(open);
        }}
      >
        <DialogContent title={t("epicsBoard.archive.confirmTitle")}>
          <p className="whitespace-pre-line text-body-md text-on-surface-variant">
            {t("epicsBoard.archive.confirmBody").replace("{count}", String(liveSelected.length))}
          </p>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setArchiveDialogOpen(false)}>
              {t("epicsBoard.archive.cancel")}
            </Button>
            <Button
              variant="danger"
              data-testid="confirm-archive-btn"
              disabled={archiveMutation.isPending}
              onClick={() => archiveMutation.mutate(liveSelected)}
            >
              {archiveMutation.isPending
                ? t("epicsBoard.archive.archiving")
                : t("epicsBoard.archive.confirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

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
              onToggleSelect={toggleSelect}
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
  epic: EpicWithRunSummary;
  projectId: string;
  isSelected: boolean;
  onToggleSelect: (epicId: string) => void;
}) {
  const t = useT();
  const managerSeg = epic.active_thread_id ?? "manager";
  const href = `/projects/${projectId}/epics/${epic.id}/threads/${managerSeg}`;
  const isCompleted = epic.status === "completed";
  const isMerged = !!epic.merged_at;
  const isYourTurn = hasYourTurn(epic);

  const completeMutation = useCompleteEpic(projectId);
  const reopenMutation = useReopenEpic(projectId);

  return (
    <div
      data-testid={`epic-card-${epic.id}`}
      data-epic-status={epic.status}
      className={cn(
        // Mobile: wrap into two lines (id/status/actions, then full-width title).
        // Desktop (md:): single row.
        // relative: anchors the stretched link that makes the whole row clickable.
        "relative flex flex-wrap items-center gap-3 py-3 transition-colors hover:bg-surface-container md:flex-nowrap md:gap-6 md:py-4",
      )}
      style={{
        borderBottom: "1px solid var(--edge-shadow)",
        paddingLeft: "16px",
        opacity: isCompleted ? 0.6 : undefined,
      }}
    >
      {/* Stretched link — the whole row navigates; the controls below sit
          above it (positioned elements later in the DOM paint over it). */}
      <Link
        href={href}
        data-testid={`epic-item-${epic.id}`}
        aria-label={`${epic.id} ${epic.title}`}
        className="absolute inset-0 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white focus-visible:ring-inset"
      />

      {/* Checkbox — selects for merge / archive */}
      <button
        type="button"
        aria-label={isSelected ? `Deselect ${epic.id}` : `Select ${epic.id}`}
        onClick={(e) => {
          e.preventDefault();
          onToggleSelect(epic.id);
        }}
        className={cn(
          "relative flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors",
          isSelected
            ? "border-on-surface bg-on-surface"
            : "border-outline-variant hover:border-outline",
        )}
      >
        {isSelected && <Icon name="check" className="text-[11px] text-surface" />}
      </button>

      {/* EP-id — fixed-width tabular (narrower on mobile) */}
      <span className="data w-14 shrink-0 md:w-20" style={{ letterSpacing: "0.04em" }}>
        {epic.id}
      </span>

      {/* Title + description — full-width second line on mobile, flex-1 inline on desktop */}
      <span className="order-last w-full min-w-0 pl-7 md:order-none md:w-auto md:flex-1 md:pl-0">
        <span className="font-sans text-[14px] font-semibold text-on-surface">{epic.title}</span>
        {epic.description && (
          <p className="mt-0.5 truncate text-[12px] text-on-surface-variant">{epic.description}</p>
        )}
      </span>

      {/* StatusBadge — pushed right on mobile (title is on its own line).
          The merged badge is a fact attribute shown alongside the status;
          "your turn" is current run state from run_summary, live-updated
          via the project SSE your-turn signals. */}
      <span className="ml-auto flex items-center gap-2 md:ml-0">
        {isYourTurn && (
          <span data-testid={`your-turn-${epic.id}`} className="contents">
            <StatusBadge status="awaiting" />
          </span>
        )}
        {isMerged && <StatusBadge status="merged" />}
        <StatusBadge status={epic.status} />
      </span>

      {/* Inline complete / reopen action */}
      {isCompleted ? (
        <button
          type="button"
          data-testid={`reopen-btn-${epic.id}`}
          onClick={() => reopenMutation.mutate(epic.id)}
          disabled={reopenMutation.isPending}
          title={t("epic.reopen")}
          className="relative shrink-0 rounded border border-outline-variant px-2 py-1 font-mono text-[11px] text-on-surface-variant transition-colors hover:text-on-surface disabled:opacity-50"
        >
          {t("epic.reopen")}
        </button>
      ) : (
        <button
          type="button"
          data-testid={`complete-btn-${epic.id}`}
          onClick={() => completeMutation.mutate(epic.id)}
          disabled={completeMutation.isPending}
          title={t("epic.completeTitle")}
          className="relative shrink-0 rounded border border-outline-variant px-2 py-1 font-mono text-[11px] text-on-surface-variant transition-colors hover:text-on-surface disabled:opacity-50"
        >
          <Icon name="check_circle" className="text-[13px]" />
        </button>
      )}

      {/* chevron — decorative; the stretched link handles navigation */}
      <Icon name="chevron_right" className="shrink-0 text-[18px] text-on-surface-variant" />
    </div>
  );
}
