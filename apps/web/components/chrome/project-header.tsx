"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";
import { NotificationsPopover } from "@/components/features/notifications/notifications-popover";
import { Icon } from "@/components/icon";
import { AddressLine } from "@/components/ui/address-line";
import { getIndexStatus, triggerIndex } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { useT } from "@/lib/i18n/provider";
import type { Notification } from "@/lib/sse/use-project-notifications";
import { useProjectNotifications } from "@/lib/sse/use-project-notifications";

// ---------------------------------------------------------------------------
// SyncButton (moved from topbar.tsx)
// ---------------------------------------------------------------------------

function SyncButton({ projectId }: { projectId: string }) {
  const t = useT();
  const qc = useQueryClient();
  const [expandedError, setExpandedError] = useState<string | null>(null);

  const { data: statusData } = useQuery({
    queryKey: queryKeys.index.status(projectId),
    queryFn: () => getIndexStatus(projectId),
    staleTime: 60_000,
    refetchInterval: (query) => {
      const statuses = query.state.data?.statuses ?? [];
      return statuses.some((s) => s.state === "indexing") ? 3_000 : false;
    },
  });

  const isIndexing = statusData?.statuses.some((s) => s.state === "indexing") ?? false;

  const triggerMutation = useMutation({
    mutationFn: (repo?: string) => triggerIndex(projectId, repo),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.index.status(projectId) });
    },
  });

  const statuses = statusData?.statuses ?? [];

  let tooltipLines: string[] = [];
  if (statuses.length === 0) {
    tooltipLines = [t("indexer.noReposIndexed")];
  } else {
    tooltipLines = statuses.map((s) => {
      const ts = s.last_indexed_at ? ` · ${new Date(s.last_indexed_at).toLocaleString()}` : "";
      const fallback =
        (s.fallback_files ?? 0) > 0 ? ` (line-split fallback: ${s.fallback_files})` : "";
      return `${s.repo_name}: ${s.files} files, ${s.chunks} chunks${fallback} [${s.state}]${ts}`;
    });
  }
  const tooltip = tooltipLines.join("\n");

  const spinning = isIndexing || triggerMutation.isPending;

  const rebuildableStatuses = statuses.filter((s) =>
    (["error", "unindexed", "stale"] as const).includes(s.state as "error" | "unindexed" | "stale"),
  );
  const hasError = statuses.some((s) => s.state === "error");

  const rebuildFailedStatuses = statuses.filter((s) => s.state !== "error" && s.last_error != null);

  return (
    <div className="flex items-center gap-1">
      <button
        type="button"
        title={tooltip || t("indexer.reindex")}
        disabled={spinning}
        onClick={() => {
          if (!spinning) {
            triggerMutation.mutate(undefined);
          }
        }}
        className={cn(
          "flex items-center gap-1 rounded border border-outline-variant px-2 py-1 text-[12px] text-on-surface-variant transition-colors",
          "hover:border-outline-variant hover:text-on-surface",
          "disabled:cursor-not-allowed disabled:opacity-50",
        )}
      >
        <Icon
          name="sync"
          className={cn(
            "text-[14px]",
            spinning && "animate-spin",
            hasError && !spinning && "text-error",
          )}
        />
        <span className="data">{spinning ? t("indexer.reindexing") : t("common.reindex")}</span>
      </button>

      {rebuildFailedStatuses.map((s) => (
        <div key={`warn-${s.repo_name}`} className="relative flex items-center">
          <button
            type="button"
            title={t("indexer.rebuildFailedTitle")
              .replace("{repo}", s.repo_name)
              .replace("{error}", s.last_error ?? "")}
            onClick={() =>
              setExpandedError(
                expandedError === `warn-${s.repo_name}` ? null : `warn-${s.repo_name}`,
              )
            }
            className="flex items-center gap-0.5 rounded border border-error/30 bg-error/10 px-1.5 py-0.5 text-[10px] text-error transition-colors hover:bg-error/20"
          >
            <Icon name="warning" className="text-[12px]" />
            <span className="font-mono">{s.repo_name}</span>
          </button>
          {expandedError === `warn-${s.repo_name}` && (
            <div className="absolute right-0 top-full z-50 mt-1 w-72 rounded border border-error/30 bg-surface-container p-2 shadow-lg">
              <p className="mb-1 text-[10px] font-semibold text-error">
                {t("indexer.rebuildFailedHeading").replace("{repo}", s.repo_name)}
              </p>
              <p className="font-mono text-[10px] text-on-surface-variant break-all">
                {s.last_error}
              </p>
              {s.last_error_at && (
                <p className="mt-1 text-[9px] text-outline">
                  {new Date(s.last_error_at).toLocaleString()}
                </p>
              )}
              <button
                type="button"
                disabled={spinning}
                onClick={() => {
                  triggerMutation.mutate(s.repo_name);
                  setExpandedError(null);
                }}
                className="mt-2 flex w-full items-center justify-center gap-1 rounded border border-error/30 bg-error/10 px-2 py-1 text-[10px] text-error transition-colors hover:bg-error/20 disabled:opacity-50"
              >
                <Icon name="refresh" className="text-[12px]" />
                {t("indexer.rebuildBtn")}
              </button>
            </div>
          )}
        </div>
      ))}

      {rebuildableStatuses.map((s) => (
        <div key={s.repo_name} className="relative flex items-center">
          {s.state === "error" && (
            <button
              type="button"
              title={t("indexer.indexErrorTitle")
                .replace("{repo}", s.repo_name)
                .replace("{error}", s.last_error ?? t("indexer.indexErrorFallback"))}
              onClick={() => setExpandedError(expandedError === s.repo_name ? null : s.repo_name)}
              className="flex items-center gap-0.5 rounded border border-error/30 bg-error/10 px-1.5 py-0.5 text-[10px] text-error transition-colors hover:bg-error/20"
            >
              <Icon name="error" className="text-[12px]" />
              <span className="font-mono">{s.repo_name}</span>
            </button>
          )}
          {s.state === "error" && expandedError === s.repo_name && s.last_error && (
            <div className="absolute right-0 top-full z-50 mt-1 w-72 rounded border border-error/30 bg-surface-container p-2 shadow-lg">
              <p className="mb-1 text-[10px] font-semibold text-error">
                {t("indexer.indexErrorHeading").replace("{repo}", s.repo_name)}
              </p>
              <p className="font-mono text-[10px] text-on-surface-variant break-all">
                {s.last_error}
              </p>
              {s.last_error_at && (
                <p className="mt-1 text-[9px] text-outline">
                  {new Date(s.last_error_at).toLocaleString()}
                </p>
              )}
              <button
                type="button"
                disabled={spinning}
                onClick={() => {
                  triggerMutation.mutate(s.repo_name);
                  setExpandedError(null);
                }}
                className="mt-2 flex w-full items-center justify-center gap-1 rounded border border-error/30 bg-error/10 px-2 py-1 text-[10px] text-error transition-colors hover:bg-error/20 disabled:opacity-50"
              >
                <Icon name="refresh" className="text-[12px]" />
                {t("indexer.rebuildBtn")}
              </button>
            </div>
          )}
          {(s.state === "unindexed" || s.state === "stale") && (
            <button
              type="button"
              title={t("indexer.indexStaleTitle")
                .replace("{repo}", s.repo_name)
                .replace(
                  "{state}",
                  s.state === "unindexed" ? t("indexer.indexUnindexed") : t("indexer.indexStale"),
                )}
              disabled={spinning}
              onClick={() => triggerMutation.mutate(s.repo_name)}
              className="flex items-center gap-0.5 rounded border border-outline-variant/40 px-1.5 py-0.5 text-[10px] text-outline transition-colors hover:border-outline-variant hover:text-on-surface-variant disabled:opacity-50"
            >
              <Icon name="warning" className="text-[11px]" />
              <span className="font-mono">{s.repo_name}</span>
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// NotificationsButton (moved from topbar.tsx)
// ---------------------------------------------------------------------------

function NotificationsButton({ projectId }: { projectId: string }) {
  const handleToast = (notif: Notification) => {
    if (notif.type === "run_completed") {
      toast.success(notif.message, { duration: 4000 });
    } else if (notif.type === "run_failed") {
      toast.error(notif.message, { duration: 5000 });
    }
  };

  const { notifications, unreadCount, markAllRead } = useProjectNotifications(
    projectId,
    handleToast,
  );

  return (
    <NotificationsPopover
      projectId={projectId}
      notifications={notifications}
      unreadCount={unreadCount}
      onOpen={markAllRead}
    />
  );
}

// ---------------------------------------------------------------------------
// ProjectHeader
// ---------------------------------------------------------------------------

interface ProjectHeaderProps {
  projectId: string;
  projectName: string;
}

/**
 * ProjectHeader — sticky, with a hairline on the bottom edge.
 * Left: AddressLine (yukar / projectName). Right: SyncButton + notifications + ⌘K.
 *
 * Mobile support:
 * - `flex-wrap` allows wrapping.
 * - `min-w-0` + `truncate` truncates long project names.
 * - On desktop (md:) the traditional single-row 64px height layout is preserved.
 */
export function ProjectHeader({ projectId, projectName }: ProjectHeaderProps) {
  const t = useT();

  return (
    <header
      className="sticky top-0 z-20 flex min-h-14 flex-wrap items-center justify-between gap-x-3 gap-y-1.5 edge-h px-4 py-2 md:h-16 md:flex-nowrap md:px-6 md:py-0"
      style={{ backgroundColor: "var(--color-surface-header)" }}
    >
      {/* Left: address — truncate prevents overflow on mobile */}
      <div className="min-w-0 flex-1">
        <AddressLine
          segments={[
            { label: "yukar", href: "/projects" },
            { label: projectName, active: true },
          ]}
        />
      </div>
      {/* Right: action buttons — shrink-0 prevents collapsing */}
      <div className="flex shrink-0 items-center gap-2 md:gap-3">
        <SyncButton projectId={projectId} />
        <NotificationsButton projectId={projectId} />
        {/* ⌘K delegates to the global CommandPalette. The search button only fires an event */}
        <button
          type="button"
          onClick={() =>
            window.dispatchEvent(
              new CustomEvent("yukar:open-palette", { detail: { mode: "search" } }),
            )
          }
          aria-label={`${t("indexer.codebaseSearch")} (⌘K)`}
          title={`${t("indexer.codebaseSearch")} (⌘K)`}
          className="flex h-11 w-11 items-center justify-center rounded border border-outline-variant text-on-surface-variant transition-colors hover:bg-surface-container-high hover:text-on-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-surface)] md:h-8 md:w-8"
        >
          <Icon name="search" className="text-[18px]" />
          <kbd className="sr-only">⌘K</kbd>
        </button>
      </div>
    </header>
  );
}
