import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CostTicker } from "@/components/features/usage/cost-ticker";
import { getUsageSummary, type UsageSummaryResponse } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { getDictionary } from "@/lib/i18n/dictionary";
import { I18nProvider } from "@/lib/i18n/provider";

vi.mock("@/lib/api/endpoints", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/endpoints")>();
  return {
    ...actual,
    getUsageSummary: vi.fn(),
  };
});

vi.mock("@/lib/sse/use-usage-stream", () => ({
  makeBudgetExceededHandler: vi.fn(),
  useUsageStream: vi.fn(),
}));

const initialData = {
  total_cost_jpy: 999,
  total_cost_usd: 6.5,
  today: {
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    embedding_tokens: 0,
    total_tokens: 0,
    cost_usd: 0.8,
    cost_jpy: 123,
  },
  budget: {
    limit_usd: null,
    spent_usd: 0.8,
    remaining_usd: null,
    over_budget: false,
    daily_budget_usd: null,
    daily_spent_usd: 0,
    days_in_month: 30,
    month_ratio: null,
    day_ratio: null,
  },
} as UsageSummaryResponse;

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CostTicker", () => {
  it("refetches usage after the summary query is invalidated", async () => {
    vi.mocked(getUsageSummary).mockResolvedValue(initialData);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <I18nProvider locale="ja" dict={getDictionary("ja")}>
          <CostTicker initialData={initialData} />
        </I18nProvider>
      </QueryClientProvider>,
    );

    // Shows today's spend, not the all-time total
    expect(screen.getByRole("link", { name: "本日 ¥123" })).toBeInTheDocument();

    await queryClient.invalidateQueries({ queryKey: queryKeys.usage.summary() });

    await waitFor(() => expect(getUsageSummary).toHaveBeenCalledOnce());
  });

  it("displays USD when locale=en", () => {
    const enData = {
      ...initialData,
      today: { ...initialData.today, cost_usd: 8.5 },
    } as UsageSummaryResponse;

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const dict = getDictionary("en");

    render(
      <QueryClientProvider client={queryClient}>
        <I18nProvider locale="en" dict={dict}>
          <CostTicker initialData={enData} />
        </I18nProvider>
      </QueryClientProvider>,
    );

    // formatCostCompact(_, 8.5, "en") → "$8.50"
    expect(screen.getByRole("link", { name: "Today $8.50" })).toBeInTheDocument();
  });
});
