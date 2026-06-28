"use client";

import { useState } from "react";
import type { UsageDailyPoint } from "@/lib/api/endpoints";
import { formatCost, formatTokens } from "@/lib/format-jpy";
import type { Locale } from "@/lib/i18n/dictionary";
import { useT } from "@/lib/i18n/provider";

interface DailyUsageChartProps {
  daily: UsageDailyPoint[];
  /** "YYYY-MM-DD" in JST — used to compute the number of days in the current month and highlight today */
  asOfDate: string;
  locale: Locale;
}

/** Generate an array of date strings for every day of the month (1 through last day) from as_of_date */
function buildMonthDays(asOfDate: string): string[] {
  const [year, month] = asOfDate.split("-").map(Number);
  // Return an empty array if asOfDate is empty or invalid (NaN) to prevent crashes
  if (Number.isNaN(year) || Number.isNaN(month) || month < 1 || month > 12) return [];
  const daysInMonth = new Date(year, month, 0).getDate();
  return Array.from({ length: daysInMonth }, (_, i) => {
    const d = String(i + 1).padStart(2, "0");
    return `${year}-${String(month).padStart(2, "0")}-${d}`;
  });
}

export function DailyUsageChart({ daily, asOfDate, locale }: DailyUsageChartProps) {
  const t = useT();
  const [tooltip, setTooltip] = useState<{
    x: number;
    y: number;
    point: { date: string; cost_jpy: number; cost_usd: number; total_tokens: number };
  } | null>(null);

  const allDays = buildMonthDays(asOfDate);

  // Safely render an empty chart when asOfDate is empty or invalid
  if (allDays.length === 0) {
    return (
      <div className="flex h-[180px] w-full items-center justify-center text-body-sm text-outline">
        {t("usage.chart.noData")}
      </div>
    );
  }
  const dataMap = new Map(daily.map((d) => [d.date, d]));

  // Points for every day (days with no data default to 0)
  const points = allDays.map((date) => {
    const d = dataMap.get(date);
    return {
      date,
      cost_jpy: d?.cost_jpy ?? 0,
      cost_usd: d?.cost_usd ?? 0,
      total_tokens: d?.total_tokens ?? 0,
    };
  });

  const maxCost = Math.max(...points.map((p) => p.cost_jpy), 0.001);
  const maxCostUsd = Math.max(...points.map((p) => p.cost_usd), 0.001);
  const totalDays = allDays.length;

  // SVG dimensions
  const W = 600;
  const H = 180;
  const padL = 52;
  const padR = 16;
  const padT = 16;
  const padB = 32;
  const chartW = W - padL - padR;
  const chartH = H - padT - padB;

  /** Day index (0-based) → SVG x coordinate */
  const xOf = (i: number) => padL + (i / Math.max(totalDays - 1, 1)) * chartW;
  /** Cost value → SVG y coordinate */
  const yOf = (v: number) => padT + chartH - (v / maxCost) * chartH;

  // Point string for the polyline
  const polylinePoints = points.map((p, i) => `${xOf(i)},${yOf(p.cost_jpy)}`).join(" ");

  // Y-axis grid lines (0, 50%, 100%)
  const yGridLines = [0, 0.5, 1].map((ratio) => ({
    y: padT + chartH - ratio * chartH,
    label:
      ratio === 0
        ? locale === "ja"
          ? "¥0"
          : "$0"
        : formatCost(maxCost * ratio, maxCostUsd * ratio, locale),
  }));

  // X-axis labels (show around the start, middle, and end of the month)
  const xLabels: { i: number; label: string }[] = [];
  const labelStep = totalDays <= 15 ? 5 : 10;
  for (let i = 0; i < totalDays; i++) {
    const day = i + 1;
    if (day === 1 || day % labelStep === 0 || i === totalDays - 1) {
      xLabels.push({ i, label: String(day) });
    }
  }

  return (
    <div className="relative w-full">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        aria-label={t("usage.chart.ariaLabel")}
        className="overflow-visible"
      >
        {/* Grid lines */}
        {yGridLines.map(({ y, label }) => (
          <g key={y}>
            <line
              x1={padL}
              y1={y}
              x2={W - padR}
              y2={y}
              stroke="var(--color-outline-variant, #444748)"
              strokeWidth="0.5"
              strokeDasharray="3 3"
            />
            <text
              x={padL - 6}
              y={y}
              dominantBaseline="middle"
              textAnchor="end"
              fontSize="9"
              fill="var(--color-outline, #8e9192)"
              fontFamily="var(--font-geist-mono, monospace)"
            >
              {label}
            </text>
          </g>
        ))}

        {/* X axis */}
        <line
          x1={padL}
          y1={padT + chartH}
          x2={W - padR}
          y2={padT + chartH}
          stroke="var(--color-outline-variant, #444748)"
          strokeWidth="0.5"
        />

        {/* X-axis labels */}
        {xLabels.map(({ i, label }) => (
          <text
            key={i}
            x={xOf(i)}
            y={padT + chartH + 14}
            textAnchor="middle"
            fontSize="9"
            fill="var(--color-outline, #8e9192)"
            fontFamily="var(--font-geist-mono, monospace)"
          >
            {label}
          </text>
        ))}

        {/* Line — cyan = the single point of light for the primary series */}
        <polyline
          points={polylinePoints}
          fill="none"
          stroke="var(--color-running, #00e3fd)"
          strokeWidth="1.5"
          strokeLinejoin="round"
          strokeLinecap="round"
        />

        {/* Per-day dots */}
        {points.map((p, i) => {
          const cx = xOf(i);
          const cy = yOf(p.cost_jpy);
          const isToday = p.date === asOfDate;
          return (
            // biome-ignore lint/a11y/noStaticElementInteractions: SVG tooltip trigger — keyboard nav not required for chart dots
            <g
              key={p.date}
              className="cursor-pointer"
              onMouseEnter={(e) => {
                const svg = (e.currentTarget as SVGElement).ownerSVGElement;
                if (!svg) return;
                const rect = svg.getBoundingClientRect();
                const scaleX = rect.width / W;
                const scaleY = rect.height / H;
                setTooltip({
                  x: cx * scaleX,
                  y: cy * scaleY - 8,
                  point: p,
                });
              }}
              onMouseLeave={() => setTooltip(null)}
            >
              <circle
                cx={cx}
                cy={cy}
                r={isToday ? 4 : 2.5}
                fill={
                  isToday
                    ? "var(--color-running, #00e3fd)"
                    : "var(--color-surface-container-high, #2a2a2c)"
                }
                stroke="var(--color-running, #00e3fd)"
                strokeWidth={isToday ? 1.5 : 1}
              >
                <title>{`${p.date}: ${formatCost(p.cost_jpy, p.cost_usd, locale)} / ${formatTokens(p.total_tokens)} tokens`}</title>
              </circle>
            </g>
          );
        })}
      </svg>

      {/* Hover tooltip */}
      {tooltip && (
        <div
          className="pointer-events-none absolute z-10 rounded border border-outline-variant bg-surface-container-highest px-2.5 py-1.5 text-[11px] text-on-surface shadow-sm"
          style={{
            left: tooltip.x,
            top: tooltip.y,
            transform: "translate(-50%, -100%)",
          }}
        >
          <div className="font-semibold tabular-nums">{tooltip.point.date}</div>
          <div className="font-mono tabular-nums text-[var(--color-running,#00e3fd)]">
            {formatCost(tooltip.point.cost_jpy, tooltip.point.cost_usd, locale)}
          </div>
          <div className="text-on-surface-variant tabular-nums">
            {formatTokens(tooltip.point.total_tokens)} tok
          </div>
        </div>
      )}
    </div>
  );
}
