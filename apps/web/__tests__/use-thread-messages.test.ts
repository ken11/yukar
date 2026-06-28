/**
 * Basic unit tests for the useThreadMessages hook
 *
 * Tests the hook that had 0% coverage before the G10 (thread-messages-invalidate) fix,
 * using renderHook + QueryClient.
 *
 * Testing approach:
 *   - getThreadMessages / postMessage are stubbed with vi.mock
 *   - React rendering goes through QueryClientProvider wrapper
 *   - No dependency on SSE / real network
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Message } from "../lib/api/endpoints";
import { queryKeys } from "../lib/api/query-keys";
import { useThreadMessages } from "../lib/hooks/use-thread-messages";

// ---------------------------------------------------------------------------
// Mock endpoints
// ---------------------------------------------------------------------------

vi.mock("../lib/api/endpoints", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../lib/api/endpoints")>();
  return {
    ...mod,
    getThreadMessages: vi.fn(),
    postMessage: vi.fn(),
  };
});

import { getThreadMessages, postMessage } from "../lib/api/endpoints";

const mockGetThreadMessages = vi.mocked(getThreadMessages);
const mockPostMessage = vi.mocked(postMessage);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const P = "proj1";
const E = "epic1";
const T = "thread1";

function makeMsg(id: number, text: string): Message {
  return {
    message_id: id,
    created_at: "2024-01-01T00:00:00Z",
    message: { role: "user", content: [{ text }] },
  };
}

function makeQC() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
}

function makeWrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: qc }, children);
  };
}

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------

let qc: QueryClient;

beforeEach(() => {
  qc = makeQC();
  vi.clearAllMocks();
});

afterEach(() => {
  qc.clear();
});

// ---------------------------------------------------------------------------
// 1. Initial render: initialMessages is returned as-is
// ---------------------------------------------------------------------------

describe("initial render", () => {
  it("initialMessages is returned as messages", () => {
    const initial = [makeMsg(1, "hello")];
    mockGetThreadMessages.mockResolvedValue(initial);

    const { result } = renderHook(
      () => useThreadMessages({ projectId: P, epicId: E, threadId: T, initialMessages: initial }),
      { wrapper: makeWrapper(qc) },
    );

    // The message is still present after strandsMessagesToThreadMessageLikes conversion
    expect(result.current.messages).toHaveLength(1);
  });

  it("isSending is false in the initial state", () => {
    const initial: Message[] = [];
    mockGetThreadMessages.mockResolvedValue(initial);

    const { result } = renderHook(
      () => useThreadMessages({ projectId: P, epicId: E, threadId: T, initialMessages: initial }),
      { wrapper: makeWrapper(qc) },
    );

    expect(result.current.isSending).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 2. sendMessage: POST is called and isSending becomes true
// ---------------------------------------------------------------------------

describe("sendMessage", () => {
  it("calling sendMessage calls postMessage with the correct arguments", async () => {
    const initial = [makeMsg(1, "hi")];
    const serverResponse = makeMsg(2, "reply");
    mockGetThreadMessages.mockResolvedValue(initial);
    mockPostMessage.mockResolvedValue(serverResponse);

    const { result } = renderHook(
      () => useThreadMessages({ projectId: P, epicId: E, threadId: T, initialMessages: initial }),
      { wrapper: makeWrapper(qc) },
    );

    await act(async () => {
      result.current.sendMessage("my message");
    });

    await waitFor(() => expect(result.current.isSending).toBe(false));

    expect(mockPostMessage).toHaveBeenCalledWith(P, E, T, {
      content: "my message",
      role: "user",
    });
  });

  it("isSending is true while sendMessage is in progress", async () => {
    const initial = [makeMsg(1, "init")];
    let resolveSend!: (v: Message) => void;
    const pendingPromise = new Promise<Message>((resolve) => {
      resolveSend = resolve;
    });
    mockGetThreadMessages.mockResolvedValue(initial);
    mockPostMessage.mockReturnValue(pendingPromise);

    const { result } = renderHook(
      () => useThreadMessages({ projectId: P, epicId: E, threadId: T, initialMessages: initial }),
      { wrapper: makeWrapper(qc) },
    );

    act(() => {
      result.current.sendMessage("pending");
    });

    // Sending
    await waitFor(() => expect(result.current.isSending).toBe(true));

    // Resolve it
    await act(async () => {
      resolveSend(makeMsg(2, "done"));
    });

    await waitFor(() => expect(result.current.isSending).toBe(false));
  });
});

// ---------------------------------------------------------------------------
// 3. onSuccess behavior: the core of the G10 fix
//    After switching from invalidateQueries → dedup setQueryData,
//    confirm that the cache is not invalidated
// ---------------------------------------------------------------------------

describe("onSuccess: setQueryData dedup merge", () => {
  it("POST response message is added to the cache after sendMessage succeeds", async () => {
    const m1 = makeMsg(1, "initial");
    const m2 = makeMsg(2, "from-server");
    mockGetThreadMessages.mockResolvedValue([m1]);
    mockPostMessage.mockResolvedValue(m2);

    qc.setQueryData(queryKeys.threads.messages(P, E, T), [m1]);

    const { result } = renderHook(
      () => useThreadMessages({ projectId: P, epicId: E, threadId: T, initialMessages: [m1] }),
      { wrapper: makeWrapper(qc) },
    );

    await act(async () => {
      result.current.sendMessage("hi");
    });

    await waitFor(() => expect(result.current.isSending).toBe(false));

    // After fix: POST response m2 is merged into the cache
    const cached = qc.getQueryData<Message[]>(queryKeys.threads.messages(P, E, T));
    expect(cached?.map((m) => m.message_id)).toContain(2);
  });

  it("cache is not invalidated after onSuccess (resolves refetch race)", async () => {
    const m1 = makeMsg(1, "initial");
    const m2 = makeMsg(2, "from-server");
    mockGetThreadMessages.mockResolvedValue([m1]);
    mockPostMessage.mockResolvedValue(m2);

    qc.setQueryData(queryKeys.threads.messages(P, E, T), [m1]);

    const { result } = renderHook(
      () => useThreadMessages({ projectId: P, epicId: E, threadId: T, initialMessages: [m1] }),
      { wrapper: makeWrapper(qc) },
    );

    await act(async () => {
      result.current.sendMessage("hi");
    });

    await waitFor(() => expect(result.current.isSending).toBe(false));

    // After fix: invalidateQueries is not called so isInvalidated stays false
    const state = qc.getQueryState(queryKeys.threads.messages(P, E, T));
    expect(state?.isInvalidated).toBe(false);
  });

  it("messages with the same message_id are not added twice (dedup)", async () => {
    const m1 = makeMsg(1, "initial");
    const m2 = makeMsg(2, "new-msg");
    // Scenario where m2 was already added to the cache by SSE patch
    mockGetThreadMessages.mockResolvedValue([m1]);
    mockPostMessage.mockResolvedValue(m2);

    // m2 was added first via SSE patch
    qc.setQueryData(queryKeys.threads.messages(P, E, T), [m1, m2]);

    const { result } = renderHook(
      () => useThreadMessages({ projectId: P, epicId: E, threadId: T, initialMessages: [m1, m2] }),
      { wrapper: makeWrapper(qc) },
    );

    await act(async () => {
      result.current.sendMessage("same message");
    });

    await waitFor(() => expect(result.current.isSending).toBe(false));

    // dedup: m2 appears only once
    const cached = qc.getQueryData<Message[]>(queryKeys.threads.messages(P, E, T));
    expect(cached?.filter((m) => m.message_id === 2)).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// 4. queryFn: TanStack Query refetch updates the cache
// ---------------------------------------------------------------------------

describe("cache update via queryFn", () => {
  it("cache is populated with the latest data when getThreadMessages succeeds", async () => {
    const initial = [makeMsg(1, "initial")];
    const refreshed = [makeMsg(1, "initial"), makeMsg(2, "refreshed")];
    mockGetThreadMessages.mockResolvedValue(refreshed);

    const { result } = renderHook(
      () => useThreadMessages({ projectId: P, epicId: E, threadId: T, initialMessages: initial }),
      { wrapper: makeWrapper(qc) },
    );

    // Wait for queryFn to run
    await waitFor(() =>
      expect(qc.getQueryData(queryKeys.threads.messages(P, E, T))).toEqual(refreshed),
    );

    expect(result.current.messages).toHaveLength(2);
  });
});

// ---------------------------------------------------------------------------
// G10 regression test: duplicate bubble caused by synthetic ack (message_id=-1) on manager path
//   When POST returns message_id=-1, it must NOT be added to the cache.
//   Adding -1 first would cause duplication since the following SSE patch appends the real id.
// ---------------------------------------------------------------------------

describe("G10: manager path — synthetic ack (message_id=-1) is not added to cache", () => {
  it("POST returning message_id=-1 is not added to the cache", async () => {
    const m1 = makeMsg(1, "initial");
    // The manager thread returns a synthetic ack (message_id=-1)
    const syntheticAck = makeMsg(-1, "ack");
    mockGetThreadMessages.mockResolvedValue([m1]);
    mockPostMessage.mockResolvedValue(syntheticAck);

    qc.setQueryData(queryKeys.threads.messages(P, E, T), [m1]);

    const { result } = renderHook(
      () => useThreadMessages({ projectId: P, epicId: E, threadId: T, initialMessages: [m1] }),
      { wrapper: makeWrapper(qc) },
    );

    await act(async () => {
      result.current.sendMessage("hello");
    });

    await waitFor(() => expect(result.current.isSending).toBe(false));

    // Synthetic ack is not added to the cache
    const cached = qc.getQueryData<Message[]>(queryKeys.threads.messages(P, E, T));
    expect(cached?.some((m) => m.message_id === -1)).toBe(false);
  });

  it("POST message_id=-1 → subsequent SSE patch (real id=5) → no duplicate", async () => {
    const m1 = makeMsg(1, "initial");
    const syntheticAck = makeMsg(-1, "ack");
    const realMsg = makeMsg(5, "from-sse");
    mockGetThreadMessages.mockResolvedValue([m1]);
    mockPostMessage.mockResolvedValue(syntheticAck);

    qc.setQueryData(queryKeys.threads.messages(P, E, T), [m1]);

    const { result } = renderHook(
      () => useThreadMessages({ projectId: P, epicId: E, threadId: T, initialMessages: [m1] }),
      { wrapper: makeWrapper(qc) },
    );

    await act(async () => {
      result.current.sendMessage("hello");
    });

    await waitFor(() => expect(result.current.isSending).toBe(false));

    // Simulate SSE patch appending a message with the real id
    qc.setQueryData(queryKeys.threads.messages(P, E, T), (prev: Message[] | undefined) => {
      if (!prev) return [realMsg];
      if (prev.some((m) => m.message_id === realMsg.message_id)) return prev;
      return [...prev, realMsg];
    });

    const cached = qc.getQueryData<Message[]>(queryKeys.threads.messages(P, E, T));
    // -1 does not exist
    expect(cached?.some((m) => m.message_id === -1)).toBe(false);
    // Real id exists exactly once
    expect(cached?.filter((m) => m.message_id === 5)).toHaveLength(1);
  });
});

describe("G10: non-manager path — real id is merged into cache", () => {
  it("POST returning message_id=3 (positive real id) is added to the cache", async () => {
    const m1 = makeMsg(1, "initial");
    const realMsg = makeMsg(3, "from-server");
    mockGetThreadMessages.mockResolvedValue([m1]);
    mockPostMessage.mockResolvedValue(realMsg);

    qc.setQueryData(queryKeys.threads.messages(P, E, T), [m1]);

    const { result } = renderHook(
      () => useThreadMessages({ projectId: P, epicId: E, threadId: T, initialMessages: [m1] }),
      { wrapper: makeWrapper(qc) },
    );

    await act(async () => {
      result.current.sendMessage("hi");
    });

    await waitFor(() => expect(result.current.isSending).toBe(false));

    const cached = qc.getQueryData<Message[]>(queryKeys.threads.messages(P, E, T));
    // Real id is added
    expect(cached?.some((m) => m.message_id === 3)).toBe(true);
    // -1 does not exist
    expect(cached?.some((m) => m.message_id === -1)).toBe(false);
  });
});
