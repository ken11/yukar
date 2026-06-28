"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  inputClass,
  inputMonoClass,
  textareaClass,
} from "@/components/features/settings/settings-primitives";
import { Icon } from "@/components/icon";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import type { McpConfig, McpServerConfig } from "@/lib/api/endpoints";
import { putMcpConfig } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { useSaveState } from "@/lib/hooks/use-save-state";
import { useDict } from "@/lib/i18n/provider";
import { arrayToLines, linesToArray } from "@/lib/text";

// ---- helpers -------------------------------------------------------

function envToText(env: Record<string, string>): string {
  return Object.entries(env)
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");
}

function textToEnv(text: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const idx = line.indexOf("=");
    if (idx < 1) continue;
    const key = line.slice(0, idx).trim();
    const val = line.slice(idx + 1);
    if (key) result[key] = val;
  }
  return result;
}

function toolsToText(tools: string[] | null | undefined): string {
  return (tools ?? []).join(", ");
}

function textToTools(text: string): string[] | null {
  const items = text
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean);
  return items.length > 0 ? items : null;
}

// ---- Editable state for a single server ----------------------------

interface ServerDraft {
  name: string;
  type: "stdio" | "sse";
  url: string;
  command: string;
  argsText: string;
  envText: string;
  allowedText: string;
  rejectedText: string;
}

function serverToDraft(s: McpServerConfig): ServerDraft {
  return {
    name: s.name,
    type: s.type,
    url: s.url ?? "",
    command: s.command ?? "",
    argsText: arrayToLines(s.args ?? []),
    envText: envToText(s.env ?? {}),
    allowedText: toolsToText(s.allowed_tools),
    rejectedText: toolsToText(s.rejected_tools),
  };
}

function draftToServer(d: ServerDraft): McpServerConfig {
  return {
    name: d.name,
    type: d.type,
    url: d.url.trim() || null,
    command: d.command.trim() || null,
    args: linesToArray(d.argsText),
    env: textToEnv(d.envText),
    allowed_tools: textToTools(d.allowedText),
    rejected_tools: textToTools(d.rejectedText),
  };
}

function emptyDraft(): ServerDraft {
  return {
    name: "",
    type: "stdio",
    url: "",
    command: "",
    argsText: "",
    envText: "",
    allowedText: "",
    rejectedText: "",
  };
}

// ---- component -----------------------------------------------------

interface McpSectionProps {
  projectId: string;
  initialConfig: McpConfig;
}

export function McpSection({ projectId, initialConfig }: McpSectionProps) {
  const t = useDict();
  const ps = t.projectSettings ?? ({} as NonNullable<(typeof t)["projectSettings"]>);
  const qc = useQueryClient();
  const [drafts, setDrafts] = useState<ServerDraft[]>(() =>
    (initialConfig.servers ?? []).map(serverToDraft),
  );
  const [selectedIdx, setSelectedIdx] = useState<number | null>(
    (initialConfig.servers ?? []).length > 0 ? 0 : null,
  );
  const [showRemoveConfirm, setShowRemoveConfirm] = useState(false);
  // #14: consolidate saved/saveError/timer into useSaveState
  const { saved, saveError, setSaveError, clearSavedAfter2s, setError } = useSaveState();

  const mutation = useMutation({
    mutationFn: (config: McpConfig) => putMcpConfig(projectId, config),
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.mcp.get(projectId), data);
      setSaveError(null);
      clearSavedAfter2s();
    },
    onError: (err) => {
      setError(err);
    },
  });

  function patchDraft(idx: number, patch: Partial<ServerDraft>) {
    setDrafts((prev) => prev.map((d, i) => (i === idx ? { ...d, ...patch } : d)));
  }

  function addServer() {
    const next = [...drafts, emptyDraft()];
    setDrafts(next);
    setSelectedIdx(next.length - 1);
  }

  function removeServer(idx: number) {
    const next = drafts.filter((_, i) => i !== idx);
    setDrafts(next);
    if (selectedIdx === idx) {
      setSelectedIdx(next.length > 0 ? Math.min(idx, next.length - 1) : null);
    } else if (selectedIdx !== null && selectedIdx > idx) {
      setSelectedIdx(selectedIdx - 1);
    }
  }

  function handleSave() {
    const servers = drafts.map(draftToServer);
    mutation.mutate({ servers });
  }

  const selected = selectedIdx !== null ? (drafts[selectedIdx] ?? null) : null;

  return (
    <>
      <section aria-labelledby="proj-section-mcp">
        <p
          id="proj-section-mcp"
          className="mb-1 text-[11px] font-medium uppercase tracking-[0.05em] text-on-surface-variant"
        >
          {ps.sections.mcp}
        </p>
        <p className="mb-5 text-[12px] text-outline">{ps.sections.mcpHint}</p>

        <div className="flex gap-5">
          {/* Server list */}
          <div className="flex w-44 shrink-0 flex-col gap-0.5">
            {drafts.map((d, idx) => (
              <button
                key={`server-${
                  // biome-ignore lint/suspicious/noArrayIndexKey: stable positional index for MCP servers
                  idx
                }`}
                type="button"
                data-testid={d.name ? `mcp-server-list-item-${d.name}` : undefined}
                onClick={() => setSelectedIdx(idx)}
                className={cn(
                  "flex items-center gap-2 rounded px-3 py-2 text-left text-[13px] transition-colors",
                  selectedIdx === idx
                    ? "bg-surface-container-high text-on-surface"
                    : "text-on-surface-variant hover:bg-surface-container-high/50 hover:text-on-surface",
                )}
              >
                <Icon
                  name={d.type === "sse" ? "webhook" : "terminal"}
                  className="shrink-0 text-[13px]"
                />
                <span className="truncate">
                  {d.name || <em className="opacity-50">unnamed</em>}
                </span>
              </button>
            ))}
            <button
              type="button"
              data-testid="add-mcp-server-btn"
              onClick={addServer}
              className="flex items-center gap-2 rounded border border-dashed border-outline-variant/50 px-3 py-2 text-[12px] text-outline transition-colors hover:border-outline hover:text-on-surface"
            >
              <Icon name="add" className="text-[13px]" />
              Add Server
            </button>
          </div>

          {/* Editor panel */}
          <div className="flex-1">
            {selected !== null && selectedIdx !== null ? (
              <div className="space-y-4">
                {/* Name + Type */}
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label
                      htmlFor={`mcp-name-${selectedIdx}`}
                      className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                    >
                      Server Name
                    </label>
                    <input
                      id={`mcp-name-${selectedIdx}`}
                      data-testid="mcp-server-name-input"
                      type="text"
                      value={selected.name}
                      onChange={(e) => patchDraft(selectedIdx, { name: e.target.value })}
                      placeholder="e.g. github"
                      className={inputClass}
                    />
                  </div>
                  <div>
                    <p className="mb-1.5 text-[11px] uppercase tracking-wider text-outline">Type</p>
                    <div className="flex gap-2">
                      {(["stdio", "sse"] as const).map((tp) => (
                        <button
                          key={tp}
                          type="button"
                          onClick={() => patchDraft(selectedIdx, { type: tp })}
                          className={`flex-1 rounded border px-3 py-1.5 text-[13px] transition-colors focus:outline-none focus:ring-1 focus:ring-white/20 ${
                            selected.type === tp
                              ? "border-[var(--color-light)]/40 bg-[var(--color-light)]/10 text-[var(--color-light)]"
                              : "border-outline-variant text-on-surface-variant hover:border-outline hover:text-on-surface"
                          }`}
                        >
                          {tp}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>

                {/* stdio fields */}
                {selected.type === "stdio" && (
                  <>
                    <div>
                      <label
                        htmlFor={`mcp-command-${selectedIdx}`}
                        className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                      >
                        Command
                      </label>
                      <input
                        id={`mcp-command-${selectedIdx}`}
                        type="text"
                        value={selected.command}
                        onChange={(e) => patchDraft(selectedIdx, { command: e.target.value })}
                        placeholder="e.g. npx"
                        className={inputMonoClass}
                      />
                    </div>
                    <div>
                      <label
                        htmlFor={`mcp-args-${selectedIdx}`}
                        className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                      >
                        Args{" "}
                        <span className="normal-case tracking-normal text-outline/60">
                          (one per line)
                        </span>
                      </label>
                      <textarea
                        id={`mcp-args-${selectedIdx}`}
                        rows={3}
                        value={selected.argsText}
                        onChange={(e) => patchDraft(selectedIdx, { argsText: e.target.value })}
                        placeholder={"-y\n@modelcontextprotocol/server-github"}
                        className={textareaClass}
                      />
                    </div>
                  </>
                )}

                {/* sse fields */}
                {selected.type === "sse" && (
                  <div>
                    <label
                      htmlFor={`mcp-url-${selectedIdx}`}
                      className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                    >
                      URL
                    </label>
                    <input
                      id={`mcp-url-${selectedIdx}`}
                      type="text"
                      value={selected.url}
                      onChange={(e) => patchDraft(selectedIdx, { url: e.target.value })}
                      placeholder="https://mcp.example.com/sse"
                      className={inputMonoClass}
                    />
                  </div>
                )}

                {/* Env */}
                <div>
                  <label
                    htmlFor={`mcp-env-${selectedIdx}`}
                    className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                  >
                    Environment Variables
                  </label>
                  <p className="mb-1.5 text-[11px] text-outline">{ps.mcp.envHint}</p>
                  <textarea
                    id={`mcp-env-${selectedIdx}`}
                    rows={3}
                    value={selected.envText}
                    onChange={(e) => patchDraft(selectedIdx, { envText: e.target.value })}
                    // biome-ignore lint/suspicious/noTemplateCurlyInString: intentional user-facing syntax example
                    placeholder={"GITHUB_TOKEN=${GITHUB_TOKEN}"}
                    className={textareaClass}
                  />
                </div>

                {/* Tool allow/reject */}
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label
                      htmlFor={`mcp-allowed-${selectedIdx}`}
                      className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                    >
                      Allowed Tools{" "}
                      <span className="normal-case tracking-normal text-outline/60">
                        (optional)
                      </span>
                    </label>
                    <textarea
                      id={`mcp-allowed-${selectedIdx}`}
                      rows={2}
                      value={selected.allowedText}
                      onChange={(e) => patchDraft(selectedIdx, { allowedText: e.target.value })}
                      placeholder="list_issues, create_issue"
                      className={textareaClass}
                    />
                    <p className="mt-1 text-[11px] text-outline">
                      Comma-separated. Empty = allow all.
                    </p>
                  </div>
                  <div>
                    <label
                      htmlFor={`mcp-rejected-${selectedIdx}`}
                      className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                    >
                      Rejected Tools{" "}
                      <span className="normal-case tracking-normal text-outline/60">
                        (optional)
                      </span>
                    </label>
                    <textarea
                      id={`mcp-rejected-${selectedIdx}`}
                      rows={2}
                      value={selected.rejectedText}
                      onChange={(e) => patchDraft(selectedIdx, { rejectedText: e.target.value })}
                      placeholder="delete_repo"
                      className={textareaClass}
                    />
                    <p className="mt-1 text-[11px] text-outline">Takes priority over allowed.</p>
                  </div>
                </div>

                {/* Remove server — warm/destructive, quiet but clear */}
                <div className="border-t pt-3" style={{ borderColor: "var(--edge-shadow)" }}>
                  <Button
                    variant="danger"
                    size="sm"
                    onClick={() => {
                      setShowRemoveConfirm(true);
                    }}
                  >
                    <Icon name="delete" className="text-[14px]" />
                    Remove Server
                  </Button>
                </div>
              </div>
            ) : (
              <div className="flex flex-col items-center justify-center gap-2 py-10 text-outline">
                <Icon name="webhook" className="text-[32px] opacity-30" />
                <p className="text-[13px]">
                  {drafts.length === 0 ? "No MCP servers configured." : "Select a server to edit."}
                </p>
              </div>
            )}
          </div>
        </div>

        {/* Save all */}
        <div
          className="mt-6 flex items-center gap-3 border-t pt-4"
          style={{ borderColor: "var(--edge-shadow)" }}
        >
          {saveError && (
            <span className="text-[12px]" style={{ color: "var(--color-removed)" }}>
              {saveError}
            </span>
          )}
          <Button
            variant="primary"
            data-testid="save-mcp-btn"
            onClick={handleSave}
            disabled={mutation.isPending}
          >
            <Icon name={saved ? "check" : "save"} className="text-[16px]" />
            {mutation.isPending ? ps.saving : saved ? ps.saved : ps.save}
          </Button>
          <span className="text-[11px] text-outline">{ps.mcp.savedAll}</span>
        </div>
      </section>

      {/* Remove server confirmation dialog */}
      <Dialog open={showRemoveConfirm} onOpenChange={setShowRemoveConfirm}>
        <DialogContent title="Remove Server">
          <p className="mb-4 text-body-sm text-on-surface-variant">
            Remove server &ldquo;{selected?.name || "this server"}&rdquo;? This cannot be undone.
          </p>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setShowRemoveConfirm(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              data-testid="confirm-remove-server-btn"
              onClick={() => {
                if (selectedIdx !== null) {
                  removeServer(selectedIdx);
                }
                setShowRemoveConfirm(false);
              }}
            >
              Remove
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
