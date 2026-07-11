/**
 * multi-trial tests
 *
 * M3: verifying the single-request behavior of passing archive_active:true to createThread
 * M4: verifying that the request body changes depending on whether title is present
 *
 * Note: NewThreadModal depends on browser-specific APIs (router, fetch), so
 * only the type interface of endpoints.ts + fetch body verification is done here.
 * Component-level tests are delegated to Playwright E2E.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CreateThreadRequest, ThreadEntry } from "../lib/api/endpoints";
import { createThread } from "../lib/api/endpoints";
import { computeIsActiveTrial } from "../lib/thread-utils";

// ============================================================
// fetch mock
// ============================================================

function mockFetch(response: unknown, status = 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: status >= 200 && status < 300,
      status,
      json: () => Promise.resolve(response),
    }),
  );
}

function lastFetchBody(): CreateThreadRequest {
  const fetchMock = vi.mocked(globalThis.fetch);
  const calls = fetchMock.mock.calls;
  const lastCall = calls[calls.length - 1];
  const init = lastCall?.[1] as RequestInit | undefined;
  return JSON.parse(init?.body as string) as CreateThreadRequest;
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

// ============================================================
// M3: single request with archive_active:true
// ============================================================

describe("M3: createThread — single request with archive_active:true", () => {
  it("POSTs with archive_active:true in the body", async () => {
    const newThread: ThreadEntry = {
      id: "trial-2",
      title: "Trial 2",
      role: "manager",
      status: "active",
      task: null,
      repo: null,
      parent_thread_id: null,
    };
    mockFetch(newThread);

    await createThread("proj1", "epic1", {
      role: "manager",
      archive_active: true,
      same_branch: false,
      title: "",
    });

    const body = lastFetchBody();
    expect(body.archive_active).toBe(true);
    expect(body.role).toBe("manager");
  });

  it("sends false when archive_active:false (default behavior check)", async () => {
    const newThread: ThreadEntry = {
      id: "trial-1",
      title: "Trial 1",
      role: "manager",
      status: "active",
      task: null,
      repo: null,
      parent_thread_id: null,
    };
    mockFetch(newThread);

    await createThread("proj1", "epic1", {
      role: "manager",
      archive_active: false,
      same_branch: false,
      title: "Trial 1",
    });

    const body = lastFetchBody();
    expect(body.archive_active).toBe(false);
  });

  it("rejects as ApiError on 409", async () => {
    mockFetch({ detail: "Run is active" }, 409);

    const { ApiError } = await import("../lib/api/endpoints");
    await expect(
      createThread("proj1", "epic1", {
        role: "manager",
        archive_active: true,
        same_branch: false,
        title: "",
      }),
    ).rejects.toBeInstanceOf(ApiError);
  });
});

// ============================================================
// M4: request body changes depending on whether title is present
// ============================================================

describe("M4: createThread — body changes depending on title", () => {
  it("body contains title when title is non-empty", async () => {
    const newThread: ThreadEntry = {
      id: "trial-3",
      title: "Alternative approach",
      role: "manager",
      status: "active",
      task: null,
      repo: null,
      parent_thread_id: null,
    };
    mockFetch(newThread);

    await createThread("proj1", "epic1", {
      role: "manager",
      archive_active: true,
      same_branch: false,
      title: "Alternative approach",
    });

    const body = lastFetchBody();
    expect(body.title).toBe("Alternative approach");
  });

  it("body title is empty string when title is empty string (backend auto-assigns Trial N)", async () => {
    const newThread: ThreadEntry = {
      id: "trial-4",
      title: "Trial 4",
      role: "manager",
      status: "active",
      task: null,
      repo: null,
      parent_thread_id: null,
    };
    mockFetch(newThread);

    await createThread("proj1", "epic1", {
      role: "manager",
      archive_active: true,
      same_branch: false,
      title: "", // Send empty string → backend assigns Trial N
    });

    const body = lastFetchBody();
    expect(body.title).toBe("");
  });
});

// ============================================================
// M5: composer gate — derivation logic for isActiveTrial
//
// Verifies computeIsActiveTrial in lib/thread-utils.ts.
// thread-page-client.tsx imports this function, so there is no duplication with tests.
//
// The only path to display the composer is activityState.activeTrialId
//   (activeThreadId → SET_MANAGER_THREAD_ID).
// Excluding archived from applyTreeInit / INIT is a fix for tree display nodes
//   and is unrelated to the composer.
// useRunActivity's initialThreads is an RSC initial prop, not a live subscription.
//
// Cases:
//   1. Viewing thread matches activeTrialId + not archived → show composer (true)
//   2. Even a completed (resolved) thread matches activeTrialId → show composer (true)
//   3. Manager thread that does not match activeTrialId (old trial) → read-only (false)
//   4. Archived thread → read-only (false)
// ============================================================

describe("M5: isActiveTrial — composer gate derivation logic", () => {
  it("active trial: threadId matches activeTrialId + not archived → true (composer shown)", () => {
    expect(computeIsActiveTrial("th-active", "th-active", false)).toBe(true);
  });

  it("active trial is true even for completed (resolved) threads (follow-up requests can be sent)", () => {
    // status=resolved is treated as isArchived=false
    expect(computeIsActiveTrial("th-resolved", "th-resolved", false)).toBe(true);
  });

  it("old trial: manager thread where threadId does not match activeTrialId → false (read-only)", () => {
    expect(computeIsActiveTrial("th-old-trial", "th-new-trial", false)).toBe(false);
  });

  it("archived thread → false (archived banner shown)", () => {
    expect(computeIsActiveTrial("th-archived", "th-archived", true)).toBe(false);
  });

  it("→ true when matching the activeTrialId fallback 'manager'", () => {
    expect(computeIsActiveTrial("manager", "manager", false)).toBe(true);
  });
});
