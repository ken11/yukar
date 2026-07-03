"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { textareaClass } from "@/components/features/settings/settings-primitives";
import { Icon } from "@/components/icon";
import { EmptyState } from "@/components/ui/empty-state";
import type { IndexStatusResponse, Repo } from "@/lib/api/endpoints";
import { addRepo, deleteRepo, getIndexStatus, listRepos, triggerIndex } from "@/lib/api/endpoints";
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

// ---------------------------------------------------------------------------
// AddRepoForm — inline expandable "register an existing local repo" form
// ---------------------------------------------------------------------------

function AddRepoForm({ projectId }: { projectId: string }) {
  const t = useT();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [defaultBranch, setDefaultBranch] = useState("main");
  const [error, setError] = useState("");

  function reset() {
    setPath("");
    setName("");
    setDefaultBranch("main");
    setError("");
  }

  const addMutation = useMutation({
    mutationFn: () =>
      addRepo(projectId, {
        name: name.trim(),
        path: path.trim(),
        default_branch: defaultBranch.trim() || "main",
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.repos.list(projectId) });
      qc.invalidateQueries({ queryKey: queryKeys.index.status(projectId) });
      reset();
      setOpen(false);
    },
    onError: (err) => {
      setError(err instanceof Error ? err.message : t("repos.addFailed"));
    },
  });

  const canSubmit = path.trim() !== "" && !addMutation.isPending;

  if (!open) {
    return (
      <button
        type="button"
        data-testid="add-repo-btn"
        onClick={() => setOpen(true)}
        className="flex items-center gap-1.5 rounded border border-outline-variant px-3 py-1.5 text-[12px] text-on-surface-variant transition-colors hover:border-outline hover:text-on-surface focus:outline-none focus-visible:ring-2 focus-visible:ring-white"
      >
        <Icon name="add" className="text-[16px]" />
        {t("repos.addRepo")}
      </button>
    );
  }

  return (
    <div
      data-testid="add-repo-form"
      className="rounded border border-outline-variant/60 bg-surface-container-lowest p-4"
    >
      <div className="mb-3 flex items-center justify-between">
        <span className="text-[11px] uppercase tracking-wider text-outline">
          {t("repos.addRepo")}
        </span>
        <button
          type="button"
          onClick={() => {
            reset();
            setOpen(false);
          }}
          className="rounded p-1 text-outline transition-colors hover:bg-surface-variant hover:text-on-surface"
          aria-label={t("common.cancel")}
        >
          <Icon name="close" className="text-[16px]" />
        </button>
      </div>

      <div className="space-y-3">
        <div>
          <label
            htmlFor="add-repo-path"
            className="mb-1 block text-[11px] uppercase tracking-wider text-outline"
          >
            {t("repos.pathLabel")}
          </label>
          <input
            id="add-repo-path"
            data-testid="add-repo-path-input"
            type="text"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="/Users/you/git/my-repo"
            className="w-full rounded border border-outline-variant bg-surface-container px-3 py-1.5 font-mono text-code-sm text-on-surface placeholder:text-outline focus:border-[var(--color-light)] focus:outline-none focus:ring-1 focus:ring-[var(--color-light)]"
          />
          <p className="mt-1 text-[11px] text-outline">{t("repos.pathNote")}</p>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label
              htmlFor="add-repo-name"
              className="mb-1 block text-[11px] uppercase tracking-wider text-outline"
            >
              {t("repos.nameLabel")}
            </label>
            <input
              id="add-repo-name"
              data-testid="add-repo-name-input"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t("repos.namePlaceholder")}
              className="w-full rounded border border-outline-variant bg-surface-container px-3 py-1.5 font-mono text-code-sm text-on-surface placeholder:text-outline focus:border-[var(--color-light)] focus:outline-none focus:ring-1 focus:ring-[var(--color-light)]"
            />
          </div>
          <div>
            <label
              htmlFor="add-repo-branch"
              className="mb-1 block text-[11px] uppercase tracking-wider text-outline"
            >
              {t("repos.branchLabel")}
            </label>
            <input
              id="add-repo-branch"
              data-testid="add-repo-branch-input"
              type="text"
              value={defaultBranch}
              onChange={(e) => setDefaultBranch(e.target.value)}
              placeholder="main"
              className="w-full rounded border border-outline-variant bg-surface-container px-3 py-1.5 font-mono text-code-sm text-on-surface placeholder:text-outline focus:border-[var(--color-light)] focus:outline-none focus:ring-1 focus:ring-[var(--color-light)]"
            />
          </div>
        </div>

        <div className="flex items-center gap-3">
          {error && (
            <span
              data-testid="add-repo-error"
              className="text-[12px]"
              style={{ color: "var(--color-removed)" }}
            >
              {error}
            </span>
          )}
          <button
            type="button"
            data-testid="add-repo-submit"
            disabled={!canSubmit}
            onClick={() => {
              setError("");
              addMutation.mutate();
            }}
            className="ml-auto flex items-center gap-1.5 rounded px-3 py-1.5 text-[12px] font-medium transition-colors disabled:opacity-50"
            style={{
              color: "var(--color-surface)",
              backgroundColor: "var(--color-on-surface)",
            }}
          >
            <Icon name="folder_open" className="text-[14px]" />
            {addMutation.isPending ? t("repos.adding") : t("repos.addAction")}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// DeleteRepoConfirm — confirm dialog for unregistering a repo
// ---------------------------------------------------------------------------

function DeleteRepoConfirm({
  repoName,
  onConfirm,
  onCancel,
  isPending,
  error,
}: {
  repoName: string;
  onConfirm: () => void;
  onCancel: () => void;
  isPending: boolean;
  error: string;
}) {
  const t = useT();
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="w-full max-w-md rounded-lg border border-outline-variant bg-surface-container p-6 shadow-lg">
        <div className="mb-4 flex items-center gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-error/30 bg-error/10">
            <Icon name="warning" className="text-[20px] text-error" />
          </div>
          <div>
            <h3 className="text-body-md font-semibold text-on-surface">
              {t("repos.deleteConfirmTitle")}
            </h3>
            <p className="text-[12px] text-on-surface-variant">
              {t("repos.deleteConfirmMessage").replace("{repo}", repoName)}
            </p>
          </div>
        </div>
        {error && (
          <p
            data-testid="delete-repo-error"
            className="mb-4 text-[12px]"
            style={{ color: "var(--color-removed)" }}
          >
            {error}
          </p>
        )}
        <div className="flex justify-end gap-3">
          <button
            type="button"
            onClick={onCancel}
            disabled={isPending}
            className="rounded border border-outline-variant px-4 py-2 text-body-sm text-on-surface-variant transition-colors hover:bg-surface-container-high disabled:opacity-50"
          >
            {t("common.cancel")}
          </button>
          <button
            type="button"
            data-testid="delete-repo-confirm-btn"
            onClick={onConfirm}
            disabled={isPending}
            className="flex items-center gap-1.5 rounded border border-error/40 bg-error/10 px-4 py-2 text-body-sm font-medium text-error transition-colors hover:bg-error/20 disabled:opacity-50"
          >
            <Icon name="delete" className="text-[16px]" />
            {isPending ? t("repos.deleting") : t("repos.deleteAction")}
          </button>
        </div>
      </div>
    </div>
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

  const { data: repos = initialRepos } = useQuery({
    queryKey: queryKeys.repos.list(projectId),
    queryFn: () => listRepos(projectId),
    initialData: initialRepos,
    staleTime: 30_000,
  });

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

  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const deleteMutation = useMutation({
    mutationFn: (repoName: string) => deleteRepo(projectId, repoName),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.repos.list(projectId) });
      qc.invalidateQueries({ queryKey: queryKeys.index.status(projectId) });
      setConfirmDelete(null);
    },
  });

  return (
    <div className="px-10 py-8">
      {/* Datum address */}
      <div className="mb-6">
        <p className="address">
          <span className="address-active">{t("project.tabs.repos")}</span>
        </p>
      </div>

      <div className="edge-h mb-6" aria-hidden />

      {/* Register-a-repo toolbar */}
      <div className="mb-8 w-full max-w-[960px]">
        <AddRepoForm projectId={projectId} />
      </div>

      {repos.length === 0 ? (
        <EmptyState address={`${projectId} / repos`} message={t("repos.notConfigured")} />
      ) : (
        <div className="w-full max-w-[960px]">
          {/* Table header */}
          <div
            className="grid items-center border-b border-outline-variant/40 pb-2"
            style={{ gridTemplateColumns: "1fr 1fr minmax(80px,auto) minmax(80px,auto) 40px" }}
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
            <span />
          </div>

          {/* Repository rows */}
          {repos.map((repo) => {
            const draft = drafts[repo.name] ?? { allowText: "", denyText: "" };
            const isPending = pending[repo.name] ?? false;
            const isSaved = saved[repo.name] ?? false;
            const error = saveErrors[repo.name] ?? "";
            const isReindexing =
              reindexMutation.isPending ||
              (indexStatus?.statuses.find((s) => s.repo_name === repo.name)?.state ===
                "indexing") ===
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
                  style={{
                    gridTemplateColumns: "1fr 1fr minmax(80px,auto) minmax(80px,auto) 40px",
                  }}
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
                  <button
                    type="button"
                    data-testid={`delete-repo-btn-${repo.name}`}
                    onClick={() => {
                      deleteMutation.reset();
                      setConfirmDelete(repo.name);
                    }}
                    aria-label={t("repos.removeLabel").replace("{repo}", repo.name)}
                    className="justify-self-end rounded p-1 text-outline transition-colors hover:bg-surface-variant hover:text-error"
                  >
                    <Icon name="delete" className="text-[16px]" />
                  </button>
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
      )}

      <div className="pb-16" />

      {confirmDelete !== null && (
        <DeleteRepoConfirm
          repoName={confirmDelete}
          isPending={deleteMutation.isPending}
          error={
            deleteMutation.isError
              ? deleteMutation.error instanceof Error
                ? deleteMutation.error.message
                : t("repos.deleteFailed")
              : ""
          }
          onConfirm={() => deleteMutation.mutate(confirmDelete)}
          onCancel={() => {
            deleteMutation.reset();
            setConfirmDelete(null);
          }}
        />
      )}
    </div>
  );
}
