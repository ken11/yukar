/**
 * lib/assistant-ui/runtime.ts
 *
 * Single module encapsulating the SSE → assistant-ui mapping.
 * - Strands message (GET …/threads/{t}) → converted to ThreadMessageLike
 * - SSE token / tool_call / tool_result events → patch the messages array
 * - POST …/threads/{t}/messages (HITL) executed via onNew
 *
 * Conforms to architecture.md §3.3 "close the message-part mapping in one module for testing".
 */

import type {
  ExternalStoreAdapter,
  ThreadMessageLike,
  ToolCallMessagePart,
} from "@assistant-ui/react";
import type { Message, ToolCallEvent, ToolResultEvent } from "@/lib/api/endpoints";

// ---- Strands → ThreadMessageLike ----

/**
 * Converts a Strands Message list (response from GET …/threads/{t})
 * into the ThreadMessageLike[] format required by assistant-ui.
 *
 * Pass1: Scans toolResult across all messages and builds a Map of toolUseId → result text.
 * Pass2: Converts each message.
 *   - text part → { type:"text", text }
 *   - toolUse part → { type:"tool-call", ... } (result filled in from the Pass1 Map)
 *   - Messages with only toolResult → not emitted (result has been folded into the toolUse side)
 *
 * Conforms to architecture.md §3.3.
 */
export function strandsMessagesToThreadMessageLikes(messages: Message[]): ThreadMessageLike[] {
  // Pass1: collect toolResults and build a Map of toolUseId → result text
  const resultById = new Map<string, string>();
  for (const msg of messages) {
    for (const part of msg.message.content) {
      if (part.toolResult) {
        resultById.set(part.toolResult.toolUseId, part.toolResult.text ?? "");
      }
    }
  }

  // Pass2: convert each message
  const result: ThreadMessageLike[] = [];
  for (const msg of messages) {
    const parts: ({ type: "text"; text: string } | ToolCallMessagePart)[] = [];

    for (const part of msg.message.content) {
      if (part.toolResult) {
        // Already folded into the toolUse side, so do not render standalone
        continue;
      }
      if (part.toolUse) {
        const tc = part.toolUse;
        parts.push({
          type: "tool-call" as const,
          toolCallId: tc.toolUseId,
          toolName: tc.name,
          // biome-ignore lint/suspicious/noExplicitAny: ReadonlyJSONObject cast
          args: (tc.input ?? {}) as any,
          argsText: JSON.stringify(tc.input ?? {}, null, 2),
          result: resultById.get(tc.toolUseId),
        });
      } else if (part.text) {
        parts.push({ type: "text" as const, text: part.text });
      }
    }

    // Skip messages that consist only of toolResult (parts is empty)
    if (parts.length === 0) continue;

    result.push({
      id: String(msg.message_id),
      role: msg.message.role === "user" ? "user" : "assistant",
      content: parts,
      createdAt: msg.created_at ? new Date(msg.created_at) : undefined,
    });
  }
  return result;
}

// ---- Building assistant messages during streaming ----

/** Intermediate format for accumulating tool calls received via SSE */
export interface PendingToolCall {
  toolCallId: string;
  toolName: string;
  // Restricted to JSON-serialized values for compatibility with assistant-ui ReadonlyJSONObject
  args: Record<string, unknown>;
  result?: string;
}

/** Intermediate stream state per utterance (per msg_index) */
export interface StreamSegment {
  msgIndex: number;
  tokenBuffer: string;
  toolCalls: PendingToolCall[];
}

/**
 * Stream state. Managed with useState inside useYukarThread.
 * segments is an array of utterance segments sorted in ascending msg_index order.
 */
export interface StreamState {
  segments: StreamSegment[];
  /** Whether the stream has finished (set to true by worker_completed / run_completed) */
  done: boolean;
}

/** Factory that creates a StreamState */
function makeStreamState(segments: StreamSegment[], done: boolean): StreamState {
  return { segments, done };
}

export function emptyStreamState(): StreamState {
  return makeStreamState([], false);
}

/**
 * A StreamState used to clear the live buffer after a turn completes.
 * segments is empty so streamStateToThreadMessageLikes returns [] (nothing displayed),
 * but setting done=true activates the "CLEAR_LIVE_BUFFER after REST authoritative data arrives"
 * guard in thread-page-client, structurally preventing double rendering.
 */
export function clearedStreamState(): StreamState {
  return makeStreamState([], true);
}

/** Finds the segment for msg_index. If absent, adds a new one preserving ascending msgIndex order and returns it (pure function). */
function ensureSegment(segments: StreamSegment[], msgIndex: number): StreamSegment[] {
  if (segments.some((s) => s.msgIndex === msgIndex)) return segments;
  const newSeg: StreamSegment = { msgIndex, tokenBuffer: "", toolCalls: [] };
  const inserted = [...segments, newSeg].sort((a, b) => a.msgIndex - b.msgIndex);
  return inserted;
}

/** Applies a token event to StreamState (pure function). msgIndex is the segment key (default 0). */
export function applyTokenEvent(state: StreamState, delta: string, msgIndex = 0): StreamState {
  const segs = ensureSegment(state.segments, msgIndex);
  const newSegs = segs.map((s) =>
    s.msgIndex === msgIndex ? { ...s, tokenBuffer: s.tokenBuffer + delta } : s,
  );
  return makeStreamState(newSegs, state.done);
}

/** Applies a tool_call event to StreamState (pure function). */
export function applyToolCallEvent(state: StreamState, ev: ToolCallEvent): StreamState {
  const mi = ev.msg_index ?? 0;

  // Use the tool_use_id assigned by the backend as the toolCallId.
  // Fall back to tool_name + total toolCalls across all segments only when the value is an empty string.
  const totalToolCalls = state.segments.reduce((n, s) => n + s.toolCalls.length, 0);
  const toolCallId = ev.tool_use_id !== "" ? ev.tool_use_id : `${ev.tool_name}-${totalToolCalls}`;

  // Mn5: Prevent reconnection backfill duplicates — skip if the same toolCallId already exists across all segments
  if (state.segments.some((s) => s.toolCalls.some((tc) => tc.toolCallId === toolCallId))) {
    return state;
  }

  const segs = ensureSegment(state.segments, mi);
  const newSegs = segs.map((s) => {
    if (s.msgIndex !== mi) return s;
    const pending: PendingToolCall = {
      toolCallId,
      toolName: ev.tool_name,
      args: (ev.tool_input as Record<string, unknown>) ?? {},
    };
    return { ...s, toolCalls: [...s.toolCalls, pending] };
  });
  return makeStreamState(newSegs, state.done);
}

/** Applies a tool_result event to StreamState (pure function). */
export function applyToolResultEvent(state: StreamState, ev: ToolResultEvent): StreamState {
  // Match by id when tool_use_id is valid. Fall back to tool_name only when the value is an empty string.
  // Do not route by msg_index (the backend emits toolResult at the next index from the caller).
  const useId = ev.tool_use_id !== "";
  const newSegs = state.segments.map((seg) => {
    const updated = seg.toolCalls.map((tc) => {
      if (tc.result !== undefined) return tc;
      const match = useId ? tc.toolCallId === ev.tool_use_id : tc.toolName === ev.tool_name;
      return match ? { ...tc, result: ev.result } : tc;
    });
    return { ...seg, toolCalls: updated };
  });
  return makeStreamState(newSegs, state.done);
}

/** Applies stream completion to StreamState (pure function). */
export function applyStreamDone(state: StreamState): StreamState {
  return makeStreamState(state.segments, true);
}

// ---- StreamState → ThreadMessageLike conversion ----

/**
 * Converts StreamState into ThreadMessageLike[] per utterance (per msg_index).
 * Scans each segment in ascending msgIndex order and bubbles up only those with
 * a non-empty tokenBuffer or toolCalls.
 * Text parts come first, tool parts after (natural utterance order).
 * Status is { type: "running" } while streaming.
 */
export function streamStateToThreadMessageLikes(state: StreamState): ThreadMessageLike[] {
  const result: ThreadMessageLike[] = [];
  for (const seg of state.segments) {
    if (seg.tokenBuffer === "" && seg.toolCalls.length === 0) continue;

    const textParts: { type: "text"; text: string }[] = seg.tokenBuffer
      ? [{ type: "text" as const, text: seg.tokenBuffer }]
      : [];

    const toolParts: ToolCallMessagePart[] = seg.toolCalls.map((tc) => ({
      type: "tool-call" as const,
      toolCallId: tc.toolCallId,
      toolName: tc.toolName,
      // biome-ignore lint/suspicious/noExplicitAny: ReadonlyJSONObject cast from JSON-safe tool_input
      args: tc.args as any,
      result: tc.result,
      argsText: JSON.stringify(tc.args, null, 2),
    }));

    result.push({
      id: `__streaming_${seg.msgIndex}__`,
      role: "assistant",
      content: [...textParts, ...toolParts],
      status: { type: "running" },
    });
  }
  return result;
}

// ---- StreamState helpers (used by thread-chat-inner) ----

/** Total tokenBuffer.length across all segments */
export function streamStateTextLength(state: StreamState): number {
  return state.segments.reduce((n, s) => n + s.tokenBuffer.length, 0);
}

/** True if all segments have tokenBuffer === "" and toolCalls.length === 0 (or segments is empty). */
export function streamStateIsEmpty(state: StreamState): boolean {
  if (state.segments.length === 0) return true;
  return state.segments.every((s) => s.tokenBuffer === "" && s.toolCalls.length === 0);
}

// ---- Factory that builds an ExternalStoreAdapter ----

export interface YukarThreadAdapterOptions {
  /** Existing messages fetched from Strands (initial value) */
  messages: readonly ThreadMessageLike[];
  /** State during streaming. Appended to the end if StreamState is non-empty. */
  streamState: StreamState;
  /** Whether streaming is in progress (passed to isRunning) */
  isRunning: boolean;
  /** Send a message (HITL) */
  onSendMessage: (content: string) => Promise<void>;
}

/**
 * Generates the adapter passed to assistant-ui's useExternalStoreRuntime.
 * Streaming messages (streamState) are appended to the end of messages as per-utterance bubbles,
 * and handed off to normal messages (messages update) after completion.
 *
 * #fix3 Double-render prevention: when streamState.done=true (clearedStreamState after turn completion),
 * stream bubbles are not concatenated even before REST authoritative data has arrived. done=true is set by
 * MANAGER_MESSAGE/WORKER_COMPLETED/EVAL_RESULT and fully cleared by CLEAR_LIVE_BUFFER after REST refetch
 * completes. Not emitting bubbles during this interval prevents double rendering.
 */
export function buildYukarAdapter(
  opts: YukarThreadAdapterOptions,
): ExternalStoreAdapter<ThreadMessageLike> {
  const { messages, streamState, isRunning, onSendMessage } = opts;

  // Do not concatenate stream bubbles after turn completion (done=true).
  // Explicitly guarding done=true here ensures structural prevention of double rendering.
  // P3: no synthetic "__awaiting__" bubble any more — the agent's question is
  // its final (persisted) assistant message and renders like any other.
  let allMessages: readonly ThreadMessageLike[];
  if (streamState.done) {
    allMessages = messages;
  } else {
    const segs = streamStateToThreadMessageLikes(streamState);
    allMessages = segs.length > 0 ? [...messages, ...segs] : messages;
  }

  return {
    isRunning,
    messages: allMessages,
    onNew: async (appendMsg) => {
      const text = appendMsg.content
        .filter((p): p is { type: "text"; text: string } => p.type === "text")
        .map((p) => p.text)
        .join("");
      if (text.trim()) {
        await onSendMessage(text.trim());
      }
    },
    convertMessage: (msg: ThreadMessageLike) => msg,
  };
}
