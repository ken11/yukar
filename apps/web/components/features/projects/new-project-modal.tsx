"use client";

import type { ReactNode } from "react";
import { useRef, useState } from "react";
import { Icon } from "@/components/icon";
import { Button } from "@/components/ui/button";
import { FormDialog } from "@/components/ui/form-dialog";
import { createProject } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { useModalMutation } from "@/lib/hooks/use-modal-mutation";
import { useT } from "@/lib/i18n/provider";
import { linesToArray } from "@/lib/text";

interface RepoEntry {
  uid: number;
  name: string;
  path: string;
  defaultBranch: string;
  commandsExpanded: boolean;
  commandsAllow: string;
  commandsDeny: string;
}

interface NewProjectModalProps {
  /** Custom trigger element. When omitted, the standard "New Project" button is used. */
  trigger?: ReactNode;
}

export function NewProjectModal({ trigger }: NewProjectModalProps = {}) {
  const t = useT();
  const uidRef = useRef(0);
  const nextUid = () => ++uidRef.current;

  const [repos, setRepos] = useState<RepoEntry[]>([
    {
      uid: 0,
      name: "",
      path: "",
      defaultBranch: "main",
      commandsExpanded: false,
      commandsAllow: "",
      commandsDeny: "",
    },
  ]);
  const [projectName, setProjectName] = useState("");
  const [projectId, setProjectId] = useState("");

  function resetForm() {
    setProjectName("");
    setProjectId("");
    setRepos([
      {
        uid: nextUid(),
        name: "",
        path: "",
        defaultBranch: "main",
        commandsExpanded: false,
        commandsAllow: "",
        commandsDeny: "",
      },
    ]);
  }

  const { isOpen, setOpen, error, setError, isPending, submit } = useModalMutation({
    mutationFn: () =>
      createProject({
        id: projectId || projectName.toLowerCase().replace(/\s+/g, "-"),
        name: projectName,
        repos: repos.map((r) => {
          const allow = linesToArray(r.commandsAllow);
          const deny = linesToArray(r.commandsDeny);
          const hasCommands = allow.length > 0 || deny.length > 0;
          return {
            name: r.name || r.path.split("/").pop() || r.name,
            path: r.path,
            default_branch: r.defaultBranch,
            ...(hasCommands ? { commands: { allow, deny } } : {}),
          };
        }),
      }),
    invalidateKeys: [queryKeys.projects.list()],
    onSuccess: resetForm,
    fallbackError: "Failed to create project",
  });

  function addRepo() {
    setRepos((prev) => [
      ...prev,
      {
        uid: nextUid(),
        name: "",
        path: "",
        defaultBranch: "main",
        commandsExpanded: false,
        commandsAllow: "",
        commandsDeny: "",
      },
    ]);
  }

  function removeRepo(index: number) {
    setRepos((prev) => prev.filter((_, i) => i !== index));
  }

  function updateRepo(index: number, field: keyof RepoEntry, value: string | boolean) {
    setRepos((prev) => prev.map((r, i) => (i === index ? { ...r, [field]: value } : r)));
  }

  const canSubmit =
    projectName.trim() !== "" && repos.every((r) => r.path.trim() !== "") && !isPending;

  return (
    <FormDialog
      open={isOpen}
      onOpenChange={(v) => {
        setOpen(v);
        if (!v) {
          resetForm();
          setError(null);
        }
      }}
      trigger={
        trigger ?? (
          <Button variant="primary" data-testid="new-project-btn">
            <Icon name="add" className="text-[18px]" />
            {t("common.newProject")}
          </Button>
        )
      }
      title="New Project"
      description="Register existing local repositories. yukar does not clone — point to repos already on your machine."
      error={error}
      submitLabel={
        <>
          <Icon name="folder_open" className="text-[16px]" />
          Initialize &amp; Index
        </>
      }
      submitPendingLabel={
        <>
          <Icon name="folder_open" className="text-[16px]" />
          Creating…
        </>
      }
      submitDisabled={!canSubmit}
      isPending={isPending}
      onSubmit={() => submit()}
      buttonClassName="text-body-sm"
    >
      <div className="space-y-4">
        <div>
          <label
            htmlFor="project-name"
            className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
          >
            Project Name
          </label>
          <input
            id="project-name"
            data-testid="project-name-input"
            type="text"
            value={projectName}
            onChange={(e) => setProjectName(e.target.value)}
            placeholder="my-project"
            className="w-full rounded border border-outline-variant bg-surface-container-lowest px-3 py-2 text-body-md text-on-surface placeholder:text-outline focus:border-[var(--color-light)] focus:outline-none focus:ring-1 focus:ring-[var(--color-light)]"
          />
        </div>

        <div>
          <div className="mb-2 flex items-center justify-between">
            {/* biome-ignore lint/a11y/noLabelWithoutControl: section heading for dynamic list of repo inputs */}
            <label className="text-[11px] uppercase tracking-wider text-outline">
              Repositories
            </label>
            <button
              type="button"
              onClick={addRepo}
              className="flex items-center gap-1 text-[11px] text-outline transition-colors hover:text-secondary"
            >
              <Icon name="add" className="text-[14px]" />
              Add Repo
            </button>
          </div>

          <div className="space-y-2">
            {repos.map((repo, i) => (
              <div key={repo.uid} className="flex items-start gap-2">
                <div className="flex-1 space-y-2">
                  <input
                    data-testid={`repo-path-input-${i}`}
                    type="text"
                    value={repo.path}
                    onChange={(e) => updateRepo(i, "path", e.target.value)}
                    placeholder="/Users/you/git/my-repo"
                    className="w-full rounded border border-outline-variant bg-surface-container-lowest px-3 py-1.5 font-mono text-code-sm text-on-surface placeholder:text-outline focus:border-[var(--color-light)] focus:outline-none focus:ring-1 focus:ring-[var(--color-light)]"
                  />
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] text-outline">default branch:</span>
                    <input
                      type="text"
                      value={repo.defaultBranch}
                      onChange={(e) => updateRepo(i, "defaultBranch", e.target.value)}
                      placeholder="main"
                      className="w-24 rounded border border-outline-variant bg-surface-container-lowest px-2 py-1 font-mono text-code-sm text-on-surface placeholder:text-outline focus:border-[var(--color-light)] focus:outline-none focus:ring-1 focus:ring-[var(--color-light)]"
                    />
                    {/* Commands toggle */}
                    <button
                      type="button"
                      data-testid={`repo-commands-toggle-${i}`}
                      onClick={() => updateRepo(i, "commandsExpanded", !repo.commandsExpanded)}
                      className="ml-auto flex items-center gap-1 text-[11px] text-outline transition-colors hover:text-on-surface"
                    >
                      <Icon
                        name={repo.commandsExpanded ? "expand_less" : "expand_more"}
                        className="text-[14px]"
                      />
                      {repo.commandsExpanded ? "Hide commands" : "Set commands"}
                    </button>
                  </div>

                  {/* Commands panel */}
                  {repo.commandsExpanded && (
                    <div
                      data-testid={`repo-commands-panel-${i}`}
                      className="grid grid-cols-2 gap-2 rounded border border-outline-variant/40 bg-surface-container-lowest p-3"
                    >
                      <div>
                        <label
                          htmlFor={`repo-allow-${i}`}
                          className="mb-1 block text-[10px] uppercase tracking-wider text-outline"
                        >
                          Allow{" "}
                          <span className="normal-case tracking-normal text-outline/60">
                            (one per line)
                          </span>
                        </label>
                        <textarea
                          id={`repo-allow-${i}`}
                          data-testid={`repo-modal-allow-${i}`}
                          rows={3}
                          value={repo.commandsAllow}
                          onChange={(e) => updateRepo(i, "commandsAllow", e.target.value)}
                          placeholder={"pnpm test\npytest"}
                          className="w-full rounded border border-outline-variant bg-surface-container px-2 py-1 font-mono text-code-sm text-on-surface placeholder:text-outline focus:border-[var(--color-light)] focus:outline-none focus:ring-1 focus:ring-[var(--color-light)] resize-none"
                        />
                        <p className="mt-0.5 text-[10px] text-outline">
                          Empty = no commands allowed.
                        </p>
                      </div>
                      <div>
                        <label
                          htmlFor={`repo-deny-${i}`}
                          className="mb-1 block text-[10px] uppercase tracking-wider text-outline"
                        >
                          Deny{" "}
                          <span className="normal-case tracking-normal text-outline/60">
                            (one per line)
                          </span>
                        </label>
                        <textarea
                          id={`repo-deny-${i}`}
                          data-testid={`repo-modal-deny-${i}`}
                          rows={3}
                          value={repo.commandsDeny}
                          onChange={(e) => updateRepo(i, "commandsDeny", e.target.value)}
                          placeholder={"rm -rf"}
                          className="w-full rounded border border-outline-variant bg-surface-container px-2 py-1 font-mono text-code-sm text-on-surface placeholder:text-outline focus:border-[var(--color-light)] focus:outline-none focus:ring-1 focus:ring-[var(--color-light)] resize-none"
                        />
                        <p className="mt-0.5 text-[10px] text-outline">Overrides allow list.</p>
                      </div>
                    </div>
                  )}
                </div>
                {repos.length > 1 && (
                  <button
                    type="button"
                    onClick={() => removeRepo(i)}
                    className="mt-1 rounded p-1 text-outline transition-colors hover:bg-surface-variant hover:text-error"
                  >
                    <Icon name="remove" className="text-[16px]" />
                  </button>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>
    </FormDialog>
  );
}
