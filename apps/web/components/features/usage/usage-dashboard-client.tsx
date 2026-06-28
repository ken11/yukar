"use client";

import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import type { UsagePeriodTotals, UsageSummaryResponse } from "@/lib/api/endpoints";
import { getUsageSummary } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { cn } from "@/lib/cn";
import { formatCost, formatCostCompact, formatTokens } from "@/lib/format-jpy";
import type { Locale } from "@/lib/i18n/dictionary";
import { useLocale, useT } from "@/lib/i18n/provider";
import { makeBudgetExceededHandler, useUsageStream } from "@/lib/sse/use-usage-stream";
import { BreakdownTable, ModelTable } from "./breakdown-tables";
import { BudgetForm } from "./budget-form";
import { DailyUsageChart } from "./daily-usage-chart";

interface UsageDashboardClientProps {
  initialData: UsageSummaryResponse;
}

// ---- Period metric block (no card, tonal hairline) ----

interface PeriodBlockProps {
  label: string;
  totals: UsagePeriodTotals | undefined;
  cacheLabel: string;
  locale: Locale;
}

function PeriodBlock({ label, totals, cacheLabel, locale }: PeriodBlockProps) {
  const costJpy = totals?.cost_jpy ?? 0;
  const costUsd = totals?.cost_usd ?? 0;
  const totalTokens = totals?.total_tokens ?? 0;
  const cacheRead = totals?.cache_read_tokens ?? 0;
  const cacheWrite = totals?.cache_write_tokens ?? 0;

  return (
    <div className="flex min-w-0 flex-col gap-1.5 py-4 pl-6 pr-8">
      <p className="label uppercase text-outline">{label}</p>
      <div className="flex items-baseline gap-3">
        <span className="font-mono text-[22px] font-semibold tabular-nums leading-tight text-on-surface">
          {formatCost(costJpy, costUsd, locale)}
        </span>
        {locale === "ja" && (
          <span className="data text-outline">{formatCostCompact(costJpy, costUsd, "en")}</span>
        )}
      </div>
      <div className="data mt-0.5 flex items-center gap-3 text-on-surface-variant">
        <span>{formatTokens(totalTokens)}&thinsp;tok</span>
        <span className="text-outline">·</span>
        <span>
          {cacheLabel}&thinsp;{formatTokens(cacheRead)}/{formatTokens(cacheWrite)}
        </span>
      </div>
    </div>
  );
}

// ---- Section wrapper (tonal hairline, no card) ----

function Section({
  heading,
  children,
  className,
}: {
  heading?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn("mb-6 bg-surface-container-high", className)}
      style={{
        boxShadow:
          "0 1px 0 0 var(--edge-shadow, rgba(0,0,0,0.28)), inset 0 -1px 0 0 var(--edge-lit, rgba(255,255,255,0.05))",
        borderRadius: "var(--radius-container, 8px)",
      }}
    >
      {heading && (
        <div
          className="px-6 pb-3 pt-5"
          style={{
            boxShadow:
              "0 1px 0 0 color-mix(in oklab, var(--color-outline-variant, #444748) 40%, transparent)",
          }}
        >
          <h3 className="text-[13px] font-semibold uppercase tracking-[0.05em] text-on-surface-variant">
            {heading}
          </h3>
        </div>
      )}
      {children}
    </div>
  );
}

// ---- Exported component ----

export function UsageDashboardClient({ initialData }: UsageDashboardClientProps) {
  const t = useT();
  const locale = useLocale();
  const { data = initialData } = useQuery<UsageSummaryResponse>({
    queryKey: queryKeys.usage.summary(),
    queryFn: getUsageSummary,
    initialData,
    staleTime: 60_000,
  });

  useUsageStream({
    onBudgetExceeded: makeBudgetExceededHandler((msg) => toast.error(msg, { duration: 6000 }), t),
  });

  const rateSource = data.exchange_rate.source;
  const rateSourceLabel =
    rateSource === "api"
      ? t("usage.exchangeRate.live")
      : rateSource === "cache"
        ? t("usage.exchangeRate.cached")
        : t("usage.exchangeRate.fallback");

  const fetchedAt = data.exchange_rate.fetched_at
    ? new Date(data.exchange_rate.fetched_at).toLocaleString("ja-JP")
    : null;

  return (
    <div className="px-4 py-5 md:px-10 md:py-8" style={{ maxWidth: "var(--content-max, 1280px)" }}>
      {/* Page heading */}
      <h1 className="mb-8 text-[18px] font-semibold leading-tight tracking-[-0.02em] text-on-surface">
        {t("usage.heading")}
      </h1>

      {/* All-time summary — no card, tonal section */}
      <Section className="mb-6">
        <div className="px-6 py-5">
          <p className="label mb-1.5 uppercase text-outline">{t("usage.allTime")}</p>
          <div className="flex items-baseline gap-4">
            <span className="font-mono text-[32px] font-semibold tabular-nums leading-none text-on-surface">
              {formatCost(data.total_cost_jpy, data.total_cost_usd, locale)}
            </span>
            {locale === "ja" && (
              <span className="data text-outline">
                {formatCostCompact(data.total_cost_jpy, data.total_cost_usd, "en")}
              </span>
            )}
          </div>
          <div className="data mt-3 flex flex-wrap items-center gap-3 text-outline">
            <span>{formatTokens(data.total_tokens)}&thinsp;tokens</span>
            {locale === "ja" && (
              <>
                <span className="text-outline-variant">·</span>
                <span>
                  1 USD = ¥{data.exchange_rate.rate_jpy.toFixed(2)}&thinsp;
                  <span
                    className={cn(
                      "rounded px-1 py-0.5 text-[9px] uppercase tracking-wider",
                      rateSource === "api"
                        ? "bg-[color-mix(in_oklab,#00e3fd_8%,transparent)] text-[var(--color-running)]"
                        : rateSource === "cache"
                          ? "bg-outline/10 text-outline"
                          : "bg-error/10 text-error",
                    )}
                  >
                    {rateSourceLabel}
                  </span>
                </span>
                {fetchedAt && (
                  <>
                    <span className="text-outline-variant">·</span>
                    <span>
                      {t("usage.exchangeRate.fetched")}&thinsp;{fetchedAt}
                    </span>
                  </>
                )}
              </>
            )}
          </div>
        </div>
      </Section>

      {/* Today / This month — side by side on md+, stacked on mobile */}
      <Section className="mb-6">
        <div className="grid grid-cols-1 md:grid-cols-2">
          <div className="[box-shadow:none] md:[box-shadow:1px_0_0_0_color-mix(in_oklab,var(--color-outline-variant,_#444748)_40%,transparent)]">
            <PeriodBlock
              label={t("usage.today")}
              totals={data.today}
              cacheLabel={t("usage.tokens.cacheReadWrite")}
              locale={locale}
            />
          </div>
          <PeriodBlock
            label={t("usage.thisMonth")}
            totals={data.this_month}
            cacheLabel={t("usage.tokens.cacheReadWrite")}
            locale={locale}
          />
        </div>
      </Section>

      {/* Daily chart */}
      <Section heading={t("usage.chart.title")} className="mb-6">
        <div className="px-6 pb-5 pt-4">
          <DailyUsageChart daily={data.daily ?? []} asOfDate={data.as_of_date} locale={locale} />
        </div>
      </Section>

      {/* Budget management */}
      <Section className="mb-6">
        <BudgetForm data={data} />
      </Section>

      {/* Project breakdown */}
      <Section heading={t("usage.breakdown.heading")} className="mb-6">
        <div className="pb-4 pt-3">
          <BreakdownTable data={data} />
        </div>
      </Section>

      {/* Model breakdown */}
      {(data.by_model ?? []).length > 0 && (
        <Section heading={t("usage.modelBreakdown.heading")} className="mb-20 md:mb-6">
          <div className="pb-4 pt-3">
            <ModelTable data={data} />
          </div>
        </Section>
      )}
    </div>
  );
}
