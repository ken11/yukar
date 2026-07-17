"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { textareaClass } from "@/components/features/settings/settings-primitives";
import { Icon } from "@/components/icon";
import { EmptyState } from "@/components/ui/empty-state";
import type { BlockedOriginItem, IndexStatusResponse, Repo } from "@/lib/api/endpoints";
import {
  addRepo,
  cancelBrowserLogin,
  deleteBrowserAuth,
  deleteRepo,
  extractDetail,
  finishBrowserLogin,
  getBrowserAuth,
  getIndexStatus,
  listBlockedOrigins,
  listRepos,
  startBrowserLogin,
  triggerIndex,
} from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import type { DevServerDraft, ServiceDraft } from "@/lib/dev-server/draft";
import { emptyDevServerDraft, emptyServiceDraft, SERVICE_NAME_RE } from "@/lib/dev-server/draft";
import { useRepoCommands } from "@/lib/hooks/use-repo-commands";
import { useRepoDevServer } from "@/lib/hooks/use-repo-dev-server";
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

// ---------------------------------------------------------------------------
// DevServerSection — per-repo dev server launch config editor
// ---------------------------------------------------------------------------

// Same input styling as AddRepoForm's fields.
const repoInputClass =
  "w-full rounded border border-outline-variant bg-surface-container px-3 py-1.5 font-mono text-code-sm text-on-surface placeholder:text-outline focus:border-[var(--color-light)] focus:outline-none focus:ring-1 focus:ring-[var(--color-light)]";

// ---------------------------------------------------------------------------
// BrowserAuthSection — user-interactive login capture for agent verification
// ---------------------------------------------------------------------------

const authButtonClass =
  "rounded border border-outline-variant/40 px-2 py-1 text-[11px] text-outline transition-colors hover:border-outline hover:text-on-surface disabled:opacity-50";

function BrowserAuthSection({ projectId, repoName }: { projectId: string; repoName: string }) {
  const t = useT();
  const qc = useQueryClient();

  const { data: status } = useQuery({
    queryKey: queryKeys.repos.browserAuth(projectId, repoName),
    queryFn: () => getBrowserAuth(projectId, repoName),
    // While the headed window is open, poll so "finish elsewhere / window
    // closed" is reflected without a manual reload.
    refetchInterval: (query) => (query.state.data?.login_active ? 3_000 : false),
  });

  const [actionError, setActionError] = useState("");
  const refresh = () => {
    qc.invalidateQueries({ queryKey: queryKeys.repos.browserAuth(projectId, repoName) });
  };
  const mutationOpts = {
    onSuccess: () => setActionError(""),
    // Surface the backend's `detail` (e.g. "A login capture is already in
    // progress.", "Dev server failed to start: …") rather than the generic
    // ApiError.message — login capture is a fix-the-reason-by-hand flow.
    onError: (err: unknown) =>
      setActionError(extractDetail(err) ?? (err instanceof Error ? err.message : String(err))),
    onSettled: refresh,
  };
  const start = useMutation({
    mutationFn: () => startBrowserLogin(projectId, repoName),
    ...mutationOpts,
  });
  const finish = useMutation({
    mutationFn: () => finishBrowserLogin(projectId, repoName),
    ...mutationOpts,
  });
  const cancel = useMutation({
    mutationFn: () => cancelBrowserLogin(projectId, repoName),
    ...mutationOpts,
  });
  const discard = useMutation({
    mutationFn: () => deleteBrowserAuth(projectId, repoName),
    ...mutationOpts,
  });
  const busy = start.isPending || finish.isPending || cancel.isPending || discard.isPending;

  return (
    <div data-testid={`browser-auth-${repoName}`}>
      <span className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline">
        {t("repos.devServer.browserAuthTitle")}
      </span>
      <div className="flex flex-wrap items-center gap-2">
        {status?.login_active ? (
          <>
            <span className="text-[12px] text-on-surface-variant">
              {t("repos.devServer.loginActiveNote")}
            </span>
            <button
              type="button"
              data-testid={`browser-login-finish-btn-${repoName}`}
              disabled={busy}
              onClick={() => finish.mutate()}
              className={authButtonClass}
            >
              {t("repos.devServer.loginFinish")}
            </button>
            <button
              type="button"
              data-testid={`browser-login-cancel-btn-${repoName}`}
              disabled={busy}
              onClick={() => cancel.mutate()}
              className={authButtonClass}
            >
              {t("repos.devServer.loginCancel")}
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              data-testid={`browser-login-start-btn-${repoName}`}
              disabled={busy}
              onClick={() => start.mutate()}
              className={authButtonClass}
            >
              {t("repos.devServer.loginStart")}
            </button>
            {status?.exists && (
              <>
                <span
                  data-testid={`browser-auth-captured-${repoName}`}
                  className="text-[12px] text-on-surface-variant"
                >
                  {t("repos.devServer.capturedAt").replace(
                    "{date}",
                    status.captured_at ? new Date(status.captured_at).toLocaleString() : "—",
                  )}
                </span>
                <button
                  type="button"
                  data-testid={`browser-auth-discard-btn-${repoName}`}
                  disabled={busy}
                  onClick={() => discard.mutate()}
                  className={authButtonClass}
                >
                  {t("repos.devServer.discardAuth")}
                </button>
              </>
            )}
          </>
        )}
      </div>
      {actionError && (
        <p className="mt-1 text-[11px]" style={{ color: "var(--color-removed)" }}>
          {actionError}
        </p>
      )}
      <p className="mt-1 text-[11px] text-outline">{t("repos.devServer.browserAuthNote")}</p>
    </div>
  );
}

interface DevServerSectionProps {
  projectId: string;
  repo: Repo;
  draft: DevServerDraft;
  blocked: BlockedOriginItem[];
  isPending: boolean;
  isSaved: boolean;
  error: string;
  onPatch: (patch: Partial<DevServerDraft>) => void;
  onPatchService: (idx: number, patch: Partial<ServiceDraft>) => void;
  onSave: () => void;
  onRemove: () => void;
}

function DevServerSection({
  projectId,
  repo,
  draft,
  blocked,
  isPending,
  isSaved,
  error,
  onPatch,
  onPatchService,
  onSave,
  onRemove,
}: DevServerSectionProps) {
  const t = useT();

  const allowedOriginLines = draft.allowedOriginsText
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

  function addBlockedOriginToAllowed(origin: string) {
    if (allowedOriginLines.includes(origin)) return;
    onPatch({ allowedOriginsText: [...allowedOriginLines, origin].join("\n") });
  }

  // List order IS the start (dependency) order — services launch top to
  // bottom, each awaiting readiness before the next.
  function moveService(idx: number, dir: -1 | 1) {
    const j = idx + dir;
    if (j < 0 || j >= draft.services.length) return;
    const next = [...draft.services];
    [next[idx], next[j]] = [next[j], next[idx]];
    onPatch({ services: next });
  }

  if (!draft.enabled) {
    return (
      <div className="mt-5 border-t border-outline-variant/20 pt-4">
        <button
          type="button"
          data-testid={`configure-dev-server-btn-${repo.name}`}
          onClick={() =>
            onPatch({
              enabled: true,
              services: draft.services.length > 0 ? draft.services : [emptyServiceDraft()],
            })
          }
          className="flex items-center gap-2 rounded border border-dashed border-outline-variant/50 px-3 py-2 text-[12px] text-outline transition-colors hover:border-outline hover:text-on-surface"
        >
          <Icon name="add" className="text-[13px]" />
          {t("repos.devServer.configure")}
        </button>
      </div>
    );
  }

  return (
    <div className="mt-5 border-t border-outline-variant/20 pt-4">
      {/* Heading row: label + remove */}
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[11px] uppercase tracking-wider text-outline">
          {t("repos.devServer.sectionLabel")}
        </span>
        <button
          type="button"
          data-testid={`remove-dev-server-btn-${repo.name}`}
          disabled={isPending}
          onClick={() => {
            // A saved config needs the DELETE endpoint; an unsaved draft just collapses.
            if (repo.dev_server) onRemove();
            else onPatch({ enabled: false });
          }}
          className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-outline transition-colors hover:bg-surface-variant hover:text-error disabled:opacity-50"
        >
          <Icon name="delete" className="text-[13px]" />
          {t("repos.devServer.remove")}
        </button>
      </div>

      <div className="space-y-3">
        <p className="text-[11px] text-outline">{t("repos.devServer.startOrderNote")}</p>

        {/* Services */}
        {draft.services.map((svc, idx) => (
          <div
            key={`service-${
              // biome-ignore lint/suspicious/noArrayIndexKey: stable positional index for dev services
              idx
            }`}
            className="rounded border border-outline-variant/40 p-3"
          >
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <div>
                <label
                  htmlFor={`dev-server-service-name-${repo.name}-${idx}`}
                  className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                >
                  {t("repos.devServer.serviceName")}
                </label>
                <div className="flex items-center gap-2">
                  <input
                    id={`dev-server-service-name-${repo.name}-${idx}`}
                    data-testid={`dev-server-service-name-${repo.name}-${idx}`}
                    type="text"
                    value={svc.name}
                    onChange={(e) => onPatchService(idx, { name: e.target.value })}
                    placeholder="web"
                    className={repoInputClass}
                  />
                  <button
                    type="button"
                    disabled={idx === 0}
                    onClick={() => moveService(idx, -1)}
                    aria-label={t("repos.devServer.moveServiceUp")}
                    className="rounded p-1 text-outline transition-colors hover:bg-surface-variant hover:text-on-surface disabled:opacity-30 md:hidden"
                  >
                    <Icon name="arrow_upward" className="text-[16px]" />
                  </button>
                  <button
                    type="button"
                    disabled={idx === draft.services.length - 1}
                    onClick={() => moveService(idx, 1)}
                    aria-label={t("repos.devServer.moveServiceDown")}
                    className="rounded p-1 text-outline transition-colors hover:bg-surface-variant hover:text-on-surface disabled:opacity-30 md:hidden"
                  >
                    <Icon name="arrow_downward" className="text-[16px]" />
                  </button>
                  <button
                    type="button"
                    data-testid={`dev-server-remove-service-btn-${repo.name}-${idx}`}
                    onClick={() =>
                      onPatch({ services: draft.services.filter((_, i) => i !== idx) })
                    }
                    aria-label={t("repos.devServer.removeService")}
                    className="rounded p-1 text-outline transition-colors hover:bg-surface-variant hover:text-error md:hidden"
                  >
                    <Icon name="delete" className="text-[16px]" />
                  </button>
                </div>
              </div>
              <div className="hidden items-start justify-end gap-1 md:flex">
                <button
                  type="button"
                  data-testid={`dev-server-move-service-up-btn-${repo.name}-${idx}`}
                  disabled={idx === 0}
                  onClick={() => moveService(idx, -1)}
                  aria-label={t("repos.devServer.moveServiceUp")}
                  className="rounded p-1 text-outline transition-colors hover:bg-surface-variant hover:text-on-surface disabled:opacity-30"
                >
                  <Icon name="arrow_upward" className="text-[14px]" />
                </button>
                <button
                  type="button"
                  data-testid={`dev-server-move-service-down-btn-${repo.name}-${idx}`}
                  disabled={idx === draft.services.length - 1}
                  onClick={() => moveService(idx, 1)}
                  aria-label={t("repos.devServer.moveServiceDown")}
                  className="rounded p-1 text-outline transition-colors hover:bg-surface-variant hover:text-on-surface disabled:opacity-30"
                >
                  <Icon name="arrow_downward" className="text-[14px]" />
                </button>
                <button
                  type="button"
                  onClick={() => onPatch({ services: draft.services.filter((_, i) => i !== idx) })}
                  className="flex h-fit items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-outline transition-colors hover:bg-surface-variant hover:text-error"
                >
                  <Icon name="delete" className="text-[13px]" />
                  {t("repos.devServer.removeService")}
                </button>
              </div>
            </div>

            <div className="mt-3">
              <label
                htmlFor={`dev-server-service-command-${repo.name}-${idx}`}
                className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
              >
                {t("repos.devServer.serviceCommand")}
              </label>
              <input
                id={`dev-server-service-command-${repo.name}-${idx}`}
                data-testid={`dev-server-service-command-${repo.name}-${idx}`}
                type="text"
                value={svc.commandLine}
                onChange={(e) => onPatchService(idx, { commandLine: e.target.value })}
                placeholder="pnpm dev --port {port}"
                className={repoInputClass}
              />
              <p className="mt-1 text-[11px] text-outline">
                {t("repos.devServer.portNote").replace(
                  /\{example\}/g,
                  // Live example: prefer a sibling service's actual name over a
                  // canned one. Function replacement — never interpret "$&" etc.
                  () =>
                    draft.services
                      .find((other, i) => i !== idx && SERVICE_NAME_RE.test(other.name.trim()))
                      ?.name.trim() ?? "api",
                )}
              </p>
            </div>

            <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
              <div>
                <label
                  htmlFor={`dev-server-service-cwd-${repo.name}-${idx}`}
                  className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                >
                  {t("repos.devServer.serviceCwd")}
                </label>
                <input
                  id={`dev-server-service-cwd-${repo.name}-${idx}`}
                  data-testid={`dev-server-service-cwd-${repo.name}-${idx}`}
                  type="text"
                  value={svc.cwd}
                  onChange={(e) => onPatchService(idx, { cwd: e.target.value })}
                  placeholder="."
                  className={repoInputClass}
                />
              </div>
              <div>
                <label
                  htmlFor={`dev-server-service-port-${repo.name}-${idx}`}
                  className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                >
                  {t("repos.devServer.servicePort")}
                </label>
                <input
                  id={`dev-server-service-port-${repo.name}-${idx}`}
                  data-testid={`dev-server-service-port-${repo.name}-${idx}`}
                  type="text"
                  inputMode="numeric"
                  value={svc.basePort}
                  onChange={(e) => onPatchService(idx, { basePort: e.target.value })}
                  placeholder="3000"
                  className={repoInputClass}
                />
              </div>
            </div>

            <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
              <div>
                <label
                  htmlFor={`dev-server-service-readiness-path-${repo.name}-${idx}`}
                  className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                >
                  {t("repos.devServer.serviceReadinessPath")}
                </label>
                <input
                  id={`dev-server-service-readiness-path-${repo.name}-${idx}`}
                  data-testid={`dev-server-service-readiness-path-${repo.name}-${idx}`}
                  type="text"
                  value={svc.readinessPath}
                  onChange={(e) => onPatchService(idx, { readinessPath: e.target.value })}
                  placeholder="/health"
                  className={repoInputClass}
                />
                <p className="mt-1 text-[11px] text-outline">
                  {t("repos.devServer.readinessEmptyNote")}
                </p>
              </div>
              <div>
                <label
                  htmlFor={`dev-server-service-readiness-timeout-${repo.name}-${idx}`}
                  className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                >
                  {t("repos.devServer.serviceReadinessTimeout")}
                </label>
                <input
                  id={`dev-server-service-readiness-timeout-${repo.name}-${idx}`}
                  data-testid={`dev-server-service-readiness-timeout-${repo.name}-${idx}`}
                  type="text"
                  inputMode="numeric"
                  value={svc.readinessTimeout}
                  onChange={(e) => onPatchService(idx, { readinessTimeout: e.target.value })}
                  placeholder="60"
                  className={repoInputClass}
                />
              </div>
            </div>

            {/* Env sources, listed in merge order: files → pass-through → literals. */}
            <p className="mt-3 text-[11px] text-outline">{t("repos.devServer.envMergeNote")}</p>

            <div className="mt-2 grid grid-cols-1 gap-3 md:grid-cols-2">
              <div>
                <label
                  htmlFor={`dev-server-service-env-file-${repo.name}-${idx}`}
                  className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                >
                  {t("repos.devServer.serviceEnvFile")}{" "}
                  <span className="normal-case tracking-normal text-outline/60">
                    {t("repos.onePerLine")}
                  </span>
                </label>
                <textarea
                  id={`dev-server-service-env-file-${repo.name}-${idx}`}
                  data-testid={`dev-server-service-env-file-${repo.name}-${idx}`}
                  rows={2}
                  value={svc.envFileText}
                  onChange={(e) => onPatchService(idx, { envFileText: e.target.value })}
                  placeholder={"~/secrets/myapp/dev.env\n.env.development"}
                  className={textareaClass}
                />
                <p className="mt-1 text-[11px] text-outline">{t("repos.devServer.envFileNote")}</p>
              </div>
              <div>
                <label
                  htmlFor={`dev-server-service-env-passthrough-${repo.name}-${idx}`}
                  className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                >
                  {t("repos.devServer.serviceEnvPassthrough")}{" "}
                  <span className="normal-case tracking-normal text-outline/60">
                    {t("repos.onePerLine")}
                  </span>
                </label>
                <textarea
                  id={`dev-server-service-env-passthrough-${repo.name}-${idx}`}
                  data-testid={`dev-server-service-env-passthrough-${repo.name}-${idx}`}
                  rows={2}
                  value={svc.envPassthroughText}
                  onChange={(e) => onPatchService(idx, { envPassthroughText: e.target.value })}
                  placeholder={"DATABASE_URL\nSTRIPE_TEST_KEY"}
                  className={textareaClass}
                />
                <p className="mt-1 text-[11px] text-outline">
                  {t("repos.devServer.envPassthroughNote")}
                </p>
              </div>
            </div>

            <div className="mt-3">
              <label
                htmlFor={`dev-server-service-env-${repo.name}-${idx}`}
                className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
              >
                {t("repos.devServer.serviceEnv")}
              </label>
              <textarea
                id={`dev-server-service-env-${repo.name}-${idx}`}
                data-testid={`dev-server-service-env-${repo.name}-${idx}`}
                rows={3}
                value={svc.envText}
                onChange={(e) => onPatchService(idx, { envText: e.target.value })}
                placeholder={"NODE_ENV=development\nAPI_URL=http://127.0.0.1:{port:api}"}
                className={textareaClass}
              />
              <p className="mt-1 text-[11px] text-outline">{t("repos.devServer.envFormatNote")}</p>
            </div>
          </div>
        ))}

        <button
          type="button"
          data-testid={`dev-server-add-service-btn-${repo.name}`}
          onClick={() => onPatch({ services: [...draft.services, emptyServiceDraft()] })}
          className="flex items-center gap-2 rounded border border-dashed border-outline-variant/50 px-3 py-2 text-[12px] text-outline transition-colors hover:border-outline hover:text-on-surface"
        >
          <Icon name="add" className="text-[13px]" />
          {t("repos.devServer.addService")}
        </button>

        {/* Browser settings */}
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <div>
            <label
              htmlFor={`dev-server-origins-${repo.name}`}
              className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
            >
              {t("repos.devServer.allowedOrigins")}{" "}
              <span className="normal-case tracking-normal text-outline/60">
                {t("repos.onePerLine")}
              </span>
            </label>
            <textarea
              id={`dev-server-origins-${repo.name}`}
              data-testid={`dev-server-origins-${repo.name}`}
              rows={3}
              value={draft.allowedOriginsText}
              onChange={(e) => onPatch({ allowedOriginsText: e.target.value })}
              placeholder="https://fonts.googleapis.com"
              className={textareaClass}
            />
            <p className="mt-1 text-[11px] text-outline">
              {t("repos.devServer.allowedOriginsNote")}
            </p>
          </div>
          <div>
            <label
              htmlFor={`dev-server-cdn-${repo.name}`}
              className="flex cursor-pointer items-center gap-2 text-[13px] text-on-surface-variant"
            >
              <input
                id={`dev-server-cdn-${repo.name}`}
                data-testid={`dev-server-cdn-${repo.name}`}
                type="checkbox"
                checked={draft.allowCommonCdns}
                onChange={(e) => onPatch({ allowCommonCdns: e.target.checked })}
                className="h-4 w-4 accent-[var(--color-light)]"
              />
              {t("repos.devServer.allowCommonCdns")}
            </label>
            <p className="mt-1 text-[11px] text-outline">
              {t("repos.devServer.allowCommonCdnsNote")}
            </p>
          </div>
        </div>

        {/* Interactive login capture (§12) — needs a SAVED config to launch */}
        {repo.dev_server && <BrowserAuthSection projectId={projectId} repoName={repo.name} />}

        {/* Blocked destinations — what the egress gate rejected (§13) */}
        {blocked.length > 0 && (
          <div data-testid={`dev-server-blocked-${repo.name}`}>
            <span className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline">
              {t("repos.devServer.blockedTitle")}
            </span>
            <ul className="space-y-1">
              {blocked.map((item) => {
                const alreadyAllowed = allowedOriginLines.includes(item.origin);
                return (
                  <li
                    key={item.origin}
                    className="flex items-center justify-between gap-3 rounded border border-outline-variant/40 px-2 py-1"
                  >
                    <span className="data min-w-0 truncate text-on-surface-variant">
                      {item.origin}
                      <span className="ml-2 text-outline">
                        ×{item.count} ({item.resource_types.join(", ")})
                      </span>
                    </span>
                    <button
                      type="button"
                      data-testid={`add-blocked-origin-btn-${repo.name}-${item.origin}`}
                      disabled={alreadyAllowed}
                      onClick={() => addBlockedOriginToAllowed(item.origin)}
                      className="shrink-0 rounded border border-outline-variant/40 px-1.5 py-0.5 text-[11px] text-outline transition-colors hover:border-outline hover:text-on-surface disabled:opacity-50"
                    >
                      {alreadyAllowed
                        ? t("repos.devServer.addedToAllowed")
                        : t("repos.devServer.addToAllowed")}
                    </button>
                  </li>
                );
              })}
            </ul>
            <p className="mt-1 text-[11px] text-outline">{t("repos.devServer.blockedNote")}</p>
          </div>
        )}

        {/* Save */}
        <div className="flex items-center gap-3">
          {error && (
            <span
              data-testid={`dev-server-save-error-${repo.name}`}
              className="text-[12px]"
              style={{ color: "var(--color-removed)" }}
            >
              {error}
            </span>
          )}
          <button
            type="button"
            data-testid={`save-dev-server-btn-${repo.name}`}
            disabled={isPending}
            onClick={onSave}
            className="flex items-center gap-1.5 rounded px-3 py-1.5 text-[12px] font-medium transition-colors disabled:opacity-50"
            style={{
              color: "var(--color-surface)",
              backgroundColor: "var(--color-on-surface)",
            }}
          >
            {isPending
              ? t("repos.devServer.saving")
              : isSaved
                ? t("repos.devServer.saved")
                : t("repos.devServer.save")}
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

  const devServer = useRepoDevServer(projectId, initialRepos);

  // What the browser egress gate rejected (in-process aggregate on the API
  // host).  Polled while the page is open so the "run once → allow what the
  // app needs" loop works without a manual refresh.
  const { data: blockedOrigins = [] } = useQuery({
    queryKey: queryKeys.repos.blockedOrigins(projectId),
    queryFn: () => listBlockedOrigins(projectId),
    refetchInterval: 10_000,
  });

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
    <div className="px-4 py-6 md:px-10 md:py-8">
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
          {/* Table header — hidden on mobile (rows stack there, so columns have no meaning) */}
          <div
            className="hidden items-center border-b border-outline-variant/40 pb-2 md:grid"
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
                {/* Summary row — stacked on mobile (name+delete / path / branch+index), grid on desktop.
                    The inline gridTemplateColumns only applies once md:grid kicks in. */}
                <div
                  className="flex flex-wrap items-center gap-x-3 gap-y-1.5 md:grid md:gap-0"
                  style={{
                    gridTemplateColumns: "1fr 1fr minmax(80px,auto) minmax(80px,auto) 40px",
                  }}
                >
                  <span className="data order-1 text-on-surface md:order-none">{repo.name}</span>
                  <span
                    className="data order-3 w-full truncate text-outline md:order-none md:w-auto"
                    title={repo.path}
                  >
                    {repo.path}
                  </span>
                  <span className="data order-4 text-on-surface-variant md:order-none">
                    {repo.default_branch}
                  </span>
                  <span className="order-5 md:order-none">
                    <IndexStateBadge
                      repoName={repo.name}
                      indexStatus={indexStatus}
                      onReindex={(name) => reindexMutation.mutate(name)}
                      isReindexing={isReindexing}
                    />
                  </span>
                  <button
                    type="button"
                    data-testid={`delete-repo-btn-${repo.name}`}
                    onClick={() => {
                      deleteMutation.reset();
                      setConfirmDelete(repo.name);
                    }}
                    aria-label={t("repos.removeLabel").replace("{repo}", repo.name)}
                    className="order-2 ml-auto rounded p-1 text-outline transition-colors hover:bg-surface-variant hover:text-error md:order-none md:ml-0 md:justify-self-end"
                  >
                    <Icon name="delete" className="text-[16px]" />
                  </button>
                </div>

                {/* Command allow/deny — stacked on mobile */}
                <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2">
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

                {/* Save (command allow/deny) — sits with its own form, above the dev-server block */}
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
                    {isPending
                      ? t("repos.savingCommands")
                      : isSaved
                        ? t("repos.savedCommands")
                        : t("repos.saveCommands")}
                  </button>
                </div>

                {/* Dev server launch config */}
                <DevServerSection
                  projectId={projectId}
                  repo={repo}
                  draft={devServer.drafts[repo.name] ?? emptyDevServerDraft()}
                  blocked={blockedOrigins.filter((item) => item.repo === repo.name)}
                  isPending={devServer.pending[repo.name] ?? false}
                  isSaved={devServer.saved[repo.name] ?? false}
                  error={devServer.saveErrors[repo.name] ?? ""}
                  onPatch={(patch) => devServer.patchDraft(repo.name, patch)}
                  onPatchService={(idx, patch) => devServer.patchService(repo.name, idx, patch)}
                  onSave={() => devServer.handleSave(repo.name, repos)}
                  onRemove={() => devServer.handleRemove(repo.name)}
                />
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
