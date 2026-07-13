"use client";

import { DocsFoldSection, splitDocsSections, ToolCallRow } from "./docs-fold";
import { MessageMarkdown } from "./message-markdown";

// ---------------------------------------------------------------------------
// Message content renderer
// ---------------------------------------------------------------------------

/**
 * Agent prose renders as markdown (the report is a document); user input
 * renders verbatim (pre-wrap) — humans type plain text and it must not be
 * reinterpreted. Docs sections stay folded for both.
 */
function TextBlock({ text, isUser, peak }: { text: string; isUser: boolean; peak: boolean }) {
  if (isUser) {
    return (
      <p className="text-body-md leading-[var(--leading-prose,1.6)] text-on-surface whitespace-pre-wrap">
        {text}
      </p>
    );
  }
  return <MessageMarkdown text={text} peak={peak} />;
}

export function MessageContent({
  content,
  isUser = false,
  peak = false,
}: {
  content: import("@assistant-ui/react").ThreadMessageLike["content"];
  /** User input renders verbatim; agent text renders as markdown. */
  isUser?: boolean;
  /** Terrain high ground — the parked question/report addressed to the user. */
  peak?: boolean;
}) {
  if (typeof content === "string") {
    return <TextBlock text={content} isUser={isUser} peak={peak} />;
  }
  return (
    <div>
      {content.map((part, i) => {
        if (part.type === "text") {
          const key = `text:${i}`;
          const sections = splitDocsSections(part.text);
          if (sections.some((s) => s.kind === "docs")) {
            return (
              <div key={key}>
                {sections.map((s) => {
                  const sKey = `${s.kind}:${s.title || s.body.slice(0, 40)}`;
                  if (s.kind === "docs") {
                    return <DocsFoldSection key={sKey} title={s.title} body={s.body} />;
                  }
                  if (!s.body.trim()) return null;
                  return <TextBlock key={sKey} text={s.body} isUser={isUser} peak={peak} />;
                })}
              </div>
            );
          }
          return <TextBlock key={key} text={part.text} isUser={isUser} peak={peak} />;
        }
        if (part.type === "tool-call") {
          const tc = part as {
            type: "tool-call";
            toolCallId?: string;
            toolName: string;
            args?: Record<string, unknown>;
            result?: string;
          };
          // For tool-call, toolCallId is preferred as the stable identifier; fall back to index if absent.
          // The "tool:" prefix prevents key collisions when text↔tool swap at the same index.
          const key = tc.toolCallId ? `tool:${tc.toolCallId}` : `tool:${i}`;
          return (
            <ToolCallRow key={key} toolName={tc.toolName} args={tc.args ?? {}} result={tc.result} />
          );
        }
        return null;
      })}
    </div>
  );
}
