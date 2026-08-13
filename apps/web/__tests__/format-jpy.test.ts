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
  // Wide amounts drop precision so they never overflow the 56px rail
  it("locale=ja >= 100k drops the decimal (¥123k)", () => {
    expect(formatCostCompact(123_456, 800, "ja")).toBe("¥123k");
  });
  it("locale=ja >= 1M keeps 1 decimal (¥1.2M)", () => {
    expect(formatCostCompact(1_234_567, 8000, "ja")).toBe("¥1.2M");
  });
  it("locale=ja >= 100M drops the decimal (¥123M)", () => {
    expect(formatCostCompact(123_456_789, 800_000, "ja")).toBe("¥123M");
  });
  it("locale=en $100-999 drops the cents", () => {
    expect(formatCostCompact(20_000, 123.45, "en")).toBe("$123");
  });
  it("locale=en >= 100k drops the decimal ($123k)", () => {
    expect(formatCostCompact(20_000_000, 123_456, "en")).toBe("$123k");
  });
  // Rounding boundaries: a value that would ROUND into a 7-char string must be
  // promoted to the next unit instead (¥999,500 → "¥1.0M", never "¥1000k")
  it("locale=ja promotes at the rounding boundary, never emitting 7 chars", () => {
    expect(formatCostCompact(999_499, 6500, "ja")).toBe("¥999k");
    expect(formatCostCompact(999_500, 6500, "ja")).toBe("¥1.0M");
    expect(formatCostCompact(99_949, 650, "ja")).toBe("¥99.9k");
    expect(formatCostCompact(99_950, 650, "ja")).toBe("¥100k");
    expect(formatCostCompact(99_950_000, 650_000, "ja")).toBe("¥100M");
  });
  it("locale=en promotes at the rounding boundary, never emitting 7 chars", () => {
    expect(formatCostCompact(1, 999.49, "en")).toBe("$999");
    expect(formatCostCompact(1, 999.5, "en")).toBe("$1.0k");
    expect(formatCostCompact(1, 99_950, "en")).toBe("$100k");
  });
});
