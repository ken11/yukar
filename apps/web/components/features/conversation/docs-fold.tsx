"use client";

import { useState } from "react";
import { Icon } from "@/components/icon";
import { useT } from "@/lib/i18n/provider";

// ---------------------------------------------------------------------------
// Tool call instrument row
// ---------------------------------------------------------------------------

/**
 * ToolCallRow — instrument row (row, not card; state shown as glyph + label; cyan only while running).
 */
export function ToolCallRow({
  toolName,
  args,
  result,
}: {
  toolName: string;
  args: Record<string, unknown>;
  result?: string;
}) {
  const t = useT();
  const [expanded, setExpanded] = useState(false);
  const isDone = result !== undefined;
  const hasFailed = typeof result === "string" && result.startsWith("Error");

  return (
    <div
      className="my-1.5"
      style={{
        borderLeft: isDone
          ? "1px solid var(--color-outline-variant)"
          : `1px solid var(--color-light)`,
      }}
    >
      {/* Instrument header row */}
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white focus-visible:ring-inset hover:bg-surface-container-high"
      >
        {/* State glyph */}
        {isDone ? (
          <Icon
            name={hasFailed ? "warning" : "check"}
            className={`shrink-0 text-[13px] ${hasFailed ? "text-error" : "text-on-surface-variant"}`}
            aria-hidden
          />
        ) : (
          <span
            className="h-1.5 w-1.5 shrink-0 rounded-full"
            style={{ backgroundColor: "var(--color-light)" }}
            aria-hidden
          />
        )}

        {/* Tool name — mono */}
        <span className="data truncate">{toolName}</span>

        {/* State label */}
        <span className="data ml-auto shrink-0 opacity-60">
          {isDone ? (hasFailed ? "▲ error" : "✓") : t("conversation.streamingLive")}
        </span>

        {/* Expand chevron */}
        <Icon
          name={expanded ? "expand_less" : "expand_more"}
          className="shrink-0 text-[14px] text-on-surface-variant"
          aria-hidden
        />
      </button>

      {/* Expanded detail — recess surface */}
      {expanded && (
        <div
          className="px-3 py-2 font-mono text-[12px] text-on-surface-variant"
          style={{ backgroundColor: "var(--color-surface-container-lowest)" }}
        >
          <p className="mb-1 text-[10px] uppercase tracking-widest text-outline">
            {t("conversation.toolInput")}
          </p>
          <pre className="overflow-x-auto whitespace-pre-wrap break-words leading-relaxed">
            {JSON.stringify(args, null, 2)}
          </pre>
          {result !== undefined && (
            <>
              <p className="mb-1 mt-3 text-[10px] uppercase tracking-widest text-outline">
                {t("conversation.toolResult")}
              </p>
              <pre className="overflow-x-auto whitespace-pre-wrap break-words leading-relaxed">
                {result}
              </pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tool run group — a run of same-named calls folded into one instrument row
// ---------------------------------------------------------------------------

/**
 * ToolRunGroup — consecutive tool-only messages with the SAME tool name
 * ("task_update ×3") fold into one row; expanding shows each call as a
 * ToolCallRow. Cyan only while a call is still running.
 */
export function ToolRunGroup({
  toolName,
  calls,
}: {
  toolName: string;
  calls: Array<{
    toolCallId?: string;
    args?: Record<string, unknown>;
    result?: string;
  }>;
}) {
  const t = useT();
  const [expanded, setExpanded] = useState(false);
  const isDone = calls.every((c) => c.result !== undefined);
  const hasFailed = calls.some((c) => typeof c.result === "string" && c.result.startsWith("Error"));

  return (
    <div
      className="my-1.5"
      data-testid="tool-run-group"
      style={{
        borderLeft: isDone
          ? "1px solid var(--color-outline-variant)"
          : "1px solid var(--color-light)",
      }}
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white focus-visible:ring-inset hover:bg-surface-container-high"
      >
        {isDone ? (
          <Icon
            name={hasFailed ? "warning" : "check"}
            className={`shrink-0 text-[13px] ${hasFailed ? "text-error" : "text-on-surface-variant"}`}
            aria-hidden
          />
        ) : (
          <span
            className="h-1.5 w-1.5 shrink-0 rounded-full"
            style={{ backgroundColor: "var(--color-light)" }}
            aria-hidden
          />
        )}
        <span className="data truncate">{toolName}</span>
        {calls.length > 1 && (
          <span className="data shrink-0" style={{ color: "var(--color-outline)" }}>
            ×{calls.length}
          </span>
        )}
        <span className="data ml-auto shrink-0 opacity-60">
          {isDone ? (hasFailed ? "▲ error" : "✓") : t("conversation.streamingLive")}
        </span>
        <Icon
          name={expanded ? "expand_less" : "expand_more"}
          className="shrink-0 text-[14px] text-on-surface-variant"
          aria-hidden
        />
      </button>
      {expanded && (
        <div className="pl-2">
          {calls.map((c, i) => (
            <ToolCallRow
              key={c.toolCallId ?? i}
              toolName={toolName}
              args={c.args ?? {}}
              result={c.result}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Docs fold section
// ---------------------------------------------------------------------------

export function DocsFoldSection({ title, body }: { title: string; body: string }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div
      className="my-2"
      style={{
        borderLeft: "1px solid var(--color-outline-variant)",
        backgroundColor: "var(--color-surface-container-lowest)",
      }}
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-surface-container focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white focus-visible:ring-inset"
      >
        <Icon
          name="description"
          className="shrink-0 text-[13px] text-on-surface-variant"
          aria-hidden
        />
        <span className="data truncate">{title}</span>
        <Icon
          name={expanded ? "expand_less" : "expand_more"}
          className="ml-auto shrink-0 text-[14px] text-on-surface-variant"
          aria-hidden
        />
      </button>
      {expanded && (
        <div className="border-t border-outline-variant/30 px-3 py-2">
          <pre className="overflow-x-auto whitespace-pre-wrap font-mono text-[12px] leading-relaxed text-on-surface-variant">
            {body}
          </pre>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Docs sections parser
// ---------------------------------------------------------------------------

export function splitDocsSections(
  text: string,
): Array<{ kind: "text" | "docs"; title: string; body: string }> {
  const docsHeadingRe = /^(# (?:Project|Epic) Documentation\b)/m;
  const parts: Array<{ kind: "text" | "docs"; title: string; body: string }> = [];

  let remaining = text;
  while (remaining.length > 0) {
    const match = docsHeadingRe.exec(remaining);
    if (!match) {
      parts.push({ kind: "text", title: "", body: remaining });
      break;
    }
    if (match.index > 0) {
      parts.push({ kind: "text", title: "", body: remaining.slice(0, match.index) });
    }
    const afterHeading = remaining.slice(match.index + match[1].length);
    const nextMatch = docsHeadingRe.exec(afterHeading);
    const bodyEnd = nextMatch ? nextMatch.index : afterHeading.length;
    parts.push({
      kind: "docs",
      title: match[1].replace(/^# /, ""),
      body: afterHeading.slice(0, bodyEnd).trim(),
    });
    remaining = afterHeading.slice(bodyEnd);
  }
  return parts;
}
