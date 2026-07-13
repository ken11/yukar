"use client";

import { useState } from "react";
import { Icon } from "@/components/icon";
import type { KickoffView } from "@/lib/conversation/kickoff";
import { useT } from "@/lib/i18n/provider";

/**
 * KickoffBlock — the structured opening of a conversation (epic kickoff /
 * task hand-off). Shows what a human decided (title, description, acceptance
 * criteria, contract); the host's instruction boilerplate stays behind the
 * "show full prompt" fold.
 */
export function KickoffBlock({ view, raw }: { view: KickoffView; raw: string }) {
  const t = useT();
  const [showRaw, setShowRaw] = useState(false);

  return (
    <div data-testid="kickoff-block">
      <p
        className="mb-1 font-mono text-[10px] font-semibold uppercase tracking-[0.1em]"
        style={{ color: "var(--color-outline)" }}
      >
        {view.kind === "epic" ? "Epic" : "Task"}
      </p>
      <p className="mb-2 text-[15px] font-semibold leading-relaxed text-on-surface">{view.title}</p>
      {view.sections.map((s, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: sections are parsed from static text and never reorder; labels can repeat (duplicate headings in replayed prompts)
        <div key={`${i}:${s.label ?? "lead"}`} className="mb-2 last:mb-0">
          {s.label && (
            <p
              className="mb-0.5 font-mono text-[10px] font-medium uppercase tracking-[0.08em]"
              style={{ color: "var(--color-outline)" }}
            >
              {s.label}
            </p>
          )}
          <p className="whitespace-pre-wrap text-[13px] leading-[1.7] text-on-surface-variant">
            {s.text}
          </p>
        </div>
      ))}

      <button
        type="button"
        onClick={() => setShowRaw((v) => !v)}
        aria-expanded={showRaw}
        data-testid="kickoff-fold-toggle"
        className="mt-2 flex items-center gap-1 font-mono text-[11px] text-outline transition-colors hover:text-on-surface-variant focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white"
      >
        <Icon
          name={showRaw ? "expand_less" : "chevron_right"}
          className="text-[13px]"
          aria-hidden
        />
        {t("conversation.showFullPrompt")}
      </button>
      {showRaw && (
        <pre
          className="mt-2 overflow-x-auto whitespace-pre-wrap border-l pl-3 font-mono text-[12px] leading-relaxed"
          style={{
            borderColor: "var(--color-outline-variant)",
            color: "var(--color-on-surface-variant)",
          }}
        >
          {raw}
        </pre>
      )}
    </div>
  );
}
