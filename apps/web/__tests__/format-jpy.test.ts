/**
 * Unit tests for the JPY formatter
 */

import { describe, expect, it } from "vitest";
import {
  formatCost,
  formatCostCompact,
  formatJpy,
  formatTokens,
  formatUsd,
} from "../lib/format-jpy";

describe("formatJpy", () => {
  it("formats zero as ¥0", () => {
    expect(formatJpy(0)).toBe("¥0");
  });

  it("formats large amounts as integer JPY (>= 100)", () => {
    const result = formatJpy(1234);
    expect(result).toMatch(/¥1,234/);
  });

  it("formats 100 as integer JPY", () => {
    const result = formatJpy(100);
    expect(result).toMatch(/¥100/);
  });

  it("formats small amounts (< 100) with 2 decimal places", () => {
    expect(formatJpy(12.34)).toBe("¥12.34");
  });

  it("formats sub-yen amounts with 2 decimal places", () => {
    expect(formatJpy(0.05)).toBe("¥0.05");
  });

  it("formats 99.99 with 2 decimal places", () => {
    expect(formatJpy(99.99)).toBe("¥99.99");
  });
});

describe("formatUsd", () => {
  it("formats USD with 4 decimal places", () => {
    expect(formatUsd(1.2345)).toBe("$1.2345");
  });

  it("formats zero USD", () => {
    expect(formatUsd(0)).toBe("$0.0000");
  });
});

describe("formatTokens", () => {
  it("formats numbers with commas", () => {
    expect(formatTokens(1234567)).toBe("1,234,567");
  });

  it("formats zero", () => {
    expect(formatTokens(0)).toBe("0");
  });

  it("formats small numbers without commas", () => {
    expect(formatTokens(123)).toBe("123");
  });
});

describe("formatCost", () => {
  it("locale=ja returns formatJpy", () => {
    expect(formatCost(1234, 8.5, "ja")).toBe("¥1,234");
  });
  it("locale=en returns formatUsd", () => {
    expect(formatCost(1234, 8.5, "en")).toBe("$8.5000");
  });
});

describe("formatCostCompact", () => {
  it("locale=ja < 1000 returns exact JPY", () => {
    expect(formatCostCompact(842, 5.43, "ja")).toMatch(/^¥842/);
  });
  it("locale=ja >= 1000 returns k", () => {
    expect(formatCostCompact(1234, 7.96, "ja")).toBe("¥1.2k");
  });
  it("locale=en < 1000 returns $x.xx", () => {
    expect(formatCostCompact(1234, 7.96, "en")).toBe("$7.96");
  });
  it("locale=en >= 1000 returns $x.xk", () => {
    expect(formatCostCompact(155000, 1000.5, "en")).toBe("$1.0k");
  });
});
