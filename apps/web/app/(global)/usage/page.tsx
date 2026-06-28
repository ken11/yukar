import { UsageDashboardClient } from "@/components/features/usage/usage-dashboard-client";
import { getUsageSummary } from "@/lib/api/endpoints";

export default async function UsagePage() {
  const initialData = await getUsageSummary().catch(() => ({
    total_cost_usd: 0,
    total_cost_jpy: 0,
    total_input_tokens: 0,
    total_output_tokens: 0,
    total_cache_read_tokens: 0,
    total_cache_write_tokens: 0,
    total_embedding_tokens: 0,
    total_tokens: 0,
    exchange_rate: {
      rate_jpy: 150,
      fetched_at: null,
      source: "fallback",
    },
    budget: {
      limit_usd: null,
      spent_usd: 0,
      remaining_usd: null,
      over_budget: false,
      daily_budget_usd: null,
      daily_spent_usd: 0,
      // JST-based fallback: use Intl to get the JST year/month, then compute the last day of the month
      days_in_month: (() => {
        const jstParts = new Intl.DateTimeFormat("en-US", {
          timeZone: "Asia/Tokyo",
          year: "numeric",
          month: "numeric",
        }).formatToParts(new Date());
        const jstYear = Number(jstParts.find((p) => p.type === "year")?.value ?? 0);
        const jstMonth = Number(jstParts.find((p) => p.type === "month")?.value ?? 1);
        return new Date(jstYear, jstMonth, 0).getDate();
      })(),
      month_ratio: null,
      day_ratio: null,
    },
    by_project: [],
    by_model: [],
    today: undefined,
    this_month: undefined,
    daily: [],
    as_of_date: new Date().toISOString().slice(0, 10),
  }));

  return <UsageDashboardClient initialData={initialData} />;
}
