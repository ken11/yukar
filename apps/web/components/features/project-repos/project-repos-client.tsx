"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { textareaClass } from "@/components/features/settings/settings-primitives";
import { EmptyState } from "@/components/ui/empty-state";
import type { IndexStatusResponse, Repo } from "@/lib/api/endpoints";
import { getIndexStatus, triggerIndex } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { useRepoCommands } from "@/lib/hooks/use-repo-commands";
import { useT } from "@/lib/i18n/provider";

interface IndexStateBadgeProps {
  repoName: string;
  indexStatus: IndexStatusResponse | undefined;
  onReindex: (repoName: string) => void;
  isReindexing: boolean;
}

function IndexStateBadge({ repoName, indexStatus, onReindex, isReindexing }: IndexStateBadgeProps) {
  const status = indexStatus?.statuses.find((s) => s.repo_name === repoName);

  if (!status) {
    return (
      <span className="data rounded border border-outline-variant/30 px-1.5 py-0.5 text-outline">
        —
      </span>
    );
  }

  const stateLabel: Record<typeof status.state, string> = {
    indexed: "indexed",
    indexing: "indexing…",
    stale: "stale",
    unindexed: "unindexed",
    error: "error",
  };

  const stateColor: Record<typeof status.state, string> = {
    indexed: "text-on-surface-variant",
    indexing: "text-[var(--color-running)]",
    stale: "text-[var(--color-removed)]",
    unindexed: "text-outline",
    error: "text-[var(--color-removed)]",
  };

  return (
    <span className="flex items-center gap-1.5">
      <span
        className={cn(
          "data rounded border border-outline-variant/30 px-1.5 py-0.5",
          stateColor[status.state],
        )}
        title={
          status.last_error
            ? status.last_error
            : `${status.files} files, ${status.chunks} chunks${status.last_indexed_at ? ` · ${new Date(status.last_indexed_at).toLocaleString()}` : ""}`
        }
      >
        {stateLabel[status.state]}
      </span>
      {(status.state === "unindexed" || status.state === "stale" || status.state === "error") && (
        <button
          type="button"
          disabled={isReindexing}
          onClick={() => onReindex(repoName)}
          className="data rounded border border-outline-variant/40 px-1.5 py-0.5 text-outline transition-colors hover:border-outline hover:text-on-surface-variant disabled:opacity-50"
        >
          reindex
        </button>
      )}
    </span>
  );
}

interface ProjectReposClientProps {
  projectId: string;
  initialRepos: Repo[];
  initialIndexStatus: IndexStatusResponse;
}

export function ProjectReposClient({
  projectId,
  initialRepos,
  initialIndexStatus,
}: ProjectReposClientProps) {
  const t = useT();
  const qc = useQueryClient();

  const { drafts, patchDraft, saveErrors, saved, pending, handleSave } = useRepoCommands(
    projectId,
    initialRepos,
  );

  const { data: indexStatus } = useQuery({
    queryKey: queryKeys.index.status(projectId),
    queryFn: () => getIndexStatus(projectId),
    initialData: initialIndexStatus,
    staleTime: 30_000,
    refetchInterval: (query) => {
      const statuses = query.state.data?.statuses ?? [];
      return statuses.some((s) => s.state === "indexing") ? 3_000 : false;
    },
  });

  const reindexMutation = useMutation({
    mutationFn: (repoName: string) => triggerIndex(projectId, repoName),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.index.status(projectId) });
    },
  });

  if (initialRepos.length === 0) {
    return (
      <div className="px-10 py-8">
        <EmptyState address={`${projectId} / repos`} message={t("repos.notConfigured")} />
      </div>
    );
  }

  return (
    <div className="px-10 py-8">
      {/* Datum address */}
      <div className="mb-6">
        <p className="address">
          <span className="address-active">{t("project.tabs.repos")}</span>
        </p>
      </div>

      <div className="edge-h mb-8" aria-hidden />

      {/* Repository table */}
      <div className="w-full max-w-[960px]">
        {/* Table header */}
        <div
          className="grid items-center border-b border-outline-variant/40 pb-2"
          style={{ gridTemplateColumns: "1fr 1fr minmax(80px,auto) minmax(80px,auto)" }}
        >
          <span className="text-[11px] font-medium uppercase tracking-[0.05em] text-outline">
            Name
          </span>
          <span className="text-[11px] font-medium uppercase tracking-[0.05em] text-outline">
            Path
          </span>
          <span className="text-[11px] font-medium uppercase tracking-[0.05em] text-outline">
            Branch
          </span>
          <span className="text-[11px] font-medium uppercase tracking-[0.05em] text-outline">
            Index
          </span>
        </div>

        {/* Repository rows */}
        {initialRepos.map((repo) => {
          const draft = drafts[repo.name] ?? { allowText: "", denyText: "" };
          const isPending = pending[repo.name] ?? false;
          const isSaved = saved[repo.name] ?? false;
          const error = saveErrors[repo.name] ?? "";
          const isReindexing =
            reindexMutation.isPending ||
            (indexStatus?.statuses.find((s) => s.repo_name === repo.name)?.state === "indexing") ===
              true;

          return (
            <div
              key={repo.name}
              data-testid={`repo-row-${repo.name}`}
              className="border-b border-outline-variant/20 py-5"
            >
              {/* Summary row */}
              <div
                className="grid items-center"
                style={{ gridTemplateColumns: "1fr 1fr minmax(80px,auto) minmax(80px,auto)" }}
              >
                <span className="data text-on-surface">{repo.name}</span>
                <span className="data truncate text-outline" title={repo.path}>
                  {repo.path}
                </span>
                <span className="data text-on-surface-variant">{repo.default_branch}</span>
                <IndexStateBadge
                  repoName={repo.name}
                  indexStatus={indexStatus}
                  onReindex={(name) => reindexMutation.mutate(name)}
                  isReindexing={isReindexing}
                />
              </div>

              {/* Command allow/deny */}
              <div className="mt-4 grid grid-cols-2 gap-4">
                <div>
                  <label
                    htmlFor={`repo-allow-${repo.name}`}
                    className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                  >
                    {t("repos.allowLabel")}{" "}
                    <span className="normal-case tracking-normal text-outline/60">
                      {t("repos.onePerLine")}
                    </span>
                  </label>
                  <textarea
                    id={`repo-allow-${repo.name}`}
                    data-testid={`repo-allow-textarea-${repo.name}`}
                    rows={4}
                    value={draft.allowText}
                    onChange={(e) => patchDraft(repo.name, { allowText: e.target.value })}
                    placeholder={"pnpm test\npnpm lint\npytest"}
                    className={textareaClass}
                  />
                  <p className="mt-1 text-[11px] text-outline">{t("repos.allowEmptyNote")}</p>
                </div>
                <div>
                  <label
                    htmlFor={`repo-deny-${repo.name}`}
                    className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                  >
                    {t("repos.denyLabel")}{" "}
                    <span className="normal-case tracking-normal text-outline/60">
                      {t("repos.onePerLine")}
                    </span>
                  </label>
                  <textarea
                    id={`repo-deny-${repo.name}`}
                    data-testid={`repo-deny-textarea-${repo.name}`}
                    rows={4}
                    value={draft.denyText}
                    onChange={(e) => patchDraft(repo.name, { denyText: e.target.value })}
                    placeholder={"rm -rf"}
                    className={textareaClass}
                  />
                  <p className="mt-1 text-[11px] text-outline">{t("repos.denyPriorityNote")}</p>
                </div>
              </div>

              {/* Save */}
              <div className="mt-3 flex items-center gap-3">
                {error && (
                  <span
                    data-testid={`repo-save-error-${repo.name}`}
                    className="text-[12px]"
                    style={{ color: "var(--color-removed)" }}
                  >
                    {error}
                  </span>
                )}
                <button
                  type="button"
                  data-testid={`save-repo-commands-btn-${repo.name}`}
                  disabled={isPending}
                  onClick={() => handleSave(repo.name)}
                  aria-label={t("repos.saveCommandsLabel").replace("{repo}", repo.name)}
                  className="flex items-center gap-1.5 rounded px-3 py-1.5 text-[12px] font-medium transition-colors disabled:opacity-50"
                  style={{
                    color: "var(--color-surface)",
                    backgroundColor: "var(--color-on-surface)",
                  }}
                >
                  {isPending ? "Saving…" : isSaved ? "Saved" : "Save"}
                </button>
              </div>
            </div>
          );
        })}
      </div>

      <div className="pb-16" />
    </div>
  );
}
