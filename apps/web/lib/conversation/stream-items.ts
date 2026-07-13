/**
 * lib/conversation/stream-items.ts
 *
 * Turns the flat ThreadMessageLike[] into the render plan for the
 * conversation stream (the "紙面" skeleton):
 *
 *  - Consecutive assistant messages that contain ONLY tool-calls of the SAME
 *    tool name collapse into one `tool-run` item ("task_update ×3").
 *    Mixed-name or text-bearing messages are never merged — one bubble per
 *    utterance stays the rule (smoke E2E asserts distinct bubbles for
 *    task_update → dispatch → report).
 *  - `grouped` marks items that continue the previous item's role, so the
 *    attribution header renders once per role run.
 *  - `turnStart` marks user messages that begin a new human turn (a horizon
 *    rule is drawn above them, except at the very top).
 *  - `settled` marks everything before the latest human turn — the history
 *    that has been read and may recede (opacity, restored on hover).
 */

import type { ThreadMessageLike } from "@assistant-ui/react";

type ToolCallPart = {
  type: "tool-call";
  toolCallId?: string;
  toolName: string;
  args?: Record<string, unknown>;
  result?: string;
};

export type StreamItem =
  | {
      kind: "message";
      msg: ThreadMessageLike;
      /** Continues the previous item's role — hide the attribution header. */
      grouped: boolean;
      /** First message of a new human turn (horizon above, unless first item). */
      turnStart: boolean;
      /** Belongs to a turn before the latest human turn. */
      settled: boolean;
    }
  | {
      kind: "tool-run";
      /** All tool-call parts of the run, in order. */
      calls: ToolCallPart[];
      toolName: string;
      /** Stable key: first tool call id / message id. */
      id: string;
      /** Timestamp of the first message in the run (for the attribution header). */
      createdAt?: Date;
      grouped: boolean;
      settled: boolean;
    };

/** True when every content part of the message is a tool-call (and there is at least one). */
function isToolOnly(msg: ThreadMessageLike): readonly ToolCallPart[] | null {
  if (msg.role !== "assistant" || typeof msg.content === "string") return null;
  const parts = msg.content as ReadonlyArray<{ type: string }>;
  if (parts.length === 0) return null;
  if (!parts.every((p) => p.type === "tool-call")) return null;
  const calls = parts as readonly ToolCallPart[];
  // A single message may itself carry differently-named calls — only collapse
  // uniform runs so distinct actions stay distinct bubbles.
  const name = calls[0].toolName;
  if (!calls.every((c) => c.toolName === name)) return null;
  return calls;
}

export function buildStreamItems(messages: readonly ThreadMessageLike[]): StreamItem[] {
  // Index of the last user message — everything strictly before it is settled.
  let lastUserIdx = -1;
  messages.forEach((m, i) => {
    if (m.role === "user") lastUserIdx = i;
  });

  const items: StreamItem[] = [];
  let prevRole: string | null = null;

  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i];
    const settled = lastUserIdx > 0 && i < lastUserIdx;
    const calls = isToolOnly(msg);

    if (calls) {
      const prev = items[items.length - 1];
      if (prev && prev.kind === "tool-run" && prev.toolName === calls[0].toolName) {
        // Dedupe by toolCallId: while streaming, the SSE-patched buffer and the
        // refetched persisted message can transiently carry the same call.
        const seen = new Set(prev.calls.map((c) => c.toolCallId));
        prev.calls.push(...calls.filter((c) => !c.toolCallId || !seen.has(c.toolCallId)));
        prev.settled = prev.settled && settled;
        prevRole = "assistant";
        continue;
      }
      items.push({
        kind: "tool-run",
        calls: [...calls],
        toolName: calls[0].toolName,
        id: String(calls[0].toolCallId ?? msg.id ?? i),
        createdAt: msg.createdAt,
        grouped: prevRole === "assistant",
        settled,
      });
      prevRole = "assistant";
      continue;
    }

    items.push({
      kind: "message",
      msg,
      grouped: prevRole === msg.role,
      turnStart: msg.role === "user",
      settled,
    });
    prevRole = msg.role;
  }

  return items;
}
