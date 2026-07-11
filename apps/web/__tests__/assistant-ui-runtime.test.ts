/**
 * Unit tests for runtime mapping in lib/assistant-ui/runtime.ts
 * architecture.md §3.3 "Confine message-part mapping to one module and test it"
 */

import { describe, expect, it } from "vitest";
import type { Message } from "../lib/api/endpoints";
import {
  applyStreamDone,
  applyTokenEvent,
  applyToolCallEvent,
  applyToolResultEvent,
  buildYukarAdapter,
  clearedStreamState,
  emptyStreamState,
  strandsMessagesToThreadMessageLikes,
  streamStateIsEmpty,
  streamStateTextLength,
  streamStateToThreadMessageLikes,
} from "../lib/assistant-ui/runtime";

// ---- strandsMessagesToThreadMessageLikes ----

describe("strandsMessagesToThreadMessageLikes", () => {
  function makeMsg(
    id: number,
    role: "user" | "assistant",
    content: Message["message"]["content"],
    createdAt?: string,
  ): Message {
    return {
      message_id: id,
      created_at: createdAt ?? "2026-06-15T00:00:00Z",
      message: { role, content },
    };
  }

  it("converts a text-only message correctly", () => {
    const msgs = [makeMsg(1, "user", [{ text: "hello" }])];
    const result = strandsMessagesToThreadMessageLikes(msgs);
    expect(result).toHaveLength(1);
    expect(result[0].role).toBe("user");
    expect(result[0].content).toEqual([{ type: "text", text: "hello" }]);
    expect(result[0].id).toBe("1");
  });

  it("converts a toolUse part to tool-call and fills result from resultById", () => {
    const msgs = [
      // assistant: toolUse
      makeMsg(1, "assistant", [
        { toolUse: { toolUseId: "tu-1", name: "fs_read", input: { path: "/foo" } } },
      ]),
      // user: toolResult (this standalone message is not emitted)
      makeMsg(2, "user", [
        { toolResult: { toolUseId: "tu-1", text: "file contents", status: "ok" } },
      ]),
    ];
    const result = strandsMessagesToThreadMessageLikes(msgs);
    // message with only toolResult (id=2) is not emitted
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe("1");
    expect(result[0].role).toBe("assistant");
    const content = result[0].content as unknown as {
      type: string;
      toolCallId?: string;
      toolName?: string;
      result?: string;
    }[];
    const toolPart = content.find((p) => p.type === "tool-call");
    expect(toolPart).toBeDefined();
    expect(toolPart?.toolCallId).toBe("tu-1");
    expect(toolPart?.toolName).toBe("fs_read");
    expect(toolPart?.result).toBe("file contents");
  });

  it("messages consisting only of toolResult are not emitted", () => {
    const msgs = [makeMsg(1, "user", [{ toolResult: { toolUseId: "tu-1", text: "result" } }])];
    const result = strandsMessagesToThreadMessageLikes(msgs);
    expect(result).toHaveLength(0);
  });

  it("emits both parts of a message that mixes text and toolUse", () => {
    const msgs = [
      makeMsg(1, "assistant", [
        { text: "thinking..." },
        { toolUse: { toolUseId: "tu-2", name: "git_status", input: {} } },
      ]),
      makeMsg(2, "user", [{ toolResult: { toolUseId: "tu-2", text: "clean" } }]),
    ];
    const result = strandsMessagesToThreadMessageLikes(msgs);
    expect(result).toHaveLength(1);
    const content = result[0].content as unknown as { type: string }[];
    expect(content.some((p) => p.type === "text")).toBe(true);
    expect(content.some((p) => p.type === "tool-call")).toBe(true);
  });

  it("correlates results precisely by toolUseId (multiple toolUse calls)", () => {
    const msgs = [
      makeMsg(1, "assistant", [
        { toolUse: { toolUseId: "tu-A", name: "fs_read", input: { path: "/a" } } },
        { toolUse: { toolUseId: "tu-B", name: "fs_read", input: { path: "/b" } } },
      ]),
      makeMsg(2, "user", [{ toolResult: { toolUseId: "tu-A", text: "content-a" } }]),
      makeMsg(3, "user", [{ toolResult: { toolUseId: "tu-B", text: "content-b" } }]),
    ];
    const result = strandsMessagesToThreadMessageLikes(msgs);
    expect(result).toHaveLength(1);
    const content = result[0].content as unknown as {
      type: string;
      toolCallId?: string;
      result?: string;
    }[];
    const partA = content.find((p) => p.toolCallId === "tu-A");
    const partB = content.find((p) => p.toolCallId === "tu-B");
    expect(partA?.result).toBe("content-a");
    expect(partB?.result).toBe("content-b");
  });

  it("toolUse with no result has result=undefined", () => {
    const msgs = [
      makeMsg(1, "assistant", [{ toolUse: { toolUseId: "tu-X", name: "fs_read", input: {} } }]),
    ];
    const result = strandsMessagesToThreadMessageLikes(msgs);
    expect(result).toHaveLength(1);
    const content = result[0].content as unknown as { type: string; result?: string }[];
    const toolPart = content.find((p) => p.type === "tool-call");
    expect(toolPart?.result).toBeUndefined();
  });

  it("returns an empty array when passed an empty list", () => {
    expect(strandsMessagesToThreadMessageLikes([])).toEqual([]);
  });
});

// ---- Applying SSE events to StreamState ----

describe("applyTokenEvent", () => {
  it("appends delta to the buffer", () => {
    const s0 = emptyStreamState();
    const s1 = applyTokenEvent(s0, "Hello");
    const s2 = applyTokenEvent(s1, " World");
    expect(s2.segments[0].tokenBuffer).toBe("Hello World");
  });

  it("does not mutate the original state (immutable)", () => {
    const s0 = emptyStreamState();
    applyTokenEvent(s0, "x");
    expect(streamStateTextLength(s0)).toBe(0);
  });
});

describe("applyToolCallEvent", () => {
  it("adds a tool_call event to toolCalls", () => {
    const s0 = emptyStreamState();
    const s1 = applyToolCallEvent(s0, {
      type: "tool_call",
      project_id: "p",
      epic_id: "e",
      run_id: "r",
      thread_id: "t",
      tool_name: "fs_read",
      tool_input: { path: "/foo" },
      tool_use_id: "tu-001",
      msg_index: 0,
    });
    expect(s1.segments[0].toolCalls).toHaveLength(1);
    expect(s1.segments[0].toolCalls[0].toolName).toBe("fs_read");
    expect(s1.segments[0].toolCalls[0].args).toEqual({ path: "/foo" });
    expect(s1.segments[0].toolCalls[0].result).toBeUndefined();
  });

  it("uses tool_use_id as toolCallId", () => {
    const s0 = emptyStreamState();
    const s1 = applyToolCallEvent(s0, {
      type: "tool_call",
      project_id: "p",
      epic_id: "e",
      run_id: "r",
      thread_id: "t",
      tool_name: "fs_read",
      tool_input: {},
      tool_use_id: "tu-abc",
      msg_index: 0,
    });
    expect(s1.segments[0].toolCalls[0].toolCallId).toBe("tu-abc");
  });

  it("falls back to name+index to ensure uniqueness when tool_use_id is empty", () => {
    const s0 = emptyStreamState();
    const s1 = applyToolCallEvent(s0, {
      type: "tool_call",
      project_id: "p",
      epic_id: "e",
      run_id: "r",
      thread_id: "t",
      tool_name: "fs_read",
      tool_input: {},
      tool_use_id: "",
      msg_index: 0,
    });
    const s2 = applyToolCallEvent(s1, {
      type: "tool_call",
      project_id: "p",
      epic_id: "e",
      run_id: "r",
      thread_id: "t",
      tool_name: "fs_read",
      tool_input: {},
      tool_use_id: "",
      msg_index: 0,
    });
    expect(s2.segments[0].toolCalls[0].toolCallId).not.toBe(s2.segments[0].toolCalls[1].toolCallId);
  });
});

describe("applyToolResultEvent", () => {
  it("sets result on the matching tool_call identified by tool_use_id", () => {
    const s0 = emptyStreamState();
    const s1 = applyToolCallEvent(s0, {
      type: "tool_call",
      project_id: "p",
      epic_id: "e",
      run_id: "r",
      thread_id: "t",
      tool_name: "fs_read",
      tool_input: {},
      tool_use_id: "tu-001",
      msg_index: 0,
    });
    const s2 = applyToolResultEvent(s1, {
      type: "tool_result",
      project_id: "p",
      epic_id: "e",
      run_id: "r",
      thread_id: "t",
      tool_name: "fs_read",
      result: "file contents",
      tool_use_id: "tu-001",
      msg_index: 0,
    });
    expect(s2.segments[0].toolCalls[0].result).toBe("file contents");
  });

  it("correctly correlates multiple calls to the same-named tool by tool_use_id", () => {
    let s = emptyStreamState();
    // Call the same-named tool twice
    s = applyToolCallEvent(s, {
      type: "tool_call",
      project_id: "p",
      epic_id: "e",
      run_id: "r",
      thread_id: "t",
      tool_name: "fs_read",
      tool_input: { path: "/a" },
      tool_use_id: "tu-001",
      msg_index: 0,
    });
    s = applyToolCallEvent(s, {
      type: "tool_call",
      project_id: "p",
      epic_id: "e",
      run_id: "r",
      thread_id: "t",
      tool_name: "fs_read",
      tool_input: { path: "/b" },
      tool_use_id: "tu-002",
      msg_index: 0,
    });
    // Assign result to the second call
    s = applyToolResultEvent(s, {
      type: "tool_result",
      project_id: "p",
      epic_id: "e",
      run_id: "r",
      thread_id: "t",
      tool_name: "fs_read",
      result: "content-b",
      tool_use_id: "tu-002",
      msg_index: 0,
    });
    // First call has no result yet; only the second call has a result
    expect(s.segments[0].toolCalls[0].result).toBeUndefined();
    expect(s.segments[0].toolCalls[1].result).toBe("content-b");
  });

  it("does not overwrite a result that is already set", () => {
    const s0 = emptyStreamState();
    const s1 = applyToolCallEvent(s0, {
      type: "tool_call",
      project_id: "p",
      epic_id: "e",
      run_id: "r",
      thread_id: "t",
      tool_name: "fs_read",
      tool_input: {},
      tool_use_id: "tu-001",
      msg_index: 0,
    });
    const s2 = applyToolResultEvent(s1, {
      type: "tool_result",
      project_id: "p",
      epic_id: "e",
      run_id: "r",
      thread_id: "t",
      tool_name: "fs_read",
      result: "first",
      tool_use_id: "tu-001",
      msg_index: 0,
    });
    const s3 = applyToolResultEvent(s2, {
      type: "tool_result",
      project_id: "p",
      epic_id: "e",
      run_id: "r",
      thread_id: "t",
      tool_name: "fs_read",
      result: "second",
      tool_use_id: "tu-001",
      msg_index: 0,
    });
    // The first result remains "first"
    expect(s3.segments[0].toolCalls[0].result).toBe("first");
  });

  it("falls back to tool_name matching when tool_use_id is empty string", () => {
    const s0 = emptyStreamState();
    const s1 = applyToolCallEvent(s0, {
      type: "tool_call",
      project_id: "p",
      epic_id: "e",
      run_id: "r",
      thread_id: "t",
      tool_name: "fs_read",
      tool_input: {},
      tool_use_id: "",
      msg_index: 0,
    });
    const s2 = applyToolResultEvent(s1, {
      type: "tool_result",
      project_id: "p",
      epic_id: "e",
      run_id: "r",
      thread_id: "t",
      tool_name: "fs_read",
      result: "fallback result",
      tool_use_id: "",
      msg_index: 0,
    });
    expect(s2.segments[0].toolCalls[0].result).toBe("fallback result");
  });
});

describe("applyStreamDone", () => {
  it("sets the done flag to true", () => {
    const s = applyStreamDone(emptyStreamState());
    expect(s.done).toBe(true);
  });
});

// ---- Mn5: Reconnection backfill deduplication ----

describe("Mn5: tool_call is not duplicated on reconnection backfill replay", () => {
  const makeToolCall = (id: string, name = "fs_read") => ({
    type: "tool_call" as const,
    project_id: "p",
    epic_id: "e",
    run_id: "r",
    thread_id: "t",
    tool_name: name,
    tool_input: { path: "/foo" },
    tool_use_id: id,
    msg_index: 0,
  });

  it("toolCalls stays at 1 entry even when the same toolCallId arrives twice", () => {
    let s = emptyStreamState();
    s = applyToolCallEvent(s, makeToolCall("tu-001"));
    // The same event is replayed again on reconnection backfill
    s = applyToolCallEvent(s, makeToolCall("tu-001"));

    expect(s.segments[0].toolCalls).toHaveLength(1);
    expect(s.segments[0].toolCalls[0].toolCallId).toBe("tu-001");
  });

  it("different toolCallIds are added as separate entries", () => {
    let s = emptyStreamState();
    s = applyToolCallEvent(s, makeToolCall("tu-001", "fs_read"));
    s = applyToolCallEvent(s, makeToolCall("tu-002", "git_status"));

    expect(s.segments[0].toolCalls).toHaveLength(2);
  });

  it("replay backfill: result is preserved after tool_call(tu-001) → tool_result(tu-001) → replay tool_call(tu-001)", () => {
    let s = emptyStreamState();
    s = applyToolCallEvent(s, makeToolCall("tu-001"));
    s = applyToolResultEvent(s, {
      type: "tool_result",
      project_id: "p",
      epic_id: "e",
      run_id: "r",
      thread_id: "t",
      tool_name: "fs_read",
      result: "original result",
      tool_use_id: "tu-001",
      msg_index: 0,
    });
    // Existing entry is skipped even when tool_call arrives again on backfill replay
    s = applyToolCallEvent(s, makeToolCall("tu-001"));

    expect(s.segments[0].toolCalls).toHaveLength(1);
    expect(s.segments[0].toolCalls[0].result).toBe("original result");
  });
});

// ---- buildYukarAdapter ----

describe("buildYukarAdapter", () => {
  it("returns allMessages combining messages and streamState", () => {
    const [msg1] = strandsMessagesToThreadMessageLikes([
      {
        message_id: 1,
        message: { role: "user", content: [{ text: "hi" }] },
      },
    ]);
    const streamState = applyTokenEvent(emptyStreamState(), "partial");
    const adapter = buildYukarAdapter({
      messages: [msg1],
      streamState,
      isRunning: true,
      onSendMessage: async () => {},
    });
    expect(adapter.messages).toHaveLength(2);
    expect(adapter.messages?.[1].id).toBe("__streaming_0__");
  });

  it("message count is unchanged when streamState is empty", () => {
    const [msg1] = strandsMessagesToThreadMessageLikes([
      {
        message_id: 1,
        message: { role: "user", content: [{ text: "hi" }] },
      },
    ]);
    const adapter = buildYukarAdapter({
      messages: [msg1],
      streamState: emptyStreamState(),
      isRunning: false,
      onSendMessage: async () => {},
    });
    expect(adapter.messages).toHaveLength(1);
  });

  it("passes text content to onSendMessage via onNew", async () => {
    let sent = "";
    const adapter = buildYukarAdapter({
      messages: [],
      streamState: emptyStreamState(),
      isRunning: false,
      onSendMessage: async (content) => {
        sent = content;
      },
    });
    await adapter.onNew({
      parentId: null,
      sourceId: null,
      runConfig: undefined,
      role: "user",
      content: [{ type: "text", text: "hello agent" }],
      attachments: [],
      metadata: { custom: {} },
      createdAt: new Date(),
    });
    expect(sent).toBe("hello agent");
  });

  it("P3: never appends a synthetic '__awaiting__' bubble — the question is the last persisted message", () => {
    const [msg1] = strandsMessagesToThreadMessageLikes([
      {
        message_id: 1,
        message: { role: "user", content: [{ text: "hi" }] },
      },
    ]);
    const adapter = buildYukarAdapter({
      messages: [msg1],
      streamState: emptyStreamState(),
      isRunning: false,
      onSendMessage: async () => {},
    });
    expect(adapter.messages).toHaveLength(1);
    expect(adapter.messages?.some((m) => m.id === "__awaiting__")).toBe(false);
  });

  it("P3: only the streaming bubble is concatenated while streaming — no synthetic tail", () => {
    const streamState = applyTokenEvent(emptyStreamState(), "generating...");
    const adapter = buildYukarAdapter({
      messages: [],
      streamState,
      isRunning: true,
      onSendMessage: async () => {},
    });
    expect(adapter.messages).toHaveLength(1);
    expect(adapter.messages?.[0].id).toBe("__streaming_0__");
  });
});

// ---- #fix3: preventing double-display via clearedStreamState (done=true) ----

describe("clearedStreamState", () => {
  it("done=true, tokenBuffer and toolCalls are empty", () => {
    const s = clearedStreamState();
    expect(s.done).toBe(true);
    expect(streamStateTextLength(s)).toBe(0);
    expect(streamStateIsEmpty(s)).toBe(true);
  });

  it("streamStateToThreadMessageLikes returns an empty array for clearedStreamState", () => {
    expect(streamStateToThreadMessageLikes(clearedStreamState())).toEqual([]);
  });
});

describe("#fix3: buildYukarAdapter — does not concatenate stream bubble when done=true", () => {
  it("does not add a stream bubble even when tokenBuffer has content and done=true (prevents double-display)", () => {
    // clearedStreamState() has an empty tokenBuffer, so construct done=true + non-empty tokenBuffer manually
    const doneWithContent = applyStreamDone(applyTokenEvent(emptyStreamState(), "finalized text"));
    const [restMsg] = strandsMessagesToThreadMessageLikes([
      {
        message_id: 10,
        message: { role: "assistant", content: [{ text: "finalized text" }] },
      },
    ]);
    const adapter = buildYukarAdapter({
      messages: [restMsg],
      streamState: doneWithContent,
      isRunning: false,
      onSendMessage: async () => {},
    });
    // done=true so no stream bubble is added — message count stays at 1
    expect(adapter.messages).toHaveLength(1);
    expect(adapter.messages?.[0].id).toBe("10");
  });

  it("adds a stream bubble when tokenBuffer has content and done=false (normal streaming)", () => {
    const streaming = applyTokenEvent(emptyStreamState(), "streaming...");
    const adapter = buildYukarAdapter({
      messages: [],
      streamState: streaming,
      isRunning: true,
      onSendMessage: async () => {},
    });
    expect(adapter.messages).toHaveLength(1);
    expect(adapter.messages?.[0].id).toBe("__streaming_0__");
  });

  it("clearedStreamState (done=true, empty) does not add a stream bubble", () => {
    const [restMsg] = strandsMessagesToThreadMessageLikes([
      {
        message_id: 5,
        message: { role: "assistant", content: [{ text: "completed message" }] },
      },
    ]);
    const adapter = buildYukarAdapter({
      messages: [restMsg],
      streamState: clearedStreamState(),
      isRunning: false,
      onSendMessage: async () => {},
    });
    // Only the REST authoritative message is displayed
    expect(adapter.messages).toHaveLength(1);
    expect(adapter.messages?.[0].id).toBe("5");
  });
});

// ---- Multi-turn re-stream: turn 1 completes → turn 2 emits a stream bubble again ----

describe("multi-turn re-stream (#multi-turn-regression fix verification)", () => {
  it("turn 2 stream bubble is rendered after turn 1 completes (done=true)", () => {
    // Turn 1: streaming → turn completes with clearedStreamState(done=true)
    const turn1Done = clearedStreamState(); // done=true, empty buffer

    // No display immediately after turn 1 completes in buildYukarAdapter
    const adapterAfterTurn1 = buildYukarAdapter({
      messages: [],
      streamState: turn1Done,
      isRunning: false,
      onSendMessage: async () => {},
    });
    expect(adapterAfterTurn1.messages).toHaveLength(0);

    // Turn 2: state after reset to emptyStreamState(done=false) and a token arrives
    const turn2State = applyTokenEvent(emptyStreamState(), "turn 2 text");
    expect(turn2State.done).toBe(false);

    const adapterTurn2 = buildYukarAdapter({
      messages: [],
      streamState: turn2State,
      isRunning: true,
      onSendMessage: async () => {},
    });
    // Turn 2 stream bubble should be displayed
    expect(adapterTurn2.messages).toHaveLength(1);
    expect(adapterTurn2.messages?.[0].id).toBe("__streaming_0__");
    expect(adapterTurn2.messages?.[0].status).toEqual({ type: "running" });
  });

  it("turn 2 bubble disappears if done=false reset is missing (regression reproduction)", () => {
    // Even when TOKEN arrives while done=true, buildYukarAdapter does not emit a bubble
    const stillDone = applyTokenEvent(clearedStreamState(), "turn 2 text (reset forgotten)");
    // applyTokenEvent does not change the done flag
    expect(stillDone.done).toBe(true);
    expect(stillDone.segments[0].tokenBuffer).toBe("turn 2 text (reset forgotten)");

    const adapter = buildYukarAdapter({
      messages: [],
      streamState: stillDone,
      isRunning: true,
      onSendMessage: async () => {},
    });
    // Stream bubble is suppressed when done=true remains (regression reproduction)
    expect(adapter.messages).toHaveLength(0);
  });
});

// ---- issue②: utterance segment splitting by msg_index (core behavior) ----

describe("issue②: msg_index utterance segment splitting", () => {
  const toolCall = (id: string, mi: number, name = "fs_read") => ({
    type: "tool_call" as const,
    project_id: "p",
    epic_id: "e",
    run_id: "r",
    thread_id: "t",
    tool_name: name,
    tool_input: { path: "/x" },
    tool_use_id: id,
    msg_index: mi,
  });
  const toolResult = (id: string, mi: number, result: string, name = "fs_read") => ({
    type: "tool_result" as const,
    project_id: "p",
    epic_id: "e",
    run_id: "r",
    thread_id: "t",
    tool_name: name,
    result,
    tool_use_id: id,
    msg_index: mi,
  });

  it("different msg_index values produce separate segments = separate bubbles", () => {
    let s = emptyStreamState();
    s = applyTokenEvent(s, "thinking", 0);
    s = applyTokenEvent(s, "done", 1);
    expect(s.segments).toHaveLength(2);
    const bubbles = streamStateToThreadMessageLikes(s);
    expect(bubbles.map((b) => b.id)).toEqual(["__streaming_0__", "__streaming_1__"]);
    expect(bubbles[0].content).toContainEqual({ type: "text", text: "thinking" });
    expect(bubbles[1].content).toContainEqual({ type: "text", text: "done" });
  });

  it("deltas with the same msg_index are concatenated into the same segment (1 bubble)", () => {
    let s = emptyStreamState();
    s = applyTokenEvent(s, "Hel", 0);
    s = applyTokenEvent(s, "lo", 0);
    expect(s.segments).toHaveLength(1);
    expect(s.segments[0].tokenBuffer).toBe("Hello");
    expect(streamStateToThreadMessageLikes(s)).toHaveLength(1);
  });

  it("segments are in ascending msgIndex order (even when they arrive out of order)", () => {
    let s = emptyStreamState();
    s = applyTokenEvent(s, "second", 1);
    s = applyTokenEvent(s, "first", 0);
    expect(s.segments.map((seg) => seg.msgIndex)).toEqual([0, 1]);
    expect(streamStateToThreadMessageLikes(s).map((b) => b.id)).toEqual([
      "__streaming_0__",
      "__streaming_1__",
    ]);
  });

  it("toolResult is linked to the calling segment by tool_use_id, not msg_index", () => {
    // The backend emits toolResult with the index of the message after the calling assistant message.
    // Even when toolCall=msg_index0 and toolResult=msg_index1, the frontend links by id to seg0.
    let s = emptyStreamState();
    s = applyTokenEvent(s, "calling A", 0);
    s = applyToolCallEvent(s, toolCall("tu-A", 0));
    s = applyToolResultEvent(s, toolResult("tu-A", 1, "contents of A"));
    s = applyTokenEvent(s, "got it", 1);

    const seg0 = s.segments.find((seg) => seg.msgIndex === 0);
    expect(seg0?.toolCalls).toHaveLength(1);
    expect(seg0?.toolCalls[0].toolCallId).toBe("tu-A");
    expect(seg0?.toolCalls[0].result).toBe("contents of A");
    // toolResult itself does not create an empty segment (msg_index1); seg1 is created by the "got it" token.
    expect(s.segments.map((seg) => seg.msgIndex)).toEqual([0, 1]);
    expect(streamStateToThreadMessageLikes(s)).toHaveLength(2);
  });

  it("empty segments / empty state are not converted to bubbles", () => {
    const s = emptyStreamState();
    expect(streamStateToThreadMessageLikes(s)).toEqual([]);
    expect(streamStateIsEmpty(s)).toBe(true);
    expect(streamStateTextLength(s)).toBe(0);
  });

  it("buildYukarAdapter adds all segments when !done and suppresses them when done", () => {
    let s = emptyStreamState();
    s = applyTokenEvent(s, "a", 0);
    s = applyTokenEvent(s, "b", 1);
    const live = buildYukarAdapter({
      messages: [],
      streamState: s,
      isRunning: true,
      onSendMessage: async () => {},
    });
    expect(live.messages).toHaveLength(2);
    const done = buildYukarAdapter({
      messages: [],
      streamState: clearedStreamState(),
      isRunning: false,
      onSendMessage: async () => {},
    });
    expect(done.messages).toHaveLength(0);
  });
});
