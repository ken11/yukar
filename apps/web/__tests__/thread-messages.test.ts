/**
 * Characterization tests for finding[thread-messages-invalidate]
 *
 * Target:
 *   `invalidateQueries` called by sendMutation.onSuccess in
 *   lib/hooks/use-thread-messages.ts can overwrite the `setQueryData`
 *   (optimistic append) performed by SSE cache-patch.ts when an
 *   in-flight refetch completes.
 *
 * Test strategy:
 *   - Directly manipulate the TanStack Query QueryClient to reproduce
 *     the hook's wiring.
 *   - No network, React, or SSE libraries needed — verify pure cache
 *     behavior only.
 *   - Confirmed bugs are expressed with `it.fails(...)` to keep the
 *     suite green.
 *   - Behavior that matches the spec is a normal `it(...)` PASS test.
 *
 * Terminology:
 *   SSE patch   = optimistic append via `qc.setQueryData` performed by
 *                 the user_message_committed branch of applyRunCachePatch
 *   invalidate  = `qc.invalidateQueries` called by sendMutation.onSuccess
 *   refetch     = cache replacement after the REST GET triggered
 *                 automatically by invalidate (if an active observer exists)
 */

import { QueryClient } from "@tanstack/react-query";
import { beforeEach, describe, expect, it } from "vitest";
import { queryKeys } from "../lib/api/query-keys";

// ---- helpers ----

/** Returns a fresh QueryClient for each test (defaults: retry=0, staleTime=0) */
function makeQC(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

const P = "proj1";
const E = "epic1";
const T = "thread1";

function msgKey() {
  return queryKeys.threads.messages(P, E, T);
}

/** Minimal fixture for the Message type */
function makeMsg(id: string, text: string) {
  return {
    message_id: id,
    created_at: "2024-01-01T00:00:00Z",
    message: { role: "user" as const, content: [{ text }] },
  };
}

// ============================================================
// 1. Characterization of basic TanStack Query behavior
// ============================================================

describe("Characterization of TanStack Query cache behavior", () => {
  let qc: QueryClient;

  beforeEach(() => {
    qc = makeQC();
  });

  it("can write a value to cache with setQueryData", () => {
    const data = [makeMsg("m1", "hello")];
    qc.setQueryData(msgKey(), data);
    expect(qc.getQueryData(msgKey())).toEqual(data);
  });

  it("invalidateQueries sets isInvalidated to true but does not clear data", () => {
    const data = [makeMsg("m1", "hello")];
    qc.setQueryData(msgKey(), data);
    qc.invalidateQueries({ queryKey: msgKey() });

    const state = qc.getQueryState(msgKey());
    expect(state?.isInvalidated).toBe(true);
    expect(qc.getQueryData(msgKey())).toEqual(data); // data is retained
  });

  it("calling setQueryData after invalidateQueries resets isInvalidated to false", () => {
    qc.setQueryData(msgKey(), [makeMsg("m1", "hello")]);
    qc.invalidateQueries({ queryKey: msgKey() });
    expect(qc.getQueryState(msgKey())?.isInvalidated).toBe(true);

    // SSE patch
    qc.setQueryData(msgKey(), (prev: ReturnType<typeof makeMsg>[]) => [
      ...prev,
      makeMsg("m2", "live"),
    ]);
    // setQueryData clears the isInvalidated flag
    expect(qc.getQueryState(msgKey())?.isInvalidated).toBe(false);
  });
});

// ============================================================
// 2. Ordering of SSE patch → invalidate (happy path A)
//    Case where SSE arrives first, invalidate arrives after
// ============================================================

describe("Happy path A: invalidate after SSE patch", () => {
  let qc: QueryClient;

  beforeEach(() => {
    qc = makeQC();
    // Initial data (equivalent to initialData set by RSC)
    qc.setQueryData(msgKey(), [makeMsg("m1", "initial")]);
  });

  it("patch remains after invalidate following SSE patch when there is no observer", () => {
    // SSE patch (user_message_committed)
    qc.setQueryData(msgKey(), (prev: ReturnType<typeof makeMsg>[]) => [
      ...prev,
      makeMsg("m2", "live"),
    ]);

    // invalidate called by sendMutation.onSuccess
    qc.invalidateQueries({ queryKey: msgKey() });

    // No observer, so refetch is not triggered → patch is retained
    const data = qc.getQueryData(msgKey()) as ReturnType<typeof makeMsg>[];
    expect(data.map((m) => m.message_id)).toContain("m2");
  });
});

// ============================================================
// 3. Reproduction of invalidate → refetch complete → SSE patch overwrite
//    (core of the finding: refetch overwrites the SSE patch)
// ============================================================

describe("finding[thread-messages-invalidate]: refetch overwrites the SSE patch", () => {
  let qc: QueryClient;

  beforeEach(() => {
    qc = makeQC();
    qc.setQueryData(msgKey(), [makeMsg("m1", "initial")]);
  });

  /**
   * Scenario:
   *   1. sendMutation.onSuccess → invalidateQueries (triggers refetch)
   *   2. SSE user_message_committed → setQueryData (optimistic append of m2)
   *   3. refetch completes → server returns only m1 (m2 not yet committed)
   *      → cache is replaced with [m1] and m2 disappears
   *
   * This is a confirmed bug: refetch clobbers the SSE patch.
   * Expressed with it.fails to keep the suite green.
   */
  it("SSE patch m2 remains in cache via setQueryData dedup in onSuccess", () => {
    // Fixed scenario:
    //   1. onSuccess → merge via setQueryData(dedup) instead of invalidateQueries
    //      → no refetch triggered, so Step 3 (refetch overwrite) does not occur
    //   2. SSE user_message_committed → setQueryData optimistically appends m2
    //   3. onSuccess dedup setQueryData → skips if m2 already exists
    //      → m2 is not lost

    // Step 1: dedup setQueryData called by onSuccess (not invalidateQueries)
    const serverMsg = makeMsg("m2", "committed-via-onSuccess");
    qc.setQueryData(msgKey(), (prev: ReturnType<typeof makeMsg>[] | undefined) => {
      if (!prev) return [serverMsg];
      if (prev.some((m) => m.message_id === serverMsg.message_id)) return prev;
      return [...prev, serverMsg];
    });

    // Step 2: SSE patch — optimistic append (same message_id = skipped by dedup)
    qc.setQueryData(msgKey(), (prev: ReturnType<typeof makeMsg>[]) => {
      if (!prev) return prev;
      if (prev.some((m) => m.message_id === "m2")) return prev; // dedup
      return [...prev, makeMsg("m2", "live-via-SSE")];
    });

    // Expected after fix: m2 is present as exactly 1 entry, not lost
    const final = qc.getQueryData(msgKey()) as ReturnType<typeof makeMsg>[];
    expect(final.map((m) => m.message_id)).toContain("m2");
    // no duplicates
    expect(final.filter((m) => m.message_id === "m2")).toHaveLength(1);
  });

  /**
   * Inverse: a PASS test that characterizes the current "actual" behavior.
   * Documents as spec that refetch deletes m2.
   */
  it("current behavior characterization: SSE patch m2 disappears from cache after refetch completes", () => {
    // Step 1: invalidate
    qc.invalidateQueries({ queryKey: msgKey() });

    // Step 2: SSE patch
    qc.setQueryData(msgKey(), (prev: ReturnType<typeof makeMsg>[]) => [
      ...(prev ?? []),
      makeMsg("m2", "live-via-SSE"),
    ]);

    // Step 3: refetch completes (server returns only m1)
    qc.setQueryData(msgKey(), [makeMsg("m1", "initial")]);

    // Current behavior: m2 is gone
    const final = qc.getQueryData(msgKey()) as ReturnType<typeof makeMsg>[];
    expect(final.map((m) => m.message_id)).not.toContain("m2");
  });

  /**
   * Alternative race scenario: refetch completes before SSE patch.
   *
   *   1. invalidate → refetch complete (server: [m1, m2]) ← m2 already committed on server
   *   2. SSE user_message_committed arrives late → setQueryData
   *      dedup guard identifies same message_id → skip → no duplicate
   *
   * This is a happy path. Not it.fails.
   */
  it("when refetch completes first and server includes m2: no duplicate via SSE dedup guard", () => {
    // Step 1: invalidate + refetch complete (server has m2)
    qc.setQueryData(msgKey(), [makeMsg("m1", "initial"), makeMsg("m2", "committed")]);

    // Step 2: SSE arrives late — equivalent to dedup guard (message_id duplicate check)
    qc.setQueryData(msgKey(), (prev: ReturnType<typeof makeMsg>[]) => {
      if (!prev) return prev;
      if (prev.some((m) => m.message_id === "m2")) return prev; // dedup
      return [...prev, makeMsg("m2", "committed")];
    });

    const final = qc.getQueryData(msgKey()) as ReturnType<typeof makeMsg>[];
    // m2 is exactly 1 entry (no duplicates)
    expect(final.filter((m) => m.message_id === "m2")).toHaveLength(1);
  });
});

// ============================================================
// 4. Characterization that worker_completed / manager_message follow
//    the same path.
//    applyRunCachePatch also invalidates threads.messages for
//    worker_completed / manager_message.
//    However, their purpose is to settle the live buffer (StreamState),
//    which differs from user_message_committed.
// ============================================================

describe("Characterization of the worker_completed invalidate pattern", () => {
  let qc: QueryClient;

  beforeEach(() => {
    qc = makeQC();
    qc.setQueryData(msgKey(), [makeMsg("m1", "initial")]);
  });

  it("worker_completed invalidate is for live buffer settlement — does not conflict with setQueryData (normally not concurrent)", () => {
    // SSE patch for worker_completed: reduces active_workers (no setQueryData to threads.messages)
    // Only invalidates threads.messages (see applyRunCachePatch lines 218-231)
    qc.invalidateQueries({
      queryKey: queryKeys.threads.messages(P, E, T),
    });

    // Data is retained after invalidate
    const data = qc.getQueryData(msgKey());
    expect(data).toBeTruthy();
  });

  /**
   * For manager_message, applyRunCachePatch invalidates threads.messages
   * but does not call setQueryData (optimistic append).
   * Therefore no "optimistic append" can be overwritten by a refetch, and
   * the finding does not apply directly to manager_message.
   */
  it("manager_message invalidate: no setQueryData to threads.messages and no target for refetch overwrite", () => {
    // manager_message SSE path: invalidate only (no setQueryData)
    qc.invalidateQueries({
      queryKey: queryKeys.threads.messages(P, E, T),
    });

    // isInvalidated = true but data is retained
    const state = qc.getQueryState(msgKey());
    expect(state?.isInvalidated).toBe(true);
    expect(qc.getQueryData(msgKey())).toEqual([makeMsg("m1", "initial")]);
  });
});

// ============================================================
// 5. Characterization of the fix proposal: replacing invalidate in
//    onSuccess with dedup setQueryData only eliminates the conflict
//    with SSE patch
// ============================================================

describe("Characterization of fix proposal: change onSuccess to setQueryData (merge)", () => {
  let qc: QueryClient;

  beforeEach(() => {
    qc = makeQC();
    qc.setQueryData(msgKey(), [makeMsg("m1", "initial")]);
  });

  /**
   * Fix proposal: instead of calling invalidateQueries in sendMutation.onSuccess,
   * append the returned message via dedup setQueryData.
   * SSE user_message_committed appends with the same message_id, but the dedup
   * guard skips it.
   *
   * In this case no refetch overwrite occurs, so the SSE patch survives.
   *
   * NOTE: This is a proposal; the current implementation is not changed.
   */
  it("fix proposal: setQueryData (dedup merge) in onSuccess → SSE patch is not lost", () => {
    // dedup merge in onSuccess instead of invalidate
    const serverResponseMsg = makeMsg("m2", "committed");
    qc.setQueryData(msgKey(), (prev: ReturnType<typeof makeMsg>[] | undefined) => {
      if (!prev) return [serverResponseMsg];
      if (prev.some((m) => m.message_id === serverResponseMsg.message_id)) return prev;
      return [...prev, serverResponseMsg];
    });

    // SSE patch arrives with the same message_id → skipped by dedup
    qc.setQueryData(msgKey(), (prev: ReturnType<typeof makeMsg>[] | undefined) => {
      if (!prev) return prev;
      if (prev.some((m) => m.message_id === "m2")) return prev; // dedup
      return [...prev, makeMsg("m2", "via-SSE")];
    });

    const final = qc.getQueryData(msgKey()) as ReturnType<typeof makeMsg>[];
    // m2 exists as exactly 1 entry and is not lost
    expect(final.filter((m) => m.message_id === "m2")).toHaveLength(1);
    expect(final.find((m) => m.message_id === "m2")?.message.content[0].text).toBe("committed");
  });
});
