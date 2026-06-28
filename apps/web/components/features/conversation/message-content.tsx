"use client";

import { DocsFoldSection, splitDocsSections, ToolCallRow } from "./docs-fold";

// ---------------------------------------------------------------------------
// Message content renderer
// ---------------------------------------------------------------------------

export function MessageContent({
  content,
}: {
  content: import("@assistant-ui/react").ThreadMessageLike["content"];
}) {
  if (typeof content === "string") {
    return (
      <p className="text-body-md leading-[var(--leading-prose,1.6)] text-on-surface">{content}</p>
    );
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
                  return (
                    <p
                      key={sKey}
                      className="text-body-md leading-[var(--leading-prose,1.6)] text-on-surface whitespace-pre-wrap"
                    >
                      {s.body}
                    </p>
                  );
                })}
              </div>
            );
          }
          return (
            <p
              key={key}
              className="text-body-md leading-[var(--leading-prose,1.6)] text-on-surface whitespace-pre-wrap"
            >
              {part.text}
            </p>
          );
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
