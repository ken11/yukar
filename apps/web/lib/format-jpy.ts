/**
 * JPY amount formatter.
 *
 * Because Intl.NumberFormat may produce a full-width ¥ (U+FFE5) depending on the environment,
 * always manually prefix with the half-width ¥ (U+00A5).
 */
import type { Locale } from "@/lib/i18n/dictionary";

const _commaFmt = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 0,
  minimumFractionDigits: 0,
});

/**
 * Converts a JPY amount to a display string.
 * - 100 yen or more: rounded to integer (¥1,234)
 * - Less than 100 yen: 2 decimal places (¥12.34)
 */
export function formatJpy(amount: number): string {
  if (amount >= 100 || amount === 0) {
    return `¥${_commaFmt.format(Math.round(amount))}`;
  }
  // Display small amounts with 2 decimal places
  return `¥${amount.toFixed(2)}`;
}

/**
 * Converts a JPY amount to a compact notation for rail display.
 * - < 1000: exact (¥842)
 * - >= 1000: 1 decimal place k (¥1.2k)
 * - >= 100_000: integer k (¥123k) — the decimal no longer fits the 56px rail
 * - >= 1_000_000: 1 decimal place M (¥1.2M), integer M from ¥100M (¥123M)
 * Always abbreviated to fit the available space. Pass the exact total separately to formatJpy for the title attribute.
 */
export function formatJpyCompact(amount: number): string {
  // Thresholds sit at the ROUNDING boundary, not the display boundary:
  // ¥999,500 already rounds to "¥1000k" in the k-branch (7 chars — wider than
  // the rail), so it must be promoted to "¥1.0M" instead.  Same for 99,950
  // ("¥100.0k" → "¥100k") and 99,950,000 ("¥100.0M" → "¥100M").
  if (amount >= 99_950_000) {
    return `¥${Math.round(amount / 1_000_000)}M`;
  }
  if (amount >= 999_500) {
    return `¥${(amount / 1_000_000).toFixed(1)}M`;
  }
  if (amount >= 99_950) {
    return `¥${Math.round(amount / 1_000)}k`;
  }
  if (amount >= 1_000) {
    return `¥${(amount / 1_000).toFixed(1)}k`;
  }
  return formatJpy(amount);
}

/**
 * Converts a USD amount to a display string (4 decimal places).
 * Precision to show even tiny per-run costs (e.g. $0.0105) without rounding to zero.
 */
export function formatUsd(amount: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 4,
    maximumFractionDigits: 4,
  }).format(amount);
}

/**
 * For displaying budget-related values such as "USD-denominated configured amounts and remaining balance" (2 decimal places: $50.00).
 * Use formatUsd (4 decimal places) for displaying tiny per-run costs.
 */
export function formatUsdBudget(amount: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amount);
}

/**
 * Formats a token count with comma separators.
 */
export function formatTokens(n: number): string {
  return new Intl.NumberFormat("en-US").format(n);
}

/**
 * Returns a currency display string appropriate for the locale.
 * - ja: formatJpy(jpy)
 * - en: formatUsd(usd)
 */
export function formatCost(jpy: number, usd: number, locale: Locale): string {
  return locale === "ja" ? formatJpy(jpy) : formatUsd(usd);
}

/**
 * Returns a compact currency display string appropriate for the locale.
 * - ja: formatJpyCompact(jpy)
 * - en: $x.xx / $x.xk / $x.xM
 */
export function formatCostCompact(jpy: number, usd: number, locale: Locale): string {
  if (locale === "ja") return formatJpyCompact(jpy);
  // EN: compact USD — same width budget (and same rounding-boundary
  // thresholds, see formatJpyCompact) as the JPY rail display.
  if (usd >= 99_950_000) return `$${Math.round(usd / 1_000_000)}M`;
  if (usd >= 999_500) return `$${(usd / 1_000_000).toFixed(1)}M`;
  if (usd >= 99_950) return `$${Math.round(usd / 1_000)}k`;
  if (usd >= 999.5) return `$${(usd / 1_000).toFixed(1)}k`;
  // $100–999: the cents no longer fit the rail
  if (usd >= 99.5) return `$${Math.round(usd)}`;
  // < $100: 2 decimal places
  return `$${usd.toFixed(2)}`;
}
