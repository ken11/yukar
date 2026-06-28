"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { Repo, RepoCommands } from "@/lib/api/endpoints";
import { putRepoCommands } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { useResetTimer } from "@/lib/hooks/use-reset-timer";
import { arrayToLines, linesToArray } from "@/lib/text";

export interface RepoDraft {
  allowText: string;
  denyText: string;
}

export function repoDraftFromRepo(repo: Repo): RepoDraft {
  return {
    allowText: arrayToLines(repo.commands?.allow),
    denyText: arrayToLines(repo.commands?.deny),
  };
}

/**
 * Encapsulates draft state + putRepoCommands mutation for one or more repos.
 *
 * Returns:
 *   drafts / patchDraft – per-repo draft state
 *   saveErrors / saved / pending – per-repo status maps
 *   handleSave(repoName) – fire the mutation for a given repo
 */
export function useRepoCommands(projectId: string, initialRepos: Repo[]) {
  const qc = useQueryClient();
  const scheduleReset = useResetTimer();

  const [drafts, setDrafts] = useState<Record<string, RepoDraft>>(() =>
    Object.fromEntries(initialRepos.map((r) => [r.name, repoDraftFromRepo(r)])),
  );
  const [saveErrors, setSaveErrors] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<Record<string, boolean>>({});
  const [pending, setPending] = useState<Record<string, boolean>>({});

  const mutation = useMutation({
    mutationFn: ({ repoName, commands }: { repoName: string; commands: RepoCommands }) =>
      putRepoCommands(projectId, repoName, commands),
    onMutate: ({ repoName }) => {
      setPending((prev) => ({ ...prev, [repoName]: true }));
    },
    onSuccess: (data, { repoName }) => {
      qc.invalidateQueries({ queryKey: queryKeys.repos.list(projectId) });
      setPending((prev) => ({ ...prev, [repoName]: false }));
      setSaved((prev) => ({ ...prev, [repoName]: true }));
      setSaveErrors((prev) => ({ ...prev, [repoName]: "" }));
      if (data.commands) {
        setDrafts((prev) => ({
          ...prev,
          [repoName]: {
            allowText: arrayToLines(data.commands?.allow),
            denyText: arrayToLines(data.commands?.deny),
          },
        }));
      }
      scheduleReset(() => setSaved((prev) => ({ ...prev, [repoName]: false })), { key: repoName });
    },
    onError: (err, { repoName }) => {
      setPending((prev) => ({ ...prev, [repoName]: false }));
      setSaveErrors((prev) => ({
        ...prev,
        [repoName]: err instanceof Error ? err.message : "Save failed",
      }));
    },
  });

  function patchDraft(repoName: string, patch: Partial<RepoDraft>) {
    setDrafts((prev) => ({
      ...prev,
      [repoName]: { ...prev[repoName], ...patch },
    }));
  }

  function handleSave(repoName: string) {
    const draft = drafts[repoName] ?? { allowText: "", denyText: "" };
    const commands: RepoCommands = {
      allow: linesToArray(draft.allowText),
      deny: linesToArray(draft.denyText),
    };
    mutation.mutate({ repoName, commands });
  }

  return { drafts, patchDraft, saveErrors, saved, pending, handleSave };
}
