"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import dynamic from "next/dynamic";
import { useState } from "react";
import { Icon } from "@/components/icon";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import type { Skill, SkillMeta } from "@/lib/api/endpoints";
import { deleteSkill, getSkill, listSkills, putSkill } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { useResetTimer } from "@/lib/hooks/use-reset-timer";
import { useSaveState } from "@/lib/hooks/use-save-state";
import { useDict } from "@/lib/i18n/provider";

const CodeMirrorEditor = dynamic(
  () => import("@/components/features/editor/code-mirror-editor").then((m) => m.CodeMirrorEditor),
  {
    ssr: false,
    loading: () => (
      <div className="flex h-full items-center justify-center text-[12px] text-outline">
        Loading editor…
      </div>
    ),
  },
);

function buildDefaultContent(name: string): string {
  return `---
name: ${name}
description:
---

# ${name}

Describe the skill here.
`;
}

interface SkillsSectionProps {
  projectId: string;
  initialSkills: SkillMeta[];
}

export function SkillsSection({ projectId, initialSkills }: SkillsSectionProps) {
  const t = useDict();
  const ps = t.projectSettings ?? ({} as NonNullable<(typeof t)["projectSettings"]>);
  const qc = useQueryClient();
  const scheduleReset = useResetTimer();

  const { data: skills = initialSkills } = useQuery({
    queryKey: queryKeys.skills.list(projectId),
    queryFn: () => listSkills(projectId),
    initialData: initialSkills,
    staleTime: 30_000,
  });

  const [selectedName, setSelectedName] = useState<string | null>(skills[0]?.name ?? null);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [editingContent, setEditingContent] = useState<string | null>(null);
  const [isCreating, setIsCreating] = useState(false);
  const [newName, setNewName] = useState("");
  // #14: consolidate saveError/setError into useSaveState. savedName is kept separate because it is name-based.
  const { saveError, setSaveError, setError } = useSaveState("Save failed");
  const [savedName, setSavedName] = useState<string | null>(null);

  const { data: selectedSkill, isLoading: skillLoading } = useQuery<Skill>({
    queryKey: queryKeys.skills.detail(projectId, selectedName ?? ""),
    queryFn: () => getSkill(projectId, selectedName as string),
    enabled: !!selectedName,
    staleTime: 30_000,
  });

  const [lastFetchedName, setLastFetchedName] = useState<string | null>(null);
  if (selectedSkill && selectedSkill.name !== lastFetchedName) {
    setLastFetchedName(selectedSkill.name);
    setEditingContent(selectedSkill.content);
  }

  const saveMutation = useMutation({
    mutationFn: ({ name, content }: { name: string; content: string }) =>
      putSkill(projectId, name, content),
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.skills.detail(projectId, data.name), data);
      qc.invalidateQueries({ queryKey: queryKeys.skills.list(projectId) });
      setSavedName(data.name);
      setSaveError(null);
      setIsCreating(false);
      setNewName("");
      if (!skills.find((s) => s.name === data.name)) {
        setSelectedName(data.name);
        setLastFetchedName(data.name);
        setEditingContent(data.content);
      }
      scheduleReset(() => setSavedName(null));
    },
    onError: (err) => {
      // #14: consolidate err instanceof Error check into setError
      setError(err);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (name: string) => deleteSkill(projectId, name),
    onSuccess: (_, deletedName) => {
      qc.invalidateQueries({ queryKey: queryKeys.skills.list(projectId) });
      qc.removeQueries({ queryKey: queryKeys.skills.detail(projectId, deletedName) });
      if (selectedName === deletedName) {
        const next = skills.find((s) => s.name !== deletedName);
        setSelectedName(next?.name ?? null);
        setEditingContent(null);
        setLastFetchedName(null);
      }
    },
    onError: (err) => {
      // #14: consolidate err instanceof Error check into setError
      setError(err, "Delete failed");
    },
  });

  function handleSelectSkill(name: string) {
    setSelectedName(name);
    setEditingContent(null);
    setLastFetchedName(null);
    setIsCreating(false);
    setSaveError(null);
  }

  function handleStartCreate() {
    setIsCreating(true);
    setSelectedName(null);
    setEditingContent(null);
    setLastFetchedName(null);
    setNewName("");
    setSaveError(null);
  }

  function handleCreateSave() {
    if (!newName.trim()) {
      setSaveError("Skill name is required");
      return;
    }
    const content = editingContent ?? buildDefaultContent(newName.trim());
    saveMutation.mutate({ name: newName.trim(), content });
  }

  function handleSaveExisting() {
    if (!selectedName || editingContent === null) return;
    saveMutation.mutate({ name: selectedName, content: editingContent });
  }

  const activeContent = isCreating
    ? (editingContent ?? buildDefaultContent(newName || "new-skill"))
    : (editingContent ?? selectedSkill?.content ?? "");

  return (
    <>
      <section aria-labelledby="proj-section-skills">
        <p
          id="proj-section-skills"
          className="mb-1 text-[11px] font-medium uppercase tracking-[0.05em] text-on-surface-variant"
        >
          {ps.sections.skills}
        </p>
        <p className="mb-5 text-[12px] text-outline">{ps.sections.skillsHint}</p>

        <div className="flex gap-5">
          {/* Skill list sidebar */}
          <div className="flex w-44 shrink-0 flex-col gap-0.5">
            {skills.map((skill) => (
              <button
                key={skill.name}
                type="button"
                data-testid={`skill-list-item-${skill.name}`}
                onClick={() => handleSelectSkill(skill.name)}
                className={cn(
                  "flex items-start gap-2 rounded px-3 py-2 text-left transition-colors",
                  selectedName === skill.name && !isCreating
                    ? "bg-surface-container-high text-on-surface"
                    : "text-on-surface-variant hover:bg-surface-container-high/50 hover:text-on-surface",
                )}
              >
                <Icon name="auto_awesome" className="mt-0.5 shrink-0 text-[13px]" />
                <div className="min-w-0">
                  <p className="truncate text-[13px] font-medium">{skill.name}</p>
                  {skill.description && (
                    <p className="mt-0.5 truncate text-[11px] text-outline">{skill.description}</p>
                  )}
                </div>
              </button>
            ))}
            <button
              type="button"
              onClick={handleStartCreate}
              className={cn(
                "flex items-center gap-2 rounded px-3 py-2 text-[12px] transition-colors",
                isCreating
                  ? "bg-surface-container-high text-[var(--color-light)]"
                  : "border border-dashed border-outline-variant/50 text-outline hover:border-outline hover:text-on-surface",
              )}
            >
              <Icon name="add" className="text-[13px]" />
              New Skill
            </button>
          </div>

          {/* Editor panel */}
          <div className="flex flex-1 flex-col gap-3">
            {isCreating && (
              <div className="flex items-center gap-3">
                <label
                  htmlFor="new-skill-name"
                  className="text-[11px] uppercase tracking-wider text-outline whitespace-nowrap"
                >
                  Name
                </label>
                <input
                  id="new-skill-name"
                  data-testid="new-skill-name-input"
                  type="text"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder="e.g. run-tests"
                  className="min-w-0 flex-1 rounded border border-outline-variant bg-surface-container-lowest px-3 py-1.5 font-mono text-[13px] text-on-surface focus:border-outline focus:outline-none focus:ring-1 focus:ring-white/20"
                />
              </div>
            )}

            {(isCreating || selectedName) && (
              <>
                {/* recess surface for editor */}
                <div className="h-72 overflow-hidden rounded border border-outline-variant bg-surface-container-lowest">
                  {skillLoading && !isCreating ? (
                    <div className="flex h-full items-center justify-center text-[12px] text-outline">
                      Loading…
                    </div>
                  ) : (
                    <CodeMirrorEditor
                      key={isCreating ? "__new__" : (selectedName ?? "")}
                      value={activeContent}
                      onChange={setEditingContent}
                      language="markdown"
                    />
                  )}
                </div>

                <div className="flex items-center gap-3">
                  {saveError && (
                    <span className="text-[12px]" style={{ color: "var(--color-removed)" }}>
                      {saveError}
                    </span>
                  )}
                  <Button
                    variant="primary"
                    data-testid="save-skill-btn"
                    onClick={isCreating ? handleCreateSave : handleSaveExisting}
                    disabled={saveMutation.isPending}
                  >
                    <Icon
                      name={
                        savedName === (isCreating ? newName.trim() : (selectedName ?? ""))
                          ? "check"
                          : "save"
                      }
                      className="text-[16px]"
                    />
                    {saveMutation.isPending ? ps.saving : "Save Skill"}
                  </Button>
                  {selectedName && !isCreating && (
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() => {
                        setShowDeleteConfirm(true);
                      }}
                      disabled={deleteMutation.isPending}
                    >
                      <Icon name="delete" className="text-[14px]" />
                      {deleteMutation.isPending ? "Deleting…" : "Delete"}
                    </Button>
                  )}
                </div>
              </>
            )}

            {!isCreating && !selectedName && skills.length === 0 && (
              <div className="flex flex-1 flex-col items-center justify-center gap-2 py-8 text-outline">
                <Icon name="auto_awesome" className="text-[32px] opacity-30" />
                <p className="text-[13px]">No skills yet.</p>
              </div>
            )}
          </div>
        </div>
      </section>

      {/* Delete confirmation dialog */}
      <Dialog open={showDeleteConfirm} onOpenChange={setShowDeleteConfirm}>
        <DialogContent title="Delete Skill">
          <p className="mb-4 text-body-sm text-on-surface-variant">
            Delete skill &ldquo;{selectedName}&rdquo;? This cannot be undone.
          </p>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setShowDeleteConfirm(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              disabled={deleteMutation.isPending}
              data-testid="confirm-delete-skill-btn"
              onClick={() => {
                if (selectedName) {
                  deleteMutation.mutate(selectedName);
                }
                setShowDeleteConfirm(false);
              }}
            >
              {deleteMutation.isPending ? "Deleting…" : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
