"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import type { UsageSummaryResponse } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { formatCost, formatTokens } from "@/lib/format-jpy";
import { useResetTimer } from "@/lib/hooks/use-reset-timer";
import { useLocale } from "@/lib/i18n/provider";
import { useEventStream } from "@/lib/sse/use-event-stream";
import type { TokenUsageEvent } from "@/lib/sse/use-usage-stream";

interface RunCostState {
  costJpy: number;
  costUsd: number;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  embeddingTokens: number;
}

interface RunCostBadgeProps {
  projectId: string;
  epicId: string;
  runId: string;
  /** Whether the run is active (SSE should be subscribed) */
  enabled: boolean;
  /** Initial cost from server-side fetch (optional) */
  initialCostJpy?: number;
  initialCostUsd?: number;
}

/**
 * Badge that shows cost and token count per run in real time.
 * Receives token_usage events from the existing epic SSE (/run/events) and updates accordingly.
 *
 * Placement: understated single-line inline badge.
 */
export function RunCostBadge({
  projectId,
  epicId,
  runId,
  enabled,
  initialCostJpy = 0,
  initialCostUsd = 0,
}: RunCostBadgeProps) {
  const qc = useQueryClient();
  const locale = useLocale();
  const scheduleReset = useResetTimer();

  const [state, setState] = useState<RunCostState>({
    costJpy: initialCostJpy,
    costUsd: initialCostUsd,
    inputTokens: 0,
    outputTokens: 0,
    cacheReadTokens: 0,
    cacheWriteTokens: 0,
    embeddingTokens: 0,
  });

  const url = enabled ? `/api/projects/${projectId}/epics/${epicId}/run/events` : null;

  const scheduleInvalidate = useCallback(() => {
    scheduleReset(() => {
      qc.invalidateQueries({ queryKey: queryKeys.usage.summary() });
    });
  }, [scheduleReset, qc]);

  useEventStream<TokenUsageEvent>({
    url,
    onMessage: ({ type, data }) => {
      if (type !== "token_usage" || !data || typeof data !== "object") return;
      const ev = data as TokenUsageEvent;
      if (ev.run_id !== runId) return;

      setState({
        costJpy: ev.run_totals.cost_jpy,
        costUsd: ev.run_totals.cost_usd,
        inputTokens: ev.run_totals.input_tokens,
        outputTokens: ev.run_totals.output_tokens,
        cacheReadTokens: ev.run_totals.cache_read_tokens,
        cacheWriteTokens: ev.run_totals.cache_write_tokens,
        embeddingTokens: ev.run_totals.embedding_tokens,
      });

      // Also patch global usage cache
      qc.setQueryData<UsageSummaryResponse>(queryKeys.usage.summary(), (prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          total_cost_usd: ev.global_totals.cost_usd,
          total_cost_jpy: ev.global_totals.cost_jpy,
          budget: {
            ...prev.budget,
            spent_usd: ev.global_totals.cost_usd,
          },
        };
      });
      scheduleInvalidate();
    },
  });

  const totalTokens =
    state.inputTokens +
    state.outputTokens +
    state.cacheReadTokens +
    state.cacheWriteTokens +
    state.embeddingTokens;

  if (state.costJpy === 0 && totalTokens === 0) return null;

  return (
    <span
      className={cn(
        "inline-flex items-center gap-2 rounded border border-outline-variant/50 bg-surface-container px-2 py-0.5 font-mono text-[11px] text-on-surface-variant",
      )}
      title={`${formatCost(state.costJpy, state.costUsd, locale)} — Input: ${formatTokens(state.inputTokens)} / Output: ${formatTokens(state.outputTokens)} / Cache: ${formatTokens(state.cacheReadTokens + state.cacheWriteTokens)} / Embed: ${formatTokens(state.embeddingTokens)}`}
    >
      <span style={{ color: "var(--color-light)" }}>
        {formatCost(state.costJpy, state.costUsd, locale)}
      </span>
      <span className="text-outline">{formatTokens(totalTokens)} tok</span>
    </span>
  );
}
