"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Icon } from "@/components/icon";
import { Button } from "@/components/ui/button";
import type {
  ConversationSummarySettings,
  EmbeddingSettings,
  IndexerSettings,
  LLMRolesSettings,
  LLMSettings,
  Settings,
} from "@/lib/api/endpoints";
import { getSettings, putSettings } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { useSaveState } from "@/lib/hooks/use-save-state";
import { useDict } from "@/lib/i18n/provider";
import {
  Field,
  FieldHint,
  FieldLabel,
  inputClass,
  inputMonoClass,
  ProviderPills,
  SectionLabel,
  ToggleSwitch,
} from "./settings-primitives";

// ---- defaults -------------------------------------------------------

function defaultSettings(): Settings {
  return {
    workspace_root: "~/yukar-projects",
    llm: {
      provider: "bedrock",
      model_id: "us.anthropic.claude-sonnet-4-6-20251201-v1:0",
      max_tokens: 8192,
      prompt_caching: true,
      request_timeout: 900,
      summarization: {
        enabled: true,
        summary_ratio: 0.3,
        preserve_recent_messages: 10,
        proactive_compression_threshold: null,
      },
    },
    embedding: {
      provider: "bedrock",
      model_id: "amazon.titan-embed-text-v2:0",
      region: null,
      dimensions: null,
    },
    agent: {
      max_parallel_epics: 2,
      max_parallel_workers: 4,
      worker_max_turns: 60,
      evaluator_max_turns: 20,
      worker_max_total_tokens: null,
      evaluator_max_total_tokens: null,
    },
    git: { author_name: "yukar", author_email: "yukar@localhost" },
    indexer: { watch: true },
  };
}

// ---- types ----------------------------------------------------------

type LLMProvider = "bedrock" | "anthropic" | "fake";
type EmbeddingProvider = "bedrock" | "fake";

const LLM_PROVIDERS: { value: LLMProvider; label: string }[] = [
  { value: "bedrock", label: "AWS Bedrock" },
  { value: "anthropic", label: "Anthropic API" },
  { value: "fake", label: "Fake (testing)" },
];

const EMBEDDING_PROVIDERS: { value: EmbeddingProvider; label: string }[] = [
  { value: "bedrock", label: "AWS Bedrock" },
  { value: "fake", label: "Fake (testing)" },
];

// ---- component ------------------------------------------------------

interface SettingsFormClientProps {
  initialSettings: Settings | null;
}

export function SettingsFormClient({ initialSettings }: SettingsFormClientProps) {
  const t = useDict();
  const qc = useQueryClient();

  const { data: settingsData } = useQuery({
    queryKey: queryKeys.settings.get(),
    queryFn: getSettings,
    initialData: initialSettings ?? undefined,
  });

  // Initialize form once from server data; background refetches do NOT reset in-progress edits.
  const [form, setForm] = useState<Settings>(() => settingsData ?? defaultSettings());
  const [synced, setSynced] = useState(initialSettings != null);
  if (!synced && settingsData) {
    setSynced(true);
    setForm(settingsData);
  }

  // #14: consolidate saved/saveError/timer into useSaveState
  const { saved, saveError, setSaveError, clearSavedAfter2s, setError } = useSaveState(
    t.settings?.saveError ?? "Save failed",
  );

  const mutation = useMutation({
    mutationFn: () => putSettings(form),
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.settings.get(), data);
      setSaveError(null);
      clearSavedAfter2s();
    },
    onError: (err) => {
      setError(err, t.settings?.saveError ?? "Save failed");
    },
  });

  // derived values
  const llm: LLMSettings = form.llm ?? {
    provider: "bedrock",
    model_id: "",
    max_tokens: 8192,
    prompt_caching: true,
    request_timeout: 900,
  };
  const agent = form.agent ?? {
    max_parallel_epics: 2,
    max_parallel_workers: 4,
    worker_max_turns: 60,
    evaluator_max_turns: 20,
    worker_max_total_tokens: null,
    evaluator_max_total_tokens: null,
  };
  const roles: LLMRolesSettings = llm.roles ?? {};
  const summarization: ConversationSummarySettings = llm.summarization ?? {
    enabled: true,
    summary_ratio: 0.3,
    preserve_recent_messages: 10,
    proactive_compression_threshold: null,
  };
  const embedding: EmbeddingSettings = form.embedding ?? {
    provider: "bedrock",
    model_id: "amazon.titan-embed-text-v2:0",
    region: null,
    dimensions: null,
  };
  const indexer: IndexerSettings = form.indexer ?? { watch: true };

  const provider = llm.provider as LLMProvider;
  const embeddingProvider = embedding.provider as EmbeddingProvider;

  function setLlm(patch: Partial<LLMSettings>) {
    setForm((f) => ({ ...f, llm: { ...llm, ...patch } }));
  }

  function setSummarization(patch: Partial<ConversationSummarySettings>) {
    setLlm({ summarization: { ...summarization, ...patch } });
  }

  function setEmbedding(patch: Partial<EmbeddingSettings>) {
    setForm((f) => ({ ...f, embedding: { ...embedding, ...patch } }));
  }

  function setIndexer(patch: Partial<IndexerSettings>) {
    setForm((f) => ({ ...f, indexer: { ...indexer, ...patch } }));
  }

  function setRoleModelId(
    role: "manager" | "worker" | "evaluator" | "arbiter" | "reviewer",
    value: string,
  ) {
    const updated: LLMRolesSettings = { ...roles, [role]: { model_id: value || null } };
    setLlm({ roles: updated });
  }

  const st = t.settings ?? ({} as NonNullable<(typeof t)["settings"]>);

  return (
    <div className="w-full max-w-[860px] space-y-0 pb-[env(safe-area-inset-bottom,0px)]">
      {/* ── Workspace root ───────────────────────────────────── */}
      <div className="pb-8">
        <Field id="settings-workspace-root" label="Workspace Root">
          <input
            id="settings-workspace-root"
            type="text"
            value={form.workspace_root}
            onChange={(e) => setForm((f) => ({ ...f, workspace_root: e.target.value }))}
            className={inputClass}
          />
        </Field>
      </div>

      <div className="edge-h mb-8" aria-hidden />

      {/* ── Provider ─────────────────────────────────────────── */}
      <section aria-labelledby="section-provider" className="pb-8">
        <SectionLabel>
          <span id="section-provider">{st.sections.provider}</span>
        </SectionLabel>
        <div className="space-y-5">
          <Field label={st.provider.llmProvider}>
            <ProviderPills
              options={LLM_PROVIDERS}
              value={provider}
              onChange={(v) => setLlm({ provider: v })}
            />
          </Field>

          {provider === "anthropic" && (
            <Field
              id="settings-llm-api-key-env"
              label={st.provider.apiKeyEnv}
              hint={st.provider.apiKeyEnvHint}
            >
              <input
                id="settings-llm-api-key-env"
                type="text"
                value={llm.api_key_env ?? ""}
                onChange={(e) => setLlm({ api_key_env: e.target.value || null })}
                placeholder="ANTHROPIC_API_KEY"
                className={inputMonoClass}
              />
            </Field>
          )}
        </div>
      </section>

      <div className="edge-h mb-8" aria-hidden />

      {/* ── Model ────────────────────────────────────────────── */}
      <section aria-labelledby="section-model" className="pb-8">
        <SectionLabel>
          <span id="section-model">{st.sections.model}</span>
        </SectionLabel>
        <div className="space-y-5">
          <Field
            id="settings-llm-model-id"
            label={st.model.globalModel}
            hint={
              provider === "anthropic"
                ? "e.g. claude-sonnet-5, claude-fable-5, claude-sonnet-4-6"
                : provider === "fake"
                  ? "Any string (fake provider ignores model ID)"
                  : "e.g. us.anthropic.claude-sonnet-4-6-20251201-v1:0"
            }
          >
            <input
              id="settings-llm-model-id"
              type="text"
              value={llm.model_id}
              onChange={(e) => setLlm({ model_id: e.target.value })}
              className={inputMonoClass}
            />
          </Field>

          <Field id="settings-llm-max-tokens" label={st.model.maxTokens}>
            <input
              id="settings-llm-max-tokens"
              type="number"
              min={256}
              max={200000}
              value={llm.max_tokens ?? 8192}
              onChange={(e) => setLlm({ max_tokens: Number(e.target.value) })}
              className={inputClass}
            />
          </Field>

          <Field id="settings-llm-request-timeout" label={st.model.requestTimeout}>
            <input
              id="settings-llm-request-timeout"
              type="number"
              min={1}
              max={3600}
              value={llm.request_timeout ?? 900}
              onChange={(e) => setLlm({ request_timeout: Number(e.target.value) })}
              className={inputClass}
            />
            <FieldHint>{st.model.requestTimeoutHint}</FieldHint>
          </Field>

          <Field id="settings-llm-prompt-caching" label={st.model.promptCaching}>
            <ToggleSwitch
              id="settings-llm-prompt-caching"
              value={llm.prompt_caching ?? true}
              onChange={(v) => setLlm({ prompt_caching: v })}
              labelOn={st.model.promptCachingEnabled}
              labelOff={st.model.promptCachingDisabled}
            />
          </Field>

          {/* Role-based overrides */}
          <div>
            <FieldLabel>{st.model.roleOverrides}</FieldLabel>
            <FieldHint>{st.model.roleOverridesHint}</FieldHint>
            <div className="mt-3 grid grid-cols-1 gap-4 md:grid-cols-3">
              {(["manager", "worker", "evaluator", "arbiter", "reviewer"] as const).map((role) => (
                <div key={role}>
                  <label
                    htmlFor={`settings-llm-role-${role}`}
                    className="mb-1.5 block text-[11px] uppercase tracking-wider text-outline"
                  >
                    {role.charAt(0).toUpperCase() + role.slice(1)}
                  </label>
                  <input
                    id={`settings-llm-role-${role}`}
                    type="text"
                    value={roles[role]?.model_id ?? ""}
                    onChange={(e) => setRoleModelId(role, e.target.value)}
                    placeholder={`Inherit (${llm.model_id || "—"})`}
                    className={inputMonoClass}
                  />
                </div>
              ))}
            </div>
          </div>

          {/* Conversation summarization — recess surface */}
          <div className="rounded border border-outline-variant/40 bg-surface-container-lowest p-4">
            <p className="mb-4 text-[11px] uppercase tracking-wider text-outline">
              {st.model.summarization}
            </p>
            <div className="space-y-4">
              <Field id="settings-llm-summarization-enabled" label={st.model.summarizationEnabled}>
                <ToggleSwitch
                  id="settings-llm-summarization-enabled"
                  value={summarization.enabled}
                  onChange={(v) => setSummarization({ enabled: v })}
                  labelOn={st.model.promptCachingEnabled}
                  labelOff={st.model.promptCachingDisabled}
                />
              </Field>

              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <Field
                  id="settings-llm-summarization-ratio"
                  label={st.model.summaryRatio}
                  hint={st.model.summaryRatioHint}
                >
                  <input
                    id="settings-llm-summarization-ratio"
                    type="number"
                    min={0.1}
                    max={0.8}
                    step={0.05}
                    value={summarization.summary_ratio}
                    onChange={(e) => setSummarization({ summary_ratio: Number(e.target.value) })}
                    className={inputClass}
                  />
                </Field>

                <Field
                  id="settings-llm-summarization-preserve"
                  label={st.model.preserveRecent}
                  hint={st.model.preserveRecentHint}
                >
                  <input
                    id="settings-llm-summarization-preserve"
                    type="number"
                    min={1}
                    step={1}
                    value={summarization.preserve_recent_messages}
                    onChange={(e) =>
                      setSummarization({ preserve_recent_messages: Number(e.target.value) })
                    }
                    className={inputClass}
                  />
                </Field>
              </div>

              <Field
                id="settings-llm-summarization-threshold"
                label={st.model.proactiveThreshold}
                hint={st.model.proactiveThresholdHint}
              >
                <input
                  id="settings-llm-summarization-threshold"
                  type="number"
                  min={0}
                  max={1}
                  step={0.05}
                  value={summarization.proactive_compression_threshold ?? ""}
                  placeholder="—"
                  onChange={(e) =>
                    setSummarization({
                      proactive_compression_threshold:
                        e.target.value === "" ? null : Number(e.target.value),
                    })
                  }
                  className={inputClass}
                />
              </Field>
            </div>
          </div>
        </div>
      </section>

      <div className="edge-h mb-8" aria-hidden />

      {/* ── Embedding ────────────────────────────────────────── */}
      <section aria-labelledby="section-embedding" className="pb-8">
        <SectionLabel>
          <span id="section-embedding">{st.sections.embedding}</span>
        </SectionLabel>
        <div className="space-y-5">
          <Field label={st.embedding.provider}>
            <ProviderPills
              options={EMBEDDING_PROVIDERS}
              value={embeddingProvider}
              onChange={(v) => setEmbedding({ provider: v })}
            />
          </Field>

          <Field
            id="settings-embedding-model-id"
            label={st.embedding.modelId}
            hint="e.g. amazon.titan-embed-text-v2:0"
          >
            <input
              id="settings-embedding-model-id"
              type="text"
              value={embedding.model_id}
              onChange={(e) => setEmbedding({ model_id: e.target.value })}
              className={inputMonoClass}
            />
          </Field>

          <Field
            id="settings-embedding-region"
            label={st.embedding.region}
            hint={st.embedding.regionHint}
          >
            <input
              id="settings-embedding-region"
              type="text"
              value={embedding.region ?? ""}
              placeholder="ap-northeast-1"
              onChange={(e) => setEmbedding({ region: e.target.value || null })}
              className={inputMonoClass}
            />
          </Field>

          <Field
            id="settings-embedding-dimensions"
            label={st.embedding.dimensions}
            hint={st.embedding.dimensionsHint}
          >
            <input
              id="settings-embedding-dimensions"
              type="number"
              min={1}
              step={1}
              value={embedding.dimensions ?? ""}
              placeholder="—"
              onChange={(e) =>
                setEmbedding({
                  dimensions: e.target.value === "" ? null : Number(e.target.value),
                })
              }
              className={inputClass}
            />
          </Field>
        </div>
      </section>

      <div className="edge-h mb-8" aria-hidden />

      {/* ── Indexer ──────────────────────────────────────────── */}
      <section aria-labelledby="section-indexer" className="pb-8">
        <SectionLabel>
          <span id="section-indexer">{st.sections.indexer}</span>
        </SectionLabel>
        <Field id="settings-indexer-watch" label={st.indexer.watch}>
          <ToggleSwitch
            id="settings-indexer-watch"
            value={indexer.watch}
            onChange={(v) => setIndexer({ watch: v })}
            labelOn={st.indexer.watchEnabled}
            labelOff={st.indexer.watchDisabled}
          />
        </Field>
      </section>

      <div className="edge-h mb-8" aria-hidden />

      {/* ── Concurrency ──────────────────────────────────────── */}
      <section aria-labelledby="section-concurrency" className="pb-8">
        <SectionLabel>
          <span id="section-concurrency">{st.sections.concurrency}</span>
        </SectionLabel>
        <div className="space-y-5">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <Field id="settings-max-epics" label={st.concurrency.maxEpics}>
              <input
                id="settings-max-epics"
                type="number"
                min={1}
                max={8}
                value={agent.max_parallel_epics}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    agent: { ...agent, max_parallel_epics: Number(e.target.value) },
                  }))
                }
                className={inputClass}
              />
            </Field>
            <Field id="settings-max-workers" label={st.concurrency.maxWorkers}>
              <input
                id="settings-max-workers"
                type="number"
                min={1}
                max={16}
                value={agent.max_parallel_workers}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    agent: { ...agent, max_parallel_workers: Number(e.target.value) },
                  }))
                }
                className={inputClass}
              />
            </Field>
          </div>

          {/* Turn limits & token caps — cost safety valves */}
          <div className="rounded border border-outline-variant/40 bg-surface-container-lowest p-4">
            <p className="mb-4 text-[11px] uppercase tracking-wider text-outline">Agent limits</p>
            <div className="space-y-4">
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <Field
                  id="settings-worker-max-turns"
                  label={st.concurrency.workerMaxTurns}
                  hint={st.concurrency.workerMaxTurnsHint}
                >
                  <input
                    id="settings-worker-max-turns"
                    type="number"
                    min={1}
                    step={1}
                    value={agent.worker_max_turns}
                    onChange={(e) => {
                      const v = Number.parseInt(e.target.value, 10);
                      if (!Number.isNaN(v) && v >= 1) {
                        setForm((f) => ({
                          ...f,
                          agent: { ...agent, worker_max_turns: v },
                        }));
                      }
                    }}
                    className={inputClass}
                  />
                </Field>
                <Field
                  id="settings-evaluator-max-turns"
                  label={st.concurrency.evaluatorMaxTurns}
                  hint={st.concurrency.evaluatorMaxTurnsHint}
                >
                  <input
                    id="settings-evaluator-max-turns"
                    type="number"
                    min={1}
                    step={1}
                    value={agent.evaluator_max_turns}
                    onChange={(e) => {
                      const v = Number.parseInt(e.target.value, 10);
                      if (!Number.isNaN(v) && v >= 1) {
                        setForm((f) => ({
                          ...f,
                          agent: { ...agent, evaluator_max_turns: v },
                        }));
                      }
                    }}
                    className={inputClass}
                  />
                </Field>
              </div>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <Field
                  id="settings-worker-max-total-tokens"
                  label={st.concurrency.workerMaxTotalTokens}
                  hint={st.concurrency.workerMaxTotalTokensHint}
                >
                  <input
                    id="settings-worker-max-total-tokens"
                    type="number"
                    min={1}
                    step={1}
                    value={agent.worker_max_total_tokens ?? ""}
                    placeholder="—"
                    onChange={(e) =>
                      setForm((f) => ({
                        ...f,
                        agent: {
                          ...agent,
                          worker_max_total_tokens:
                            e.target.value === "" ? null : Number(e.target.value),
                        },
                      }))
                    }
                    className={inputClass}
                  />
                </Field>
                <Field
                  id="settings-evaluator-max-total-tokens"
                  label={st.concurrency.evaluatorMaxTotalTokens}
                  hint={st.concurrency.evaluatorMaxTotalTokensHint}
                >
                  <input
                    id="settings-evaluator-max-total-tokens"
                    type="number"
                    min={1}
                    step={1}
                    value={agent.evaluator_max_total_tokens ?? ""}
                    placeholder="—"
                    onChange={(e) =>
                      setForm((f) => ({
                        ...f,
                        agent: {
                          ...agent,
                          evaluator_max_total_tokens:
                            e.target.value === "" ? null : Number(e.target.value),
                        },
                      }))
                    }
                    className={inputClass}
                  />
                </Field>
              </div>
            </div>
          </div>
        </div>
      </section>

      <div className="edge-h mb-8" aria-hidden />

      {/* ── Git Author ───────────────────────────────────────── */}
      <section aria-labelledby="section-git" className="pb-8">
        <SectionLabel>
          <span id="section-git">{st.sections.gitAuthor}</span>
        </SectionLabel>
        <div className="space-y-5">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <Field id="settings-git-name" label={st.gitAuthor.name}>
              <input
                id="settings-git-name"
                type="text"
                value={form.git?.author_name ?? "yukar"}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    git: {
                      ...f.git,
                      author_name: e.target.value,
                      author_email: f.git?.author_email ?? "yukar@localhost",
                    },
                  }))
                }
                className={inputClass}
              />
            </Field>
            <Field id="settings-git-email" label={st.gitAuthor.email}>
              <input
                id="settings-git-email"
                type="email"
                value={form.git?.author_email ?? "yukar@localhost"}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    git: {
                      ...f.git,
                      author_email: e.target.value,
                      author_name: f.git?.author_name ?? "yukar",
                    },
                  }))
                }
                className={inputClass}
              />
            </Field>
          </div>
          <p className="text-[12px] text-outline">{st.gitAuthor.note}</p>
        </div>
      </section>

      <div className="edge-h mb-8" aria-hidden />

      {/* ── Save action ──────────────────────────────────────── */}
      <div className="flex items-center gap-4 pb-20 md:pb-16">
        <Button variant="primary" onClick={() => mutation.mutate()} disabled={mutation.isPending}>
          <Icon name={saved ? "check" : "save"} className="text-[16px]" />
          {mutation.isPending ? st.saving : saved ? st.saved : st.save}
        </Button>
        {saveError && (
          <span className="text-[13px]" style={{ color: "var(--color-removed)" }}>
            {saveError}
          </span>
        )}
      </div>
    </div>
  );
}
