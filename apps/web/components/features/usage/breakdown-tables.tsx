"use client";

import { Fragment } from "react";
import type { UsageSummaryResponse } from "@/lib/api/endpoints";
import { formatCost, formatTokens } from "@/lib/format-jpy";
import { useLocale, useT } from "@/lib/i18n/provider";

// ---- Breakdown table ----

export function BreakdownTable({ data }: { data: UsageSummaryResponse }) {
  const t = useT();
  const locale = useLocale();
  const byProject = data.by_project ?? [];

  if (byProject.length === 0) {
    return <p className="data px-6 py-6 text-outline">{t("usage.breakdown.noData")}</p>;
  }

  return (
    <div className="max-h-[420px] overflow-x-auto overflow-y-auto">
      <table className="w-full border-collapse">
        <thead>
          <tr
            className="sticky top-0 z-10 bg-surface-container-high text-left"
            style={{
              boxShadow: "0 1px 0 0 var(--color-outline-variant, #444748)",
            }}
          >
            <th className="label pb-2 pl-6 pr-4 pt-1 uppercase text-outline">
              {t("usage.breakdown.columns.projectEpicRun")}
            </th>
            <th className="label pb-2 pr-4 pt-1 text-right uppercase text-outline">
              {t("usage.breakdown.columns.input")}
            </th>
            <th className="label pb-2 pr-4 pt-1 text-right uppercase text-outline">
              {t("usage.breakdown.columns.output")}
            </th>
            <th className="label pb-2 pr-4 pt-1 text-right uppercase text-outline">
              {t("usage.breakdown.columns.cache")}
            </th>
            <th className="label pb-2 pr-4 pt-1 text-right uppercase text-outline">
              {t("usage.breakdown.columns.embed")}
            </th>
            <th className="label pb-2 pr-6 pt-1 text-right uppercase text-outline">
              {t("usage.breakdown.columns.cost")}
            </th>
          </tr>
        </thead>
        <tbody>
          {byProject.map((project) => (
            <Fragment key={`proj-${project.project_id}`}>
              <tr
                style={{
                  boxShadow:
                    "0 1px 0 0 color-mix(in oklab, var(--color-outline-variant, #444748) 50%, transparent)",
                }}
              >
                <td
                  colSpan={6}
                  className="py-2 pl-6 font-mono text-[12px] font-semibold text-on-surface"
                >
                  {project.project_id}
                </td>
              </tr>
              {(project.epics ?? []).map((epic) => (
                <Fragment key={`epic-${project.project_id}-${epic.epic_id}`}>
                  <tr>
                    <td
                      colSpan={6}
                      className="py-1 pl-10 font-mono text-[11px] text-on-surface-variant"
                    >
                      {epic.epic_id}
                    </td>
                  </tr>
                  {(epic.runs ?? []).map((run) => (
                    <tr
                      key={`run-${run.run_id}`}
                      className="transition-colors hover:bg-surface-container-high"
                    >
                      <td className="data py-1.5 pl-14 text-outline">{run.run_id.slice(0, 8)}</td>
                      <td className="data py-1.5 pr-4 text-right text-on-surface-variant">
                        {formatTokens(run.input_tokens)}
                      </td>
                      <td className="data py-1.5 pr-4 text-right text-on-surface-variant">
                        {formatTokens(run.output_tokens)}
                      </td>
                      <td className="data py-1.5 pr-4 text-right text-on-surface-variant">
                        {formatTokens(run.cache_read_tokens + run.cache_write_tokens)}
                      </td>
                      <td className="data py-1.5 pr-4 text-right text-on-surface-variant">
                        {formatTokens(run.embedding_tokens)}
                      </td>
                      <td className="data py-1.5 pr-6 text-right text-on-surface">
                        {formatCost(run.cost_jpy, run.cost_usd, locale)}
                      </td>
                    </tr>
                  ))}
                </Fragment>
              ))}
              {project.arbiter != null && (
                <Fragment key={`arbiter-${project.project_id}`}>
                  <tr>
                    <td colSpan={6} className="py-1 pl-10 font-mono text-[11px] text-outline">
                      {t("usage.breakdown.arbiter")}
                    </td>
                  </tr>
                  {(project.arbiter.runs ?? []).map((run) => (
                    <tr
                      key={`arbiter-run-${run.run_id}`}
                      className="transition-colors hover:bg-surface-container-high"
                    >
                      <td className="data py-1.5 pl-14 text-outline">{run.run_id.slice(0, 8)}</td>
                      <td className="data py-1.5 pr-4 text-right text-on-surface-variant">
                        {formatTokens(run.input_tokens)}
                      </td>
                      <td className="data py-1.5 pr-4 text-right text-on-surface-variant">
                        {formatTokens(run.output_tokens)}
                      </td>
                      <td className="data py-1.5 pr-4 text-right text-on-surface-variant">
                        {formatTokens(run.cache_read_tokens + run.cache_write_tokens)}
                      </td>
                      <td className="data py-1.5 pr-4 text-right text-on-surface-variant">
                        {formatTokens(run.embedding_tokens)}
                      </td>
                      <td className="data py-1.5 pr-6 text-right text-on-surface">
                        {formatCost(run.cost_jpy, run.cost_usd, locale)}
                      </td>
                    </tr>
                  ))}
                </Fragment>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---- Model breakdown ----

export function ModelTable({ data }: { data: UsageSummaryResponse }) {
  const t = useT();
  const locale = useLocale();
  const byModel = data.by_model ?? [];

  if (byModel.length === 0) return null;

  return (
    <div className="max-h-[420px] overflow-x-auto overflow-y-auto">
      <table className="w-full border-collapse">
        <thead>
          <tr
            className="sticky top-0 z-10 bg-surface-container-high text-left"
            style={{
              boxShadow: "0 1px 0 0 var(--color-outline-variant, #444748)",
            }}
          >
            <th className="label pb-2 pl-6 pr-4 pt-1 uppercase text-outline">
              {t("usage.modelBreakdown.columns.model")}
            </th>
            <th className="label pb-2 pr-4 pt-1 text-right uppercase text-outline">
              {t("usage.modelBreakdown.columns.input")}
            </th>
            <th className="label pb-2 pr-4 pt-1 text-right uppercase text-outline">
              {t("usage.modelBreakdown.columns.output")}
            </th>
            <th className="label pb-2 pr-4 pt-1 text-right uppercase text-outline">
              {t("usage.modelBreakdown.columns.cache")}
            </th>
            <th className="label pb-2 pr-6 pt-1 text-right uppercase text-outline">
              {t("usage.modelBreakdown.columns.cost")}
            </th>
          </tr>
        </thead>
        <tbody>
          {byModel.map((row) => (
            <tr
              key={row.model_id}
              className="transition-colors hover:bg-surface-container-high"
              style={{
                boxShadow:
                  "0 1px 0 0 color-mix(in oklab, var(--color-outline-variant, #444748) 30%, transparent)",
              }}
            >
              <td className="data py-2 pl-6 text-on-surface-variant">{row.model_id}</td>
              <td className="data py-2 pr-4 text-right text-on-surface-variant">
                {formatTokens(row.input_tokens)}
              </td>
              <td className="data py-2 pr-4 text-right text-on-surface-variant">
                {formatTokens(row.output_tokens)}
              </td>
              <td className="data py-2 pr-4 text-right text-on-surface-variant">
                {formatTokens(row.cache_read_tokens + row.cache_write_tokens)}
              </td>
              <td className="data py-2 pr-6 text-right text-on-surface">
                {formatCost(row.cost_jpy, row.cost_usd, locale)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
