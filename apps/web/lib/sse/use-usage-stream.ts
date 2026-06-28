"use client";

import { useQueryClient } from "@tanstack/react-query";
import type { components } from "@yukar/api-types";
import { useCallback, useRef } from "react";
import type { UsageSummaryResponse } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { playChime } from "@/lib/audio/chime";
import { formatUsdBudget } from "@/lib/format-jpy";
import { useEventStream } from "./use-event-stream";

// ---------- SSE event shapes (not in OpenAPI schema — type manually) ----------

export interface TokenDelta {
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
  embedding: number;
}

export interface UsageTotals {
  cost_usd: number;
  cost_jpy: number;
  budget_limit_usd?: number | null;
  budget_remaining_usd?: number | null;
  month_spent_usd?: number;
  day_spent_usd?: number;
  daily_budget_usd?: number | null;
  days_in_month?: number;
  month_ratio?: number | null;
  day_ratio?: number | null;
  over_budget?: boolean;
}

export interface TokenUsageEvent {
  type: "token_usage";
  project_id: string;
  epic_id: string;
  run_id: string;
  ts: string;
  role: string;
  model_id: string;
  delta: TokenDelta;
  run_totals: UsageTotals & {
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens: number;
    cache_write_tokens: number;
    embedding_tokens: number;
  };
  global_totals: UsageTotals;
}

// BudgetExceededEvent uses the OpenAPI-generated type
export type BudgetExceededEvent = components["schemas"]["BudgetExceededEvent"];

export type UsageStreamEvent = TokenUsageEvent | BudgetExceededEvent;

/** Called when a budget_exceeded event is received. */
export type BudgetExceededHandler = (ev: BudgetExceededEvent) => void;

interface UseUsageStreamOptions {
  /** Called when budget_exceeded fires — caller is responsible for toast/chime. */
  onBudgetExceeded?: BudgetExceededHandler;
  /** Debounce interval for `GET /api/usage` invalidation (ms). Default 2000. */
  invalidateDebounceMs?: number;
}

/**
 * Subscribes to GET /api/usage/stream and patches the TanStack Query usage.summary cache.
 *
 * - token_usage: immediately patches budget and cost with global_totals.
 *   Fine-grained breakdown updates are invalidated and re-fetched after debounce.
 * - budget_exceeded: forwarded to the onBudgetExceeded callback.
 */
export function useUsageStream({
  onBudgetExceeded,
  invalidateDebounceMs = 2000,
}: UseUsageStreamOptions = {}): void {
  const qc = useQueryClient();
  const onBudgetExceededRef = useRef(onBudgetExceeded);
  onBudgetExceededRef.current = onBudgetExceeded;

  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const scheduleInvalidate = useCallback(() => {
    if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
    debounceTimerRef.current = setTimeout(() => {
      qc.invalidateQueries({ queryKey: queryKeys.usage.summary() });
    }, invalidateDebounceMs);
  }, [qc, invalidateDebounceMs]);

  useEventStream<UsageStreamEvent>({
    url: "/api/usage/stream",
    onMessage: ({ type, data }) => {
      if (!data || typeof data !== "object") return;

      if (type === "token_usage") {
        const ev = data as TokenUsageEvent;
        // Patch global totals directly in cache for instant display
        qc.setQueryData<UsageSummaryResponse>(queryKeys.usage.summary(), (prev) => {
          if (!prev) return prev;
          return {
            ...prev,
            total_cost_usd: ev.global_totals.cost_usd,
            total_cost_jpy: ev.global_totals.cost_jpy,
            budget: {
              ...prev.budget,
              spent_usd: ev.global_totals.month_spent_usd ?? prev.budget.spent_usd,
              limit_usd: ev.global_totals.budget_limit_usd ?? prev.budget.limit_usd,
              remaining_usd:
                ev.global_totals.budget_remaining_usd !== undefined
                  ? ev.global_totals.budget_remaining_usd
                  : prev.budget.remaining_usd,
              daily_budget_usd:
                ev.global_totals.daily_budget_usd !== undefined
                  ? ev.global_totals.daily_budget_usd
                  : prev.budget.daily_budget_usd,
              daily_spent_usd: ev.global_totals.day_spent_usd ?? prev.budget.daily_spent_usd,
              days_in_month: ev.global_totals.days_in_month ?? prev.budget.days_in_month,
              month_ratio:
                ev.global_totals.month_ratio !== undefined
                  ? ev.global_totals.month_ratio
                  : prev.budget.month_ratio,
              day_ratio:
                ev.global_totals.day_ratio !== undefined
                  ? ev.global_totals.day_ratio
                  : prev.budget.day_ratio,
              over_budget:
                ev.global_totals.over_budget !== undefined
                  ? ev.global_totals.over_budget
                  : (ev.global_totals.budget_limit_usd ?? null) !== null &&
                    (ev.global_totals.month_spent_usd ?? prev.budget.spent_usd) >=
                      (ev.global_totals.budget_limit_usd ?? Number.POSITIVE_INFINITY),
            },
          };
        });
        // Schedule full refresh for detailed breakdown
        scheduleInvalidate();
      } else if (type === "budget_exceeded") {
        const ev = data as BudgetExceededEvent;
        onBudgetExceededRef.current?.(ev);
        // Force immediate full refresh after budget exceeded
        qc.invalidateQueries({ queryKey: queryKeys.usage.summary() });
      }
    },
  });
}

/**
 * Default implementation that plays a toast/chime as the budget_exceeded handler for useUsageStream.
 * Intended to be passed to the Topbar.
 */
export function makeBudgetExceededHandler(
  onToast: (msg: string) => void,
  t: (key: string) => string,
): BudgetExceededHandler {
  return (ev: BudgetExceededEvent) => {
    const limit = formatUsdBudget(ev.limit_usd);
    onToast(t("usage.budget.budgetExceeded").replace("{limit}", limit));
    playChime("error");
  };
}
