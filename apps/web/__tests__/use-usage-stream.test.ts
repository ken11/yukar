/**
 * use-usage-stream: TanStack Query cache patch tests via SSE events
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { UsageSummaryResponse } from "../lib/api/endpoints";
import { queryKeys } from "../lib/api/query-keys";
import { useUsageStream } from "../lib/sse/use-usage-stream";

// Mock playChime to avoid audio errors in tests
vi.mock("../lib/audio/chime", () => ({
  playChime: vi.fn(),
}));

class MockEventSource {
  url: string;
  onerror: ((ev: Event) => void) | null = null;
  private listeners: Map<string, EventListener[]> = new Map();
  static instances: MockEventSource[] = [];

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, handler: EventListener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type)?.push(handler);
  }

  removeEventListener() {}
  close() {}

  emit(type: string, data: string) {
    const ev = { type, data } as MessageEvent;
    const handlers = this.listeners.get(type) ?? [];
    for (const h of handlers) h(ev);
  }
}

function makeUsageData(overrides: Partial<UsageSummaryResponse> = {}): UsageSummaryResponse {
  return {
    total_cost_usd: 1.0,
    total_cost_jpy: 150.0,
    total_input_tokens: 1000,
    total_output_tokens: 500,
    total_cache_read_tokens: 0,
    total_cache_write_tokens: 0,
    total_embedding_tokens: 0,
    total_tokens: 1500,
    exchange_rate: { rate_jpy: 150, fetched_at: null, source: "fallback" },
    budget: {
      limit_usd: null,
      spent_usd: 1.0,
      remaining_usd: null,
      over_budget: false,
      daily_budget_usd: null,
      daily_spent_usd: 0,
      days_in_month: 30,
      month_ratio: null,
      day_ratio: null,
    },
    by_project: [],
    by_model: [],
    as_of_date: new Date().toISOString().slice(0, 10),
    ...overrides,
  };
}

let qc: QueryClient;

beforeEach(() => {
  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource);
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllTimers();
  qc.clear();
});

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(QueryClientProvider, { client: qc }, children);
}

function seedUsage(data: UsageSummaryResponse) {
  qc.setQueryData(queryKeys.usage.summary(), data);
}

function getUsage(): UsageSummaryResponse | undefined {
  return qc.getQueryData<UsageSummaryResponse>(queryKeys.usage.summary());
}

describe("useUsageStream — token_usage event", () => {
  it("patches total_cost_jpy in cache on token_usage event", () => {
    seedUsage(makeUsageData({ total_cost_jpy: 100 }));

    renderHook(() => useUsageStream(), { wrapper });

    const es = MockEventSource.instances[0];
    es.emit(
      "token_usage",
      JSON.stringify({
        type: "token_usage",
        project_id: "proj1",
        epic_id: "epic1",
        run_id: "run1",
        ts: new Date().toISOString(),
        role: "worker",
        model_id: "claude-sonnet",
        delta: {
          input: 10,
          output: 5,
          cache_read: 0,
          cache_write: 0,
          embedding: 0,
        },
        run_totals: {
          cost_usd: 0.002,
          cost_jpy: 0.3,
          budget_limit_usd: null,
          budget_remaining_usd: null,
          input_tokens: 110,
          output_tokens: 55,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          embedding_tokens: 0,
        },
        global_totals: {
          cost_usd: 2.0,
          cost_jpy: 300.0,
          budget_limit_usd: null,
          budget_remaining_usd: null,
        },
      }),
    );

    const usage = getUsage();
    expect(usage?.total_cost_jpy).toBe(300.0);
    expect(usage?.total_cost_usd).toBe(2.0);
  });

  it("patches budget spent_usd on token_usage event", () => {
    seedUsage(
      makeUsageData({
        budget: {
          limit_usd: 10,
          spent_usd: 1.0,
          remaining_usd: 9.0,
          over_budget: false,
          daily_budget_usd: null,
          daily_spent_usd: 0,
          days_in_month: 30,
          month_ratio: null,
          day_ratio: null,
        },
      }),
    );

    renderHook(() => useUsageStream(), { wrapper });

    const es = MockEventSource.instances[0];
    es.emit(
      "token_usage",
      JSON.stringify({
        type: "token_usage",
        project_id: "proj1",
        epic_id: "epic1",
        run_id: "run1",
        ts: new Date().toISOString(),
        role: "worker",
        model_id: "claude-sonnet",
        delta: { input: 10, output: 5, cache_read: 0, cache_write: 0, embedding: 0 },
        run_totals: {
          cost_usd: 0.002,
          cost_jpy: 0.3,
          budget_limit_usd: 10,
          budget_remaining_usd: 5.0,
          input_tokens: 10,
          output_tokens: 5,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          embedding_tokens: 0,
        },
        global_totals: {
          cost_usd: 8.0,
          cost_jpy: 1200.0,
          budget_limit_usd: 10,
          budget_remaining_usd: 5.0,
          month_spent_usd: 5.0,
        },
      }),
    );

    const usage = getUsage();
    // Detect confusion between month_spent_usd (current-month spend) and cost_usd (all-time total)
    expect(usage?.budget.spent_usd).toBe(5.0); // current-month spend = month_spent_usd
    expect(usage?.total_cost_usd).toBe(8.0); // all-time total = cost_usd
    expect(usage?.budget.limit_usd).toBe(10);
    expect(usage?.budget.remaining_usd).toBe(5.0);
  });

  it("does not throw when cache is empty", () => {
    // No seed
    renderHook(() => useUsageStream(), { wrapper });

    const es = MockEventSource.instances[0];
    expect(() =>
      es.emit(
        "token_usage",
        JSON.stringify({
          type: "token_usage",
          project_id: "p",
          epic_id: "e",
          run_id: "r",
          ts: new Date().toISOString(),
          role: "worker",
          model_id: "m",
          delta: { input: 1, output: 1, cache_read: 0, cache_write: 0, embedding: 0 },
          run_totals: {
            cost_usd: 0,
            cost_jpy: 0,
            input_tokens: 1,
            output_tokens: 1,
            cache_read_tokens: 0,
            cache_write_tokens: 0,
            embedding_tokens: 0,
          },
          global_totals: { cost_usd: 0, cost_jpy: 0 },
        }),
      ),
    ).not.toThrow();
  });
});

describe("useUsageStream — budget_exceeded event", () => {
  it("calls onBudgetExceeded callback on budget_exceeded event", () => {
    const onBudgetExceeded = vi.fn();
    seedUsage(makeUsageData());

    renderHook(() => useUsageStream({ onBudgetExceeded }), { wrapper });

    const es = MockEventSource.instances[0];
    es.emit(
      "budget_exceeded",
      JSON.stringify({
        type: "budget_exceeded",
        project_id: "proj1",
        epic_id: "epic1",
        run_id: "run1",
        ts: new Date().toISOString(),
        spent_usd: 10.0,
        limit_usd: 10.0,
      }),
    );

    expect(onBudgetExceeded).toHaveBeenCalledOnce();
    expect(onBudgetExceeded.mock.calls[0][0]).toMatchObject({
      type: "budget_exceeded",
      spent_usd: 10.0,
      limit_usd: 10.0,
    });
  });

  it("does not throw when onBudgetExceeded is not provided", () => {
    seedUsage(makeUsageData());

    renderHook(() => useUsageStream(), { wrapper });

    const es = MockEventSource.instances[0];
    expect(() =>
      es.emit(
        "budget_exceeded",
        JSON.stringify({
          type: "budget_exceeded",
          project_id: "p",
          epic_id: "e",
          run_id: "r",
          ts: new Date().toISOString(),
          spent_usd: 5.0,
          limit_usd: 5.0,
        }),
      ),
    ).not.toThrow();
  });
});
