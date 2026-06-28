"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { toast } from "sonner";
import { getUsageSummary, type UsageSummaryResponse } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { formatCostCompact } from "@/lib/format-jpy";
import { useLocale, useT } from "@/lib/i18n/provider";
import { makeBudgetExceededHandler, useUsageStream } from "@/lib/sse/use-usage-stream";

interface CostTickerProps {
  /** Initial server-fetched usage data for hydration. */
  initialData: UsageSummaryResponse;
}

function usageColor(data: UsageSummaryResponse): string {
  const { budget } = data;
  if (budget.over_budget) return "text-error";
  if (budget.limit_usd != null) {
    const pct = budget.spent_usd / budget.limit_usd;
    if (pct >= 0.8) return "text-[var(--color-running)]";
  }
  return "text-on-surface-variant";
}

/**
 * Global cost ticker.
 * Receives global_totals via SSE and updates the TanStack Query cache in real time.
 */
export function CostTicker({ initialData }: CostTickerProps) {
  const t = useT();
  const locale = useLocale();
  const { data = initialData } = useQuery<UsageSummaryResponse>({
    queryKey: queryKeys.usage.summary(),
    queryFn: getUsageSummary,
    initialData,
    staleTime: 60_000,
  });

  // Subscribe to global usage SSE
  useUsageStream({
    onBudgetExceeded: makeBudgetExceededHandler((msg) => toast.error(msg, { duration: 6000 }), t),
  });

  const color = usageColor(data);
  const hasBudget = data.budget.limit_usd != null;

  const { day_ratio: dayRatio, month_ratio: monthRatio } = data.budget;

  // Monthly bar color (three levels: over_budget=error / >=80%=warning salmon / below=neutral)
  function monthBarColor(): string {
    if (data.budget.over_budget) return "bg-error";
    if (monthRatio != null && monthRatio >= 0.8) return "bg-error/60";
    return "bg-outline/40";
  }

  return (
    <div className="flex flex-col items-stretch gap-1 px-1 py-1">
      {/* Total cost link */}
      <Link
        href="/usage"
        className={cn(
          "flex items-center justify-center rounded px-1 py-1 font-mono tabular-nums transition-colors hover:bg-surface-container-high",
          color,
        )}
        style={{ fontSize: "12px", lineHeight: "16px", whiteSpace: "nowrap" }}
        title={`${formatCostCompact(data.total_cost_jpy, data.total_cost_usd, locale)} — Usage dashboard`}
      >
        {formatCostCompact(data.total_cost_jpy, data.total_cost_usd, locale)}
      </Link>

      {/* Day/month ratio bars (only when budget is set) */}
      {hasBudget && (
        <div className="flex flex-col gap-[3px]">
          {/* Daily ratio */}
          {dayRatio != null && (
            <div>
              <p
                className="font-mono tabular-nums text-outline"
                style={{ fontSize: "9px", lineHeight: "12px" }}
              >
                {t("usage.budget.dayShort")} {Math.round(dayRatio * 100)}%
              </p>
              <div
                role="progressbar"
                aria-valuenow={Math.round(dayRatio * 100)}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label={`${t("usage.budget.dayRatio")} ${Math.round(dayRatio * 100)}%`}
                className="h-[2px] overflow-hidden rounded-full bg-surface-container-highest"
                title={`${t("usage.budget.dayRatio")} ${Math.round(dayRatio * 100)}%`}
              >
                <div
                  className="h-full rounded-full bg-[var(--color-running)]"
                  style={{ width: `${Math.min(100, dayRatio * 100)}%` }}
                />
              </div>
            </div>
          )}

          {/* Monthly ratio */}
          {monthRatio != null && (
            <div>
              <p
                className={cn(
                  "font-mono tabular-nums",
                  data.budget.over_budget
                    ? "text-error"
                    : monthRatio >= 0.8
                      ? "text-error"
                      : "text-outline",
                )}
                style={{ fontSize: "9px", lineHeight: "12px" }}
              >
                {t("usage.budget.monthShort")} {Math.round(monthRatio * 100)}%
              </p>
              <div
                role="progressbar"
                aria-valuenow={Math.round(monthRatio * 100)}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label={`${t("usage.budget.monthRatio")} ${Math.round(monthRatio * 100)}%`}
                className="h-[2px] overflow-hidden rounded-full bg-surface-container-highest"
                title={`${t("usage.budget.monthRatio")} ${Math.round(monthRatio * 100)}%`}
              >
                <div
                  className={cn("h-full rounded-full", monthBarColor())}
                  style={{ width: `${Math.min(100, monthRatio * 100)}%` }}
                />
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
