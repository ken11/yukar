/**
 * Type and behavior tests for usage endpoint functions
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getUsageSummary, setBudget } from "../lib/api/endpoints";

// Mock apiFetch
vi.mock("../lib/api/client", () => ({
  apiFetch: vi.fn(),
  ApiError: class ApiError extends Error {
    status: number;
    body: unknown;
    constructor(message: string, status: number, body: unknown) {
      super(message);
      this.status = status;
      this.body = body;
    }
  },
}));

import { apiFetch } from "../lib/api/client";

const mockFetch = apiFetch as ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockFetch.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("getUsageSummary", () => {
  it("calls GET /api/usage", async () => {
    const mockData = {
      total_cost_usd: 1.0,
      total_cost_jpy: 150,
      total_input_tokens: 100,
      total_output_tokens: 50,
      total_cache_read_tokens: 0,
      total_cache_write_tokens: 0,
      total_embedding_tokens: 0,
      total_tokens: 150,
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
    };
    mockFetch.mockResolvedValueOnce(mockData);

    const result = await getUsageSummary();

    expect(mockFetch).toHaveBeenCalledWith("/api/usage");
    expect(result.total_cost_jpy).toBe(150);
    expect(result.exchange_rate.source).toBe("fallback");
  });
});

describe("setBudget", () => {
  it("calls PUT /api/usage/budget with limit_usd", async () => {
    mockFetch.mockResolvedValueOnce({ limit_usd: 50, message: "OK" });

    await setBudget({ limit_usd: 50 });

    expect(mockFetch).toHaveBeenCalledWith("/api/usage/budget", {
      method: "PUT",
      body: { limit_usd: 50 },
    });
  });

  it("calls PUT /api/usage/budget with null to clear", async () => {
    mockFetch.mockResolvedValueOnce({ limit_usd: null, message: "Cleared" });

    await setBudget({ limit_usd: null });

    expect(mockFetch).toHaveBeenCalledWith("/api/usage/budget", {
      method: "PUT",
      body: { limit_usd: null },
    });
  });
});
