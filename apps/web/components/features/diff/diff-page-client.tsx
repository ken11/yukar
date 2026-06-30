"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Icon } from "@/components/icon";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import type { DiffResult, Epic, RepoPruneResult } from "@/lib/api/endpoints";
import {
  ApiError,
  extractConflicts,
  getGitDiff,
  getGitDiffSummary,
  gitCommit,
  gitMerge,
  gitPrune,
} from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { parseUnifiedDiff } from "@/lib/diff/parse-unified";
import { useT } from "@/lib/i18n/provider";
import { DiffLineRow, Spinner } from "./diff-line-row";
import { makeResolveEventHandlers } from "./resolve-event-handlers";
import { useResolveRun } from "./use-resolve-run";

// #42: makeResolveEventHandlers moved to resolve-event-handlers.ts. Re-exported to keep the public surface unchanged.
export { makeResolveEventHandlers };

type DiffMode = "working" | "epic";

interface DiffPageClientProps {
  projectId: string;
  epicId: string;
  epic: Epic | null;
  initialDiffs: DiffResult[];
}

// ---- #49: local component for the primary action button with duplicated className ----
// ui/Button's md=px-4 py-2 text-body-md / sm=px-3 py-1 text-body-sm does not match
// the existing button's px-4 py-2 text-body-sm, so it is kept local to avoid a visual change.

function PrimaryActionButton({
  onClick,
  disabled,
  "data-testid": testId,
  children,
}: {
  onClick?: () => void;
  disabled?: boolean;
  "data-testid"?: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      data-testid={testId}
      className="flex items-center gap-2 rounded bg-primary px-4 py-2 text-body-sm font-medium text-on-primary transition-colors hover:bg-primary-container disabled:opacity-50"
    >
      {children}
    </button>
  );
}

// ---- #45: Banner primitive ----

function Banner({
  tone,
  children,
  onDismiss,
  dismissLabel,
}: {
  tone: "error" | "secondary" | "neutral" | "running";
  children: React.ReactNode;
  onDismiss?: () => void;
  dismissLabel?: string;
}) {
  const styles: Record<typeof tone, string> = {
    error: "border-b border-error/30 bg-error/10 px-6 py-3 text-body-sm text-error",
    secondary: "border-b border-secondary/30 bg-secondary/10 px-6 py-3 text-body-sm text-secondary",
    neutral:
      "border-b border-outline-variant/50 bg-surface-container px-6 py-3 text-body-sm text-on-surface-variant",
    running:
      "flex items-center gap-3 border-b border-outline-variant/50 bg-surface-container px-6 py-3",
  };
  return (
    <div className={styles[tone]}>
      {children}
      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          className="ml-3 text-outline hover:text-on-surface"
        >
          {dismissLabel ?? "Dismiss"}
        </button>
      )}
    </div>
  );
}

// ---- #45: DiffStatusBanners — component that groups multiple banners ----

function DiffStatusBanners({
  mergeError,
  conflictResolved,
  conflictFiles,
  resolveStatus,
  resolveLastEvent,
  resolveError,
  resolvePending,
  onDismissMergeError,
  onDismissConflictResolved,
  onDismissConflict,
  onStartResolve,
  onDismissResolve,
}: {
  mergeError: string | null;
  conflictResolved: boolean;
  conflictFiles: string[] | null;
  resolveStatus: "idle" | "running" | "completed" | "failed" | "unknown";
  resolveLastEvent: string;
  resolveError: string | null;
  resolvePending: boolean;
  onDismissMergeError: () => void;
  onDismissConflictResolved: () => void;
  onDismissConflict: () => void;
  onStartResolve: () => void;
  onDismissResolve: () => void;
}) {
  const t = useT();
  return (
    <>
      {/* Generic merge error (non-409) */}
      {mergeError && (
        <Banner tone="error" onDismiss={onDismissMergeError} dismissLabel={t("diff.dismiss")}>
          {mergeError}
        </Banner>
      )}

      {/* Conflict resolved banner */}
      {conflictResolved && !conflictFiles && (
        <Banner
          tone="secondary"
          onDismiss={onDismissConflictResolved}
          dismissLabel={t("diff.dismiss")}
        >
          {t("diff.conflictsResolved")}
        </Banner>
      )}

      {/* Conflict banner with file list and resolve action */}
      {conflictFiles !== null && resolveStatus !== "running" && resolveStatus !== "completed" && (
        <div className="border-b border-error/30 bg-error/10 px-6 py-4">
          <div className="mb-2 flex items-center justify-between">
            <div className="flex items-center gap-2 text-body-sm font-medium text-error">
              <Icon name="warning" className="text-[16px]" />
              {conflictFiles.length > 0
                ? `${t("diff.mergeConflict")} — ${conflictFiles.length} ${t("diff.mergeConflictFiles").replace("{count}", String(conflictFiles.length))}`
                : t("diff.mergeConflict")}
            </div>
            <div className="flex items-center gap-2">
              <Button
                variant="primary"
                size="sm"
                onClick={onStartResolve}
                disabled={resolvePending}
              >
                {resolvePending ? (
                  <>
                    <Spinner /> {t("diff.starting")}
                  </>
                ) : (
                  <>
                    <Icon name="smart_toy" className="text-[15px]" />
                    {t("diff.resolveWithAgent")}
                  </>
                )}
              </Button>
              <button
                type="button"
                onClick={onDismissConflict}
                className="text-outline hover:text-on-surface"
              >
                {t("diff.dismiss")}
              </button>
            </div>
          </div>
          {conflictFiles.length > 0 && (
            <ul className="ml-1 space-y-0.5">
              {conflictFiles.map((f) => (
                <li key={f} className="font-mono text-[11px] text-error/80">
                  {f}
                </li>
              ))}
            </ul>
          )}
          {resolveError && <p className="mt-2 text-[11px] text-error">{resolveError}</p>}
        </div>
      )}

      {/* Resolve in-progress banner */}
      {resolveStatus === "running" && (
        <Banner tone="running">
          <Spinner />
          <span className="text-body-sm text-on-surface-variant">{t("diff.resolving")}</span>
          {resolveLastEvent && (
            <span className="truncate font-mono text-[11px] text-outline">{resolveLastEvent}</span>
          )}
        </Banner>
      )}

      {/* Resolve failed banner */}
      {resolveStatus === "failed" && (
        <Banner tone="error" onDismiss={onDismissResolve} dismissLabel={t("diff.dismiss")}>
          {resolveError ?? t("diff.resolveRunFailed")}
        </Banner>
      )}

      {/* Connection lost banner — terminal state uncertain; re-merge to verify */}
      {resolveStatus === "unknown" && (
        <Banner tone="neutral" onDismiss={onDismissResolve} dismissLabel={t("diff.dismiss")}>
          {t("diff.connectionLost")}
        </Banner>
      )}
    </>
  );
}

// ---- #47: DiffViewerPane — two-pane body containing file-list + diff-viewer ----

function DiffViewerPane({
  files,
  selectedFile,
  diffResult,
  isLoading,
  selectedParsed,
  onSelectFile,
}: {
  files: DiffResult["files"];
  selectedFile: string;
  diffResult: DiffResult | undefined;
  isLoading: boolean;
  selectedParsed: ReturnType<typeof parseUnifiedDiff>[number] | undefined;
  onSelectFile: (path: string) => void;
}) {
  const t = useT();
  return (
    <div className="flex flex-1 flex-col overflow-hidden md:flex-row">
      {/* Left: Changed files panel
          Mobile: horizontally scrollable inline list (height fixed with shrink-0)
          PC: vertical w-64 fixed panel */}
      <div
        className="flex shrink-0 flex-row overflow-x-auto border-b border-outline-variant md:w-64 md:flex-col md:overflow-x-hidden md:border-b-0 md:border-r"
        data-testid="changed-files-panel"
      >
        {/* Header: hidden on mobile (save space), visible on PC */}
        <div className="hidden p-3 md:block">
          <h3 className="mb-2 text-[10px] uppercase tracking-wider text-outline">
            {t("diff.changedFiles")} ({files.length})
          </h3>
          {diffResult && (
            <p className="data mb-1">
              <span style={{ color: "var(--color-added)" }}>+{diffResult.total_added}</span>{" "}
              <span style={{ color: "var(--color-removed)" }}>−{diffResult.total_deleted}</span>{" "}
              <span className="text-outline/60">{t("diff.inThisRepo")}</span>
            </p>
          )}
          {isLoading && <p className="text-[11px] text-outline">{t("diff.loading")}</p>}
        </div>
        {/* File list: horizontal on mobile, vertical on PC */}
        <div className="flex flex-row md:flex-col md:px-0 md:pb-0">
          {files.map((file) => {
            const isSelected = file.path === selectedFile;
            return (
              <button
                key={file.path}
                type="button"
                onClick={() => onSelectFile(file.path)}
                className={cn(
                  "edge-h flex shrink-0 items-center gap-2 px-3 py-2 text-left transition-colors md:w-full md:px-2 md:py-1.5",
                  "min-h-[44px]",
                  isSelected
                    ? "bg-surface-container-high text-on-surface"
                    : "text-on-surface-variant hover:bg-surface-container hover:text-on-surface",
                )}
              >
                {/* white tick = current location (PC only) */}
                <span
                  className="hidden shrink-0 text-[12px] leading-none md:inline"
                  aria-hidden
                  style={{ color: isSelected ? "var(--color-on-surface)" : "transparent" }}
                >
                  ›
                </span>
                <span
                  className="max-w-[120px] truncate font-mono text-[11px] md:max-w-none md:flex-1"
                  title={file.path}
                >
                  {file.path.split("/").pop()}
                </span>
                <span className="data shrink-0">
                  <span style={{ color: "var(--color-added)" }}>+{file.added}</span>
                  <span style={{ color: "var(--color-removed)" }}> −{file.deleted}</span>
                </span>
              </button>
            );
          })}
          {files.length === 0 && !isLoading && (
            <p className="px-3 py-2 text-[11px] text-outline md:px-2">
              {t("diff.noChangesInMode")}
            </p>
          )}
        </div>
      </div>

      {/* Right: Diff viewer */}
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
        {selectedFile && (
          <div className="flex items-center gap-3 border-b border-outline-variant/50 bg-surface-container-lowest px-4 py-2">
            <code className="min-w-0 truncate font-mono text-[12px] text-on-surface-variant">
              {selectedFile}
            </code>
          </div>
        )}
        {/* Contain horizontal scroll for the diff body within this element */}
        <div className="flex-1 overflow-auto bg-surface-container-lowest">
          <div className="min-w-max">
            {selectedParsed?.lines.map((row, i) => (
              <DiffLineRow
                /* biome-ignore lint/suspicious/noArrayIndexKey: diff lines have no stable id; position within a parsed hunk is the correct identity */
                key={`${row.type}-${row.oldNo ?? ""}-${row.newNo ?? ""}-${i}`}
                {...row}
              />
            ))}
            {!selectedParsed && !isLoading && files.length > 0 && (
              <p className="p-4 text-[11px] text-outline">{t("diff.selectFileToView")}</p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---- #49: DiffToolbarActions — primary button markup for commit/merge/merge-to-default ----

function DiffToolbarActions({
  mode,
  epicId,
  files,
  isRunning,
  showCommitInput,
  commitMsg,
  commitPending,
  showMergeConfirm,
  mergePending,
  onSetMode,
  onShowCommitInput,
  onHideCommitInput,
  onCommitMsgChange,
  onCommit,
  onShowMergeConfirm,
  onHideMergeConfirm,
  onMerge,
}: {
  mode: DiffMode;
  epicId: string;
  files: DiffResult["files"];
  isRunning: boolean;
  showCommitInput: boolean;
  commitMsg: string;
  commitPending: boolean;
  showMergeConfirm: boolean;
  mergePending: boolean;
  onSetMode: (m: DiffMode) => void;
  onShowCommitInput: () => void;
  onHideCommitInput: () => void;
  onCommitMsgChange: (msg: string) => void;
  onCommit: () => void;
  onShowMergeConfirm: () => void;
  onHideMergeConfirm: () => void;
  onMerge: () => void;
}) {
  const t = useT();
  return (
    <>
      {/* Mode toggle */}
      <div className="flex rounded border border-outline-variant">
        {(["working", "epic"] as DiffMode[]).map((m) => (
          <button
            key={m}
            type="button"
            onClick={() => onSetMode(m)}
            className={cn(
              "px-3 py-1.5 text-body-sm transition-colors first:rounded-l last:rounded-r",
              mode === m
                ? "bg-surface-container text-on-surface"
                : "text-on-surface-variant hover:text-on-surface",
            )}
          >
            {m === "working"
              ? t("diff.modeWorking")
              : t("diff.modeEpicVsDefault").replace("{epicId}", epicId)}
          </button>
        ))}
      </div>

      {/* Commit / Merge actions */}
      {mode === "working" ? (
        showCommitInput ? (
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={commitMsg}
              onChange={(e) => onCommitMsgChange(e.target.value)}
              placeholder={t("diff.commitMessage")}
              className="rounded border border-outline-variant bg-surface-container-lowest px-3 py-1.5 font-mono text-code-sm text-on-surface placeholder:text-outline focus:border-[var(--color-light)] focus:outline-none focus:ring-1 focus:ring-[var(--color-light)]"
            />
            <PrimaryActionButton
              onClick={onCommit}
              disabled={!commitMsg.trim() || commitPending || isRunning}
            >
              <Icon name="check" className="text-[16px]" />
              {commitPending ? t("diff.committing") : t("diff.commit")}
            </PrimaryActionButton>
            <button
              type="button"
              onClick={onHideCommitInput}
              className="rounded border border-outline-variant px-3 py-2 text-body-sm text-on-surface-variant hover:bg-surface-variant"
            >
              {t("common.cancel")}
            </button>
          </div>
        ) : (
          <PrimaryActionButton
            onClick={onShowCommitInput}
            disabled={files.length === 0 || isRunning}
          >
            <Icon name="check" className="text-[16px]" />
            {t("diff.commit")}
          </PrimaryActionButton>
        )
      ) : showMergeConfirm ? (
        <div className="flex items-center gap-2">
          <span className="text-body-sm text-on-surface-variant">
            {t("diff.mergeConfirmQuestion")}
          </span>
          <PrimaryActionButton
            onClick={onMerge}
            disabled={mergePending || isRunning}
            data-testid="confirm-merge-btn"
          >
            {mergePending ? t("diff.merging") : t("diff.confirmMerge")}
          </PrimaryActionButton>
          <button
            type="button"
            onClick={onHideMergeConfirm}
            className="rounded border border-outline-variant px-3 py-2 text-body-sm text-on-surface-variant hover:bg-surface-variant"
          >
            {t("common.cancel")}
          </button>
        </div>
      ) : (
        <PrimaryActionButton
          onClick={onShowMergeConfirm}
          disabled={files.length === 0 || isRunning}
          data-testid="merge-to-default-btn"
        >
          <Icon name="merge" className="text-[16px]" />
          {t("diff.mergeToBranch")}
        </PrimaryActionButton>
      )}
    </>
  );
}

// ---- Main Component ----

export function DiffPageClient({ projectId, epicId, epic, initialDiffs }: DiffPageClientProps) {
  const t = useT();
  const qc = useQueryClient();
  // Default to the branch ("epic") diff — the full change set of the epic
  // branch vs the default branch. "working" (uncommitted) is usually empty
  // because the host commits Worker changes automatically, so it is a poor
  // default. Users can still switch to "working" via the toggle.
  const [mode, setMode] = useState<DiffMode>("epic");
  const [activeRepo, setActiveRepo] = useState<string>(initialDiffs[0]?.repo ?? "");
  const [selectedFile, setSelectedFile] = useState<string>(initialDiffs[0]?.files[0]?.path ?? "");
  const [commitMsg, setCommitMsg] = useState("");
  const [showCommitInput, setShowCommitInput] = useState(false);
  const [showMergeConfirm, setShowMergeConfirm] = useState(false);

  // Conflict state
  const [conflictFiles, setConflictFiles] = useState<string[] | null>(null);
  const [conflictResolved, setConflictResolved] = useState(false);
  const [mergeError, setMergeError] = useState<string | null>(null);

  // Prune state
  const [showPruneConfirm, setShowPruneConfirm] = useState(false);
  const [pruneResults, setPruneResults] = useState<RepoPruneResult[] | null>(null);

  const repos = epic?.touched_repos?.length ? epic.touched_repos : initialDiffs.map((d) => d.repo);
  const uniqueRepos = [...new Set(repos)];

  // #44: useResolveRun hook
  const {
    resolveStatus,
    resolveLastEvent,
    resolveError,
    isResolving,
    startResolve,
    dismissResolve,
  } = useResolveRun({
    projectId,
    epicId,
    activeRepo,
    mode,
    qc,
    onResolved: () => {
      setConflictFiles(null);
      setConflictResolved(true);
    },
  });

  const isRunning = resolveStatus === "running";

  const { data: diffResult, isLoading } = useQuery({
    queryKey: queryKeys.git.diff(projectId, epicId, activeRepo, mode),
    queryFn: () => getGitDiff(projectId, epicId, activeRepo, mode),
    initialData: initialDiffs.find((d) => d.repo === activeRepo && d.mode === mode),
    enabled: !!activeRepo,
  });

  const { data: diffSummary } = useQuery({
    queryKey: queryKeys.git.diffSummary(projectId, epicId, mode),
    queryFn: () => getGitDiffSummary(projectId, epicId, mode),
    enabled: uniqueRepos.length > 0,
  });

  const commitMutation = useMutation({
    mutationFn: () => gitCommit(projectId, epicId, { message: commitMsg, repo: activeRepo }),
    onSuccess: () => {
      // #18: consolidate with the umbrella key queryKeys.git.all()
      qc.invalidateQueries({ queryKey: queryKeys.git.all() });
      setShowCommitInput(false);
      setCommitMsg("");
    },
  });

  const mergeMutation = useMutation({
    mutationFn: () => gitMerge(projectId, epicId, { repo: activeRepo }),
    onSuccess: () => {
      // #18: consolidate with the umbrella key queryKeys.git.all()
      qc.invalidateQueries({ queryKey: queryKeys.git.all() });
      setShowMergeConfirm(false);
      setConflictFiles(null);
      setConflictResolved(false);
      setMergeError(null);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        const conflicts = extractConflicts(err);
        setConflictFiles(conflicts);
        setMergeError(null);
      } else {
        setMergeError(err instanceof Error ? err.message : "Merge failed");
      }
      setShowMergeConfirm(false);
    },
  });

  const pruneMutation = useMutation({
    mutationFn: (force: boolean) => gitPrune(projectId, epicId, { force, repos: null }),
    onSuccess: (results) => {
      setPruneResults(results);
      setShowPruneConfirm(false);
      const anyError = results.some((r) => r.error);
      if (!anyError) {
        // #18: consolidate with the umbrella key queryKeys.git.all()
        qc.invalidateQueries({ queryKey: queryKeys.git.all() });
      }
    },
    onError: (err) => {
      setPruneResults(null);
      setShowPruneConfirm(false);
      setMergeError(err instanceof Error ? err.message : "Prune failed");
    },
  });

  const files = diffResult?.files ?? [];
  const summaryAdded = diffSummary?.total_added ?? diffResult?.total_added ?? 0;
  const summaryDeleted = diffSummary?.total_deleted ?? diffResult?.total_deleted ?? 0;

  const parsedFiles = diffResult?.unified_diff ? parseUnifiedDiff(diffResult.unified_diff) : [];
  const selectedParsed =
    parsedFiles.find(
      (f) =>
        f.newPath === selectedFile ||
        f.oldPath === selectedFile ||
        f.newPath.endsWith(selectedFile) ||
        selectedFile.endsWith(f.newPath),
    ) ?? parsedFiles[0];

  const isEpicCompleted = epic?.status === "completed" || epic?.status === "merged";

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Toolbar — actions only (heading already consolidated into the nameplate) */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-variant bg-surface px-4 py-3 md:px-6">
        <div className="flex items-center gap-2">
          <span className="font-mono text-[11px]">
            <span style={{ color: "var(--color-added)" }}>+{summaryAdded}</span>{" "}
            <span style={{ color: "var(--color-removed)" }}>−{summaryDeleted}</span>
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2 md:gap-3">
          {/* Clean up (prune) button — epic mode, completed status */}
          {isEpicCompleted && (
            <Button
              variant="secondary"
              size="sm"
              onClick={() => setShowPruneConfirm(true)}
              disabled={isRunning}
            >
              <Icon name="cleaning_services" className="text-[15px]" />
              {t("diff.cleanUp")}
            </Button>
          )}

          {/* #49: DiffToolbarActions */}
          <DiffToolbarActions
            mode={mode}
            epicId={epicId}
            files={files}
            isRunning={isRunning}
            showCommitInput={showCommitInput}
            commitMsg={commitMsg}
            commitPending={commitMutation.isPending}
            showMergeConfirm={showMergeConfirm}
            mergePending={mergeMutation.isPending}
            onSetMode={setMode}
            onShowCommitInput={() => setShowCommitInput(true)}
            onHideCommitInput={() => setShowCommitInput(false)}
            onCommitMsgChange={setCommitMsg}
            onCommit={() => commitMutation.mutate()}
            onShowMergeConfirm={() => setShowMergeConfirm(true)}
            onHideMergeConfirm={() => setShowMergeConfirm(false)}
            onMerge={() => mergeMutation.mutate()}
          />
        </div>
      </div>

      {/* #45: DiffStatusBanners */}
      <DiffStatusBanners
        mergeError={mergeError}
        conflictResolved={conflictResolved}
        conflictFiles={conflictFiles}
        resolveStatus={resolveStatus}
        resolveLastEvent={resolveLastEvent}
        resolveError={resolveError}
        resolvePending={isResolving}
        onDismissMergeError={() => setMergeError(null)}
        onDismissConflictResolved={() => setConflictResolved(false)}
        onDismissConflict={() => {
          setConflictFiles(null);
          dismissResolve();
        }}
        onStartResolve={startResolve}
        onDismissResolve={dismissResolve}
      />

      {/* Prune results panel */}
      {pruneResults !== null && (
        <div className="border-b border-outline-variant/50 bg-surface-container px-6 py-4">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-body-sm font-medium text-on-surface">
              {t("diff.cleanUpResults")}
            </span>
            <button
              type="button"
              onClick={() => setPruneResults(null)}
              className="text-outline hover:text-on-surface"
              aria-label={t("diff.dismiss")}
            >
              <Icon name="close" className="text-[16px]" />
            </button>
          </div>
          <div className="space-y-1.5">
            {pruneResults.map((r) => (
              <div key={r.repo} className="flex items-start gap-3 text-[12px]">
                <span className="w-40 shrink-0 font-mono text-on-surface-variant">{r.repo}</span>
                {r.error ? (
                  <div className="flex items-center gap-2">
                    <span className="text-error">{r.error}</span>
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() => pruneMutation.mutate(true)}
                      disabled={pruneMutation.isPending}
                    >
                      {t("diff.cleanUpForce")}
                    </Button>
                  </div>
                ) : (
                  <span className="text-on-surface-variant">
                    {[
                      r.worktree_removed && t("diff.pruneWorktreeRemoved"),
                      r.branch_deleted && t("diff.pruneBranchDeleted"),
                    ]
                      .filter(Boolean)
                      .join(", ") || t("diff.pruneNoChanges")}
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Repo tabs */}
      {uniqueRepos.length > 1 && (
        <div className="flex border-b border-outline-variant/50 bg-surface-container-lowest">
          {uniqueRepos.map((repo) => {
            const repoSummary = diffSummary?.repos.find((r) => r.repo === repo);
            return (
              <button
                key={repo}
                type="button"
                onClick={() => setActiveRepo(repo)}
                className={cn(
                  "flex items-center gap-1.5 px-4 py-2 text-[11px] transition-colors",
                  repo === activeRepo
                    ? "border-b-2 border-primary font-medium text-on-surface"
                    : "text-on-surface-variant hover:text-on-surface",
                )}
              >
                {repo}
                {repoSummary && (
                  <span className="data">
                    <span style={{ color: "var(--color-added)" }}>+{repoSummary.added}</span>
                    <span style={{ color: "var(--color-removed)" }}> −{repoSummary.deleted}</span>
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}

      {/* #47: DiffViewerPane */}
      <DiffViewerPane
        files={files}
        selectedFile={selectedFile}
        diffResult={diffResult}
        isLoading={isLoading}
        selectedParsed={selectedParsed}
        onSelectFile={setSelectedFile}
      />

      {/* Prune confirm dialog */}
      <Dialog open={showPruneConfirm} onOpenChange={setShowPruneConfirm}>
        <DialogContent title={t("diff.cleanUpConfirmTitle")}>
          <p className="mb-4 text-body-sm text-on-surface-variant">
            {t("diff.cleanUpConfirmBody")}
          </p>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setShowPruneConfirm(false)}>
              {t("diff.cleanUpCancel")}
            </Button>
            <Button
              variant="primary"
              disabled={pruneMutation.isPending}
              onClick={() => pruneMutation.mutate(false)}
            >
              {pruneMutation.isPending ? t("diff.cleanUpCleaning") : t("diff.cleanUp")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
