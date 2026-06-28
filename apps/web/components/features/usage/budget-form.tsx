"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import type { BudgetSetRequest, UsageSummaryResponse } from "@/lib/api/endpoints";
import { setBudget } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { formatJpy, formatUsdBudget } from "@/lib/format-jpy";
import { useT } from "@/lib/i18n/provider";

// ---- Budget form ----

export function BudgetForm({ data }: { data: UsageSummaryResponse }) {
  const t = useT();
  const rate = data.exchange_rate.rate_jpy > 0 ? data.exchange_rate.rate_jpy : 0;

  const qc = useQueryClient();
  const [limitInput, setLimitInput] = useState(
    data.budget.limit_usd != null ? String(data.budget.limit_usd) : "",
  );

  const setBudgetMutation = useMutation({
    mutationFn: (body: BudgetSetRequest) => setBudget(body),
    onSuccess: () => {
      toast.success(t("usage.budget.saveSuccess"));
      qc.invalidateQueries({ queryKey: queryKeys.usage.summary() });
    },
    onError: () => toast.error(t("usage.budget.saveError")),
  });

  const handleSetBudget = () => {
    const parsed = limitInput.trim() === "" ? null : Number.parseFloat(limitInput);
    if (parsed !== null && (Number.isNaN(parsed) || parsed < 0)) {
      toast.error(t("usage.budget.invalidAmount"));
      return;
    }
    setBudgetMutation.mutate({ limit_usd: parsed });
  };

  const { budget } = data;
  const hasBudget = budget.limit_usd != null;

  // Monthly progress: use month_ratio if available, otherwise fall back to the legacy calculation
  const monthPct = hasBudget
    ? budget.month_ratio != null
      ? Math.min(100, budget.month_ratio * 100)
      : Math.min(100, (budget.spent_usd / (budget.limit_usd as number)) * 100)
    : 0;
  const monthBarColor = budget.over_budget
    ? "bg-error"
    : monthPct >= 80
      ? "bg-error/60"
      : "bg-[var(--color-running)]";

  // Daily progress
  const dayRatio = budget.day_ratio ?? null;
  const dayPct = dayRatio != null ? Math.min(100, dayRatio * 100) : 0;

  // JPY conversion hint: input value × rate (hidden when rate=0)
  const limitInputNum = Number.parseFloat(limitInput);
  const jpyHint =
    rate > 0 && limitInput.trim() !== "" && !Number.isNaN(limitInputNum) && limitInputNum >= 0
      ? formatJpy(Math.round(limitInputNum * rate))
      : null;

  const jpyHintText =
    jpyHint != null ? t("usage.budget.jpyHint").replace("{jpy}", jpyHint.replace("¥", "")) : null;

  return (
    <div className="px-6 py-5">
      <h3 className="mb-4 text-[13px] font-semibold uppercase tracking-[0.05em] text-on-surface-variant">
        {t("usage.budget.heading")}
      </h3>

      {/* Monthly progress bar */}
      {hasBudget && (
        <div className="mb-4">
          <div className="mb-1.5 flex items-center justify-between">
            <span className="data text-on-surface-variant">
              {formatUsdBudget(budget.spent_usd)}&thinsp;/&thinsp;
              {formatUsdBudget(budget.limit_usd as number)}
            </span>
            <span
              className={cn(
                "data",
                budget.over_budget ? "text-error" : monthPct >= 80 ? "text-error" : "text-outline",
              )}
            >
              {monthPct.toFixed(1)}%
            </span>
          </div>
          <div
            className="h-[3px] overflow-hidden rounded-full bg-surface-container-highest"
            title={`${t("usage.budget.monthRatio")} ${monthPct.toFixed(1)}%`}
          >
            <div
              className={cn("h-full rounded-full transition-all", monthBarColor)}
              style={{ width: `${monthPct}%` }}
            />
          </div>
          {budget.remaining_usd != null && !budget.over_budget && (
            <p className="data mt-1 text-outline">
              {t("usage.budget.remaining")}&thinsp;{formatUsdBudget(budget.remaining_usd)}
            </p>
          )}
          {budget.over_budget && (
            <p className="data mt-1 text-error">{t("usage.budget.overBudget")}</p>
          )}
        </div>
      )}

      {/* Daily budget sub-block */}
      {hasBudget && dayRatio != null && (
        <div className="mb-5">
          <div className="mb-1.5 flex items-center justify-between">
            <span className="data text-on-surface-variant">
              {t("usage.budget.dailyBudget")}
              {budget.daily_budget_usd != null && (
                <span className="ml-1 text-outline">
                  {formatUsdBudget(budget.daily_budget_usd)}
                </span>
              )}
            </span>
            <span className="data text-outline">
              {t("usage.budget.dailySpent")}&thinsp;{formatUsdBudget(budget.daily_spent_usd)}
            </span>
          </div>
          <div
            className="h-[3px] overflow-hidden rounded-full bg-surface-container-highest"
            title={`${t("usage.budget.dayRatio")} ${(dayRatio * 100).toFixed(1)}%`}
          >
            <div
              className="h-full rounded-full transition-all bg-[var(--color-running)]"
              style={{ width: `${dayPct}%` }}
            />
          </div>
          <p className="data mt-1 text-outline">
            {t("usage.budget.dayRatio")}&thinsp;{(dayRatio * 100).toFixed(1)}%
          </p>
        </div>
      )}

      {/* Budget input */}
      <div className="flex items-end gap-3">
        <div className="flex-1">
          <label htmlFor="budget-limit-input" className="label mb-1.5 block uppercase text-outline">
            {t("usage.budget.limitLabel")}
          </label>
          <div className="relative">
            <span className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-[13px] text-outline">
              $
            </span>
            <input
              id="budget-limit-input"
              type="number"
              min="0"
              step="0.01"
              placeholder={t("usage.budget.limitPlaceholder")}
              value={limitInput}
              onChange={(e) => setLimitInput(e.target.value)}
              className="w-full rounded border border-outline-variant bg-surface-container-lowest py-1.5 pl-6 pr-3 font-mono text-[13px] text-on-surface placeholder:text-outline focus:border-[var(--color-running)] focus:outline-none"
            />
          </div>
          {jpyHintText != null && (
            <p className="mt-1 font-mono text-[11px] text-outline">{jpyHintText}</p>
          )}
        </div>
        <Button
          variant="primary"
          size="sm"
          disabled={setBudgetMutation.isPending}
          onClick={handleSetBudget}
        >
          {setBudgetMutation.isPending ? t("usage.budget.saving") : t("usage.budget.save")}
        </Button>
      </div>
    </div>
  );
}
