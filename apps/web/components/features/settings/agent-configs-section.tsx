"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { textareaClass } from "@/components/features/settings/settings-primitives";
import { Icon } from "@/components/icon";
import { Button } from "@/components/ui/button";
import type { AgentConfig } from "@/lib/api/endpoints";
import { putAgentConfig } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { useResetTimer } from "@/lib/hooks/use-reset-timer";
import { useSaveState } from "@/lib/hooks/use-save-state";
import { useDict } from "@/lib/i18n/provider";

const ROLES = ["manager", "worker", "evaluator"] as const;
type Role = (typeof ROLES)[number];

interface AgentConfigsSectionProps {
  projectId: string;
  initialConfigs: AgentConfig[];
}

export function AgentConfigsSection({ projectId, initialConfigs }: AgentConfigsSectionProps) {
  const t = useDict();
  const ps = t.projectSettings ?? ({} as NonNullable<(typeof t)["projectSettings"]>);
  const qc = useQueryClient();
  const [activeRole, setActiveRole] = useState<Role>("worker");
  const [values, setValues] = useState<Record<Role, string>>(() => {
    const map: Partial<Record<Role, string>> = {};
    for (const cfg of initialConfigs) {
      map[cfg.role] = cfg.instructions;
    }
    return {
      manager: map.manager ?? "",
      worker: map.worker ?? "",
      evaluator: map.evaluator ?? "",
    };
  });
  const [savedRole, setSavedRole] = useState<Role | null>(null);
  // #14: consolidate saveError/setError into useSaveState. savedRole is kept separate because it is role-based.
  const { saveError, setSaveError, setError } = useSaveState(ps.save);
  const scheduleReset = useResetTimer();

  const mutation = useMutation({
    mutationFn: (role: Role) => putAgentConfig(projectId, role, values[role]),
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.agentConfigs.detail(projectId, data.role), data);
      qc.invalidateQueries({ queryKey: queryKeys.agentConfigs.list(projectId) });
      setSavedRole(data.role as Role);
      setSaveError(null);
      scheduleReset(() => setSavedRole(null));
    },
    onError: (err) => {
      setError(err, ps.save);
    },
  });

  const roleDescriptionKey: Record<Role, keyof (typeof ps)["roles"]> = {
    manager: "managerDescription",
    worker: "workerDescription",
    evaluator: "evaluatorDescription",
  };

  return (
    <section aria-labelledby="proj-section-agent-instructions">
      <p
        id="proj-section-agent-instructions"
        className="mb-1 text-[11px] font-medium uppercase tracking-[0.05em] text-on-surface-variant"
      >
        {ps.sections.agentInstructions}
      </p>
      <p className="mb-5 text-[12px] text-outline">{ps.sections.agentInstructionsHint}</p>

      {/* Role tabs */}
      <div className="mb-4 flex gap-0 border-b border-outline-variant/40">
        {ROLES.map((role) => {
          const active = activeRole === role;
          return (
            <button
              key={role}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => setActiveRole(role)}
              className={`flex items-center gap-1.5 border-b-2 px-4 py-2 text-[12px] uppercase tracking-wider transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-white/30 ${
                active
                  ? "border-white text-on-surface"
                  : "border-transparent text-outline hover:border-outline-variant hover:text-on-surface-variant"
              }`}
            >
              <Icon name="smart_toy" className="text-[14px]" />
              {ps.roles[role]}
            </button>
          );
        })}
      </div>

      {/* Active role panel */}
      <div className="space-y-3">
        <p className="text-[12px] text-outline">{ps.roles[roleDescriptionKey[activeRole]]}</p>
        <textarea
          data-testid={`agent-config-textarea-${activeRole}`}
          value={values[activeRole]}
          onChange={(e) => setValues((prev) => ({ ...prev, [activeRole]: e.target.value }))}
          rows={10}
          placeholder={`Project-specific instructions for the ${activeRole} agent…`}
          className={cn(textareaClass, "resize-y leading-relaxed")}
        />
        <div className="flex items-center gap-3">
          {saveError && (
            <span className="text-[12px]" style={{ color: "var(--color-removed)" }}>
              {saveError}
            </span>
          )}
          <Button
            variant="primary"
            data-testid={`save-agent-config-btn-${activeRole}`}
            onClick={() => mutation.mutate(activeRole)}
            disabled={mutation.isPending}
          >
            <Icon name={savedRole === activeRole ? "check" : "save"} className="text-[16px]" />
            {mutation.isPending
              ? ps.saving
              : savedRole === activeRole
                ? ps.saved
                : `${ps.save} ${ps.roles[activeRole]}`}
          </Button>
        </div>
      </div>
    </section>
  );
}
