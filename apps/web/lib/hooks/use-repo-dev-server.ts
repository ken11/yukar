"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { DevServerConfig, Repo } from "@/lib/api/endpoints";
import { deleteRepoDevServer, extractDetail, putRepoDevServer } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import type { DevServerDraft, DraftValidationError, ServiceDraft } from "@/lib/dev-server/draft";
import { configFromDraft, draftFromConfig, emptyDevServerDraft } from "@/lib/dev-server/draft";
import { useResetTimer } from "@/lib/hooks/use-reset-timer";
import { useT } from "@/lib/i18n/provider";

export type { DevServerDraft, ServiceDraft };

/**
 * Encapsulates draft state + save/remove mutations for each repo's dev-server
 * launch config. Mirrors useRepoCommands: per-repo drafts, per-repo
 * pending/saved/error maps, and a handleSave/handleRemove per repo.
 */
export function useRepoDevServer(projectId: string, initialRepos: Repo[]) {
  const t = useT();
  const qc = useQueryClient();
  const scheduleReset = useResetTimer();

  const [drafts, setDrafts] = useState<Record<string, DevServerDraft>>(() =>
    Object.fromEntries(initialRepos.map((r) => [r.name, draftFromConfig(r.dev_server)])),
  );
  const [saveErrors, setSaveErrors] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<Record<string, boolean>>({});
  const [pending, setPending] = useState<Record<string, boolean>>({});

  function validationMessage(draft: DevServerDraft, error: DraftValidationError): string {
    if (error.code === "noServices") {
      return t("repos.devServer.errors.noServices");
    }
    const service = draft.services[error.serviceIndex]?.name.trim() || `#${error.serviceIndex + 1}`;
    const key = {
      serviceNameRequired: "repos.devServer.errors.serviceNameRequired",
      invalidServiceName: "repos.devServer.errors.invalidServiceName",
      duplicateServiceName: "repos.devServer.errors.duplicateServiceName",
      commandRequired: "repos.devServer.errors.commandRequired",
      invalidPort: "repos.devServer.errors.invalidPort",
      invalidTimeout: "repos.devServer.errors.invalidTimeout",
      invalidEnvLine: "repos.devServer.errors.invalidEnvLine",
      invalidEnvPassthroughName: "repos.devServer.errors.invalidEnvPassthroughName",
    }[error.code];
    let message = t(key).replace("{service}", service);
    if (error.code === "invalidEnvLine" || error.code === "invalidEnvPassthroughName") {
      message = message.replace("{line}", error.line);
    }
    return message;
  }

  const saveMutation = useMutation({
    mutationFn: ({ repoName, config }: { repoName: string; config: DevServerConfig }) =>
      putRepoDevServer(projectId, repoName, config),
    onMutate: ({ repoName }) => {
      setPending((prev) => ({ ...prev, [repoName]: true }));
    },
    onSuccess: (data, { repoName }) => {
      qc.invalidateQueries({ queryKey: queryKeys.repos.list(projectId) });
      setPending((prev) => ({ ...prev, [repoName]: false }));
      setSaved((prev) => ({ ...prev, [repoName]: true }));
      setSaveErrors((prev) => ({ ...prev, [repoName]: "" }));
      if (data.dev_server) {
        setDrafts((prev) => ({ ...prev, [repoName]: draftFromConfig(data.dev_server) }));
      }
      scheduleReset(() => setSaved((prev) => ({ ...prev, [repoName]: false })), { key: repoName });
    },
    onError: (err, { repoName }) => {
      setPending((prev) => ({ ...prev, [repoName]: false }));
      setSaveErrors((prev) => ({
        ...prev,
        // Prefer the backend's `detail` (e.g. "Dev server failed to start: …")
        // over the generic "API 4xx: …" ApiError.message.
        [repoName]: extractDetail(err) ?? (err instanceof Error ? err.message : "Save failed"),
      }));
    },
  });

  const removeMutation = useMutation({
    mutationFn: (repoName: string) => deleteRepoDevServer(projectId, repoName),
    onMutate: (repoName) => {
      setPending((prev) => ({ ...prev, [repoName]: true }));
    },
    onSuccess: (_data, repoName) => {
      qc.invalidateQueries({ queryKey: queryKeys.repos.list(projectId) });
      setPending((prev) => ({ ...prev, [repoName]: false }));
      setSaveErrors((prev) => ({ ...prev, [repoName]: "" }));
      setDrafts((prev) => ({ ...prev, [repoName]: emptyDevServerDraft() }));
    },
    onError: (err, repoName) => {
      setPending((prev) => ({ ...prev, [repoName]: false }));
      setSaveErrors((prev) => ({
        ...prev,
        [repoName]: extractDetail(err) ?? (err instanceof Error ? err.message : "Save failed"),
      }));
    },
  });

  function patchDraft(repoName: string, patch: Partial<DevServerDraft>) {
    setDrafts((prev) => ({
      ...prev,
      [repoName]: { ...(prev[repoName] ?? emptyDevServerDraft()), ...patch },
    }));
  }

  function patchService(repoName: string, idx: number, patch: Partial<ServiceDraft>) {
    setDrafts((prev) => {
      const draft = prev[repoName] ?? emptyDevServerDraft();
      return {
        ...prev,
        [repoName]: {
          ...draft,
          services: draft.services.map((s, i) => (i === idx ? { ...s, ...patch } : s)),
        },
      };
    });
  }

  function handleSave(repoName: string) {
    const draft = drafts[repoName] ?? emptyDevServerDraft();
    const result = configFromDraft(draft);
    if (!result.ok) {
      setSaveErrors((prev) => ({ ...prev, [repoName]: validationMessage(draft, result.error) }));
      return;
    }
    setSaveErrors((prev) => ({ ...prev, [repoName]: "" }));
    saveMutation.mutate({ repoName, config: result.config });
  }

  function handleRemove(repoName: string) {
    removeMutation.mutate(repoName);
  }

  return {
    drafts,
    patchDraft,
    patchService,
    saveErrors,
    saved,
    pending,
    handleSave,
    handleRemove,
  };
}
