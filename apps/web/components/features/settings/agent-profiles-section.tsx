"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  inputClass,
  inputMonoClass,
  textareaClass,
} from "@/components/features/settings/settings-primitives";
import { Icon } from "@/components/icon";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import type { AgentProfile, McpConfig, SkillMeta } from "@/lib/api/endpoints";
import {
  deleteAgentProfile,
  getMcpConfig,
  listAgentProfiles,
  listSkills,
  putAgentProfile,
} from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { useResetTimer } from "@/lib/hooks/use-reset-timer";
import { useSaveState } from "@/lib/hooks/use-save-state";
import { useDict } from "@/lib/i18n/provider";

// ---- Form state ----------------------------------------------------

interface ProfileDraft {
  name: string;
  description: string;
  base_role: "worker" | "evaluator";
  instructions: string;
  skills: string[];
  mcp_servers: string[];
}

function profileToDraft(p: AgentProfile): ProfileDraft {
  return {
    name: p.name,
    description: p.description,
    base_role: p.base_role,
    instructions: p.instructions,
    skills: p.skills ?? [],
    mcp_servers: p.mcp_servers ?? [],
  };
}

function emptyDraft(): ProfileDraft {
  return {
    name: "",
    description: "",
    base_role: "worker",
    instructions: "",
    skills: [],
    mcp_servers: [],
  };
}

function draftToProfile(d: ProfileDraft): AgentProfile {
  return {
    name: d.name,
    description: d.description,
    base_role: d.base_role,
    instructions: d.instructions,
    skills: d.skills.length > 0 ? d.skills : undefined,
    mcp_servers: d.mcp_servers.length > 0 ? d.mcp_servers : undefined,
  };
}

// ---- MultiSelect ---------------------------------------------------

interface MultiSelectProps {
  label: string;
  items: string[];
  selected: string[];
  onChange: (next: string[]) => void;
  "data-testid"?: string;
}

function MultiSelect({
  label,
  items,
  selected,
  onChange,
  "data-testid": testId,
}: MultiSelectProps) {
  function toggle(item: string) {
    if (selected.includes(item)) {
      onChange(selected.filter((s) => s !== item));
    } else {
      onChange([...selected, item]);
    }
  }

  // Selected entries that are no longer offered (e.g. a repo allow-list command
  // was renamed/removed after this profile was saved). Render them as removable
  // "stale" chips so they stay visible and clearable instead of silently
  // persisting — a disjoint allow list otherwise blocks every command at dispatch.
  const staleSelected = selected.filter((s) => !items.includes(s));

  if (items.length === 0 && staleSelected.length === 0) {
    return (
      <p data-testid={testId} className="text-[11px] text-outline italic">
        No {label.toLowerCase()} defined for this project.
      </p>
    );
  }

  return (
    <div data-testid={testId} className="flex flex-wrap gap-2">
      {items.map((item) => (
        <button
          key={item}
          type="button"
          onClick={() => toggle(item)}
          className={cn(
            "rounded border px-2 py-0.5 font-mono text-[11px] transition-colors focus:outline-none focus:ring-1 focus:ring-white/20",
            selected.includes(item)
              ? "border-[var(--color-light)]/40 bg-[var(--color-light)]/10 text-[var(--color-light)]"
              : "border-outline-variant/50 text-outline hover:border-outline hover:text-on-surface",
          )}
        >
          {item}
        </button>
      ))}
      {staleSelected.map((item) => (
        <button
          key={`stale-${item}`}
          type="button"
          onClick={() => toggle(item)}
          title="No longer available for this project — click to remove"
          className="rounded border border-error/40 bg-error/10 px-2 py-0.5 font-mono text-[11px] text-error line-through transition-colors hover:bg-error/20 focus:outline-none focus:ring-1 focus:ring-white/20"
        >
          {item} ✕
        </button>
      ))}
    </div>
  );
}

// ---- Main section --------------------------------------------------

export interface AgentProfilesSectionProps {
  projectId: string;
  initialProfiles: AgentProfile[];
  initialSkills: SkillMeta[];
  initialMcpConfig: McpConfig;
}

export function AgentProfilesSection({
  projectId,
  initialProfiles,
  initialSkills,
  initialMcpConfig,
}: AgentProfilesSectionProps) {
  const t = useDict();
  const ps = t.projectSettings ?? ({} as NonNullable<(typeof t)["projectSettings"]>);
  const qc = useQueryClient();
  const scheduleReset = useResetTimer();

  const { data: profiles = initialProfiles } = useQuery({
    queryKey: queryKeys.agentProfiles.list(projectId),
    queryFn: () => listAgentProfiles(projectId),
    initialData: initialProfiles,
    staleTime: 30_000,
  });

  const { data: skills = initialSkills } = useQuery({
    queryKey: queryKeys.skills.list(projectId),
    queryFn: () => listSkills(projectId),
    initialData: initialSkills,
    staleTime: 30_000,
  });

  const { data: mcpConfig = initialMcpConfig } = useQuery({
    queryKey: queryKeys.mcp.get(projectId),
    queryFn: () => getMcpConfig(projectId),
    initialData: initialMcpConfig,
    staleTime: 30_000,
  });

  const [selectedName, setSelectedName] = useState<string | null>(profiles[0]?.name ?? null);
  const [isCreating, setIsCreating] = useState(false);
  const [draft, setDraft] = useState<ProfileDraft>(emptyDraft);
  // #14: consolidate saveError/timer into useSaveState. savedName is kept separate because it is name-based.
  const { saveError, setSaveError, setError } = useSaveState("Save failed");
  const [savedName, setSavedName] = useState<string | null>(null);

  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [lastLoaded, setLastLoaded] = useState<string | null>(null);
  const selectedProfile = profiles.find((p) => p.name === selectedName);
  if (selectedProfile && selectedProfile.name !== lastLoaded && !isCreating) {
    setLastLoaded(selectedProfile.name);
    setDraft(profileToDraft(selectedProfile));
  }

  const skillNames = skills.map((s) => s.name);
  const mcpServerNames = (mcpConfig.servers ?? []).map((s) => s.name);

  const saveMutation = useMutation({
    mutationFn: ({ name, profile }: { name: string; profile: AgentProfile }) =>
      putAgentProfile(projectId, name, profile),
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.agentProfiles.detail(projectId, data.name), data);
      qc.invalidateQueries({ queryKey: queryKeys.agentProfiles.list(projectId) });
      setSavedName(data.name);
      setSaveError(null);
      setIsCreating(false);
      setSelectedName(data.name);
      setLastLoaded(data.name);
      scheduleReset(() => setSavedName(null));
    },
    onError: (err) => {
      // #14: consolidate err instanceof Error check into setError
      setError(err);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (name: string) => deleteAgentProfile(projectId, name),
    onSuccess: (_, deletedName) => {
      qc.invalidateQueries({ queryKey: queryKeys.agentProfiles.list(projectId) });
      qc.removeQueries({ queryKey: queryKeys.agentProfiles.detail(projectId, deletedName) });
      const next = profiles.find((p) => p.name !== deletedName);
      setSelectedName(next?.name ?? null);
      setLastLoaded(null);
      if (next) {
        setDraft(profileToDraft(next));
      } else {
        setDraft(emptyDraft());
      }
    },
    onError: (err) => {
      // #14: consolidate err instanceof Error check into setError
      setError(err, "Delete failed");
    },
  });

  function handleSelectProfile(name: string) {
    setSelectedName(name);
    setIsCreating(false);
    setSaveError(null);
    setLastLoaded(null);
  }

  function handleStartCreate() {
    setIsCreating(true);
    setSelectedName(null);
    setLastLoaded(null);
    setDraft(emptyDraft());
    setSaveError(null);
  }

  function handleSave() {
    if (!draft.name.trim()) {
      setSaveError("Profile name is required");
      return;
    }
    const profile = draftToProfile(draft);
    saveMutation.mutate({ name: draft.name.trim(), profile });
  }

  function patchDraft(patch: Partial<ProfileDraft>) {
    setDraft((prev) => ({ ...prev, ...patch }));
  }

  const showPanel = isCreating || selectedName !== null;

  return (
    <>
      <section data-testid="agent-profiles-section" aria-labelledby="proj-section-profiles">
        <p
          id="proj-section-profiles"
          className="mb-1 text-[11px] font-medium uppercase tracking-[0.05em] text-on-surface-variant"
        >
          {ps.sections.agentProfiles}
        </p>
        <p className="mb-5 text-[12px] text-outline">{ps.sections.agentProfilesHint}</p>

        <div className="flex gap-5">
          {/* Profile list sidebar */}
          <div className="flex w-44 shrink-0 flex-col gap-0.5">
            {profiles.map((profile) => (
              <button
                key={profile.name}
                type="button"
                data-testid={`profile-list-item-${profile.name}`}
                onClick={() => handleSelectProfile(profile.name)}
                className={cn(
                  "flex items-start gap-2 rounded px-3 py-2 text-left transition-colors",
                  selectedName === profile.name && !isCreating
                    ? "bg-surface-container-high text-on-surface"
                    : "text-on-surface-variant hover:bg-surface-container-high/50 hover:text-on-surface",
                )}
              >
                <Icon name="smart_toy" className="mt-0.5 shrink-0 text-[13px]" />
                <div className="min-w-0">
                  <p className="truncate text-[13px] font-medium">{profile.name}</p>
                  <span className="data text-outline mt-0.5 inline-block">{profile.base_role}</span>
                </div>
              </button>
            ))}
            <button
              type="button"
              data-testid="new-profile-btn"
              onClick={handleStartCreate}
              className={cn(
                "flex items-center gap-2 rounded px-3 py-2 text-[12px] transition-colors",
                isCreating
                  ? "bg-surface-container-high text-[var(--color-light)]"
                  : "border border-dashed border-outline-variant/50 text-outline hover:border-outline hover:text-on-surface",
              )}
            >
              <Icon name="add" className="text-[13px]" />
              New Profile
            </button>
          </div>

          {/* Editor panel */}
          {showPanel ? (
            <div className="flex flex-1 flex-col gap-4">
              {/* Name + Base role */}
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label
                    htmlFor="profile-name"
                    className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                  >
                    Name{" "}
                    <span className="normal-case tracking-normal text-outline/60">
                      (kebab-case)
                    </span>
                  </label>
                  <input
                    id="profile-name"
                    data-testid="profile-name-input"
                    type="text"
                    value={draft.name}
                    onChange={(e) => patchDraft({ name: e.target.value })}
                    disabled={!isCreating}
                    placeholder="e.g. frontend-worker"
                    className={cn(inputMonoClass, !isCreating && "cursor-default opacity-60")}
                  />
                </div>
                <div>
                  <p className="mb-1.5 text-[11px] uppercase tracking-wider text-outline">
                    Base Role
                  </p>
                  <div className="flex gap-2">
                    {(["worker", "evaluator"] as const).map((role) => (
                      <button
                        key={role}
                        type="button"
                        onClick={() => patchDraft({ base_role: role })}
                        className={cn(
                          "flex-1 rounded border px-3 py-1.5 text-[13px] transition-colors focus:outline-none focus:ring-1 focus:ring-white/20",
                          draft.base_role === role
                            ? "border-[var(--color-light)]/40 bg-[var(--color-light)]/10 text-[var(--color-light)]"
                            : "border-outline-variant text-on-surface-variant hover:border-outline hover:text-on-surface",
                        )}
                      >
                        {role}
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              {/* Description */}
              <div>
                <label
                  htmlFor="profile-description"
                  className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                >
                  Description
                </label>
                <input
                  id="profile-description"
                  data-testid="profile-description-input"
                  type="text"
                  value={draft.description}
                  onChange={(e) => patchDraft({ description: e.target.value })}
                  placeholder="Short description of this profile's purpose"
                  className={inputClass}
                />
              </div>

              {/* Instructions */}
              <div>
                <label
                  htmlFor="profile-instructions"
                  className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                >
                  Instructions
                </label>
                <textarea
                  id="profile-instructions"
                  data-testid="profile-instructions-textarea"
                  value={draft.instructions}
                  onChange={(e) => patchDraft({ instructions: e.target.value })}
                  rows={6}
                  placeholder="Agent-specific instructions appended to the base system prompt…"
                  className={cn(textareaClass, "resize-y leading-relaxed")}
                />
              </div>

              {/* Skills multi-select */}
              <div>
                <p className="mb-1.5 text-[11px] uppercase tracking-wider text-outline">
                  Skills{" "}
                  <span className="normal-case tracking-normal text-outline/60">
                    (empty = all project skills)
                  </span>
                </p>
                <MultiSelect
                  label="Skills"
                  items={skillNames}
                  selected={draft.skills}
                  onChange={(next) => patchDraft({ skills: next })}
                  data-testid="profile-skills-multiselect"
                />
              </div>

              {/* MCP servers multi-select */}
              <div>
                <p className="mb-1.5 text-[11px] uppercase tracking-wider text-outline">
                  MCP Servers{" "}
                  <span className="normal-case tracking-normal text-outline/60">
                    (empty = all project servers)
                  </span>
                </p>
                <MultiSelect
                  label="MCP Servers"
                  items={mcpServerNames}
                  selected={draft.mcp_servers}
                  onChange={(next) => patchDraft({ mcp_servers: next })}
                  data-testid="profile-mcp-multiselect"
                />
              </div>

              {/* Actions */}
              <div
                className="flex items-center gap-3 border-t pt-3"
                style={{ borderColor: "var(--edge-shadow)" }}
              >
                {saveError && (
                  <span
                    data-testid="profile-save-error"
                    className="text-[12px]"
                    style={{ color: "var(--color-removed)" }}
                  >
                    {saveError}
                  </span>
                )}
                <Button
                  variant="primary"
                  data-testid="save-profile-btn"
                  onClick={handleSave}
                  disabled={saveMutation.isPending}
                >
                  <Icon
                    name={savedName === draft.name ? "check" : "save"}
                    className="text-[16px]"
                  />
                  {saveMutation.isPending
                    ? ps.saving
                    : savedName === draft.name
                      ? ps.saved
                      : "Save Profile"}
                </Button>
                {selectedName && !isCreating && (
                  <Button
                    variant="danger"
                    size="sm"
                    data-testid="delete-profile-btn"
                    onClick={() => setShowDeleteConfirm(true)}
                    disabled={deleteMutation.isPending}
                  >
                    <Icon name="delete" className="text-[14px]" />
                    {deleteMutation.isPending ? "Deleting…" : "Delete"}
                  </Button>
                )}
              </div>
            </div>
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center gap-2 py-10 text-outline">
              <Icon name="smart_toy" className="text-[32px] opacity-30" />
              <p className="text-[13px]">No profiles yet.</p>
            </div>
          )}
        </div>
      </section>

      {/* Delete confirmation dialog */}
      <Dialog open={showDeleteConfirm} onOpenChange={setShowDeleteConfirm}>
        <DialogContent title="Delete Profile">
          <p className="mb-4 text-body-sm text-on-surface-variant">
            Delete profile &ldquo;{selectedName}&rdquo;? This cannot be undone.
          </p>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setShowDeleteConfirm(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              disabled={deleteMutation.isPending}
              data-testid="confirm-delete-profile-btn"
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
