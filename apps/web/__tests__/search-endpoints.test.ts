/**
 * Tests for search/index API endpoints added in M3.
 * Validates type shapes and correct HTTP method / URL construction.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  getIndexStatus,
  type IndexStatusResponse,
  type IndexTriggerResponse,
  type SearchResponse,
  searchCodebase,
  triggerIndex,
} from "../lib/api/endpoints";

const mockFetch = vi.fn();
beforeEach(() => {
  vi.stubGlobal("fetch", mockFetch);
});
afterEach(() => {
  vi.unstubAllGlobals();
  mockFetch.mockReset();
});

function mockResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: String(status),
    json: () => Promise.resolve(body),
  };
}

// ---- searchCodebase ----

describe("searchCodebase", () => {
  it("POST /api/projects/{p}/search and returns SearchResponse", async () => {
    const payload: SearchResponse = {
      results: [
        {
          repo: "myrepo",
          path: "src/foo.ts",
          snippet: "const x = 1;",
          score: 0.92,
          start_line: 10,
          end_line: 15,
          language: "typescript",
        },
      ],
      unindexed_repos: [],
    };
    mockFetch.mockResolvedValueOnce(mockResponse(200, payload));

    const result = await searchCodebase("proj1", { query: "constant x", top_k: 8 });

    expect(result.results).toHaveLength(1);
    expect(result.results[0].repo).toBe("myrepo");
    expect(result.results[0].score).toBe(0.92);
    expect(result.unindexed_repos).toEqual([]);

    const call = mockFetch.mock.calls[0];
    expect(call[0]).toContain("/api/projects/proj1/search");
    expect(call[1].method).toBe("POST");
    expect(JSON.parse(call[1].body)).toMatchObject({ query: "constant x", top_k: 8 });
  });

  it("passes repo filter when provided", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(200, { results: [], unindexed_repos: [] }));

    await searchCodebase("proj1", { query: "foo", repo: "myrepo", top_k: 8 });

    const body = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(body.repo).toBe("myrepo");
  });

  it("surfaces unindexed_repos in the response", async () => {
    const payload: SearchResponse = {
      results: [],
      unindexed_repos: ["repo-a", "repo-b"],
    };
    mockFetch.mockResolvedValueOnce(mockResponse(200, payload));

    const result = await searchCodebase("proj1", { query: "anything", top_k: 8 });
    expect(result.unindexed_repos).toEqual(["repo-a", "repo-b"]);
  });

  it("throws ApiError on 422", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(422, { detail: "bad query" }));

    await expect(searchCodebase("proj1", { query: "", top_k: 8 })).rejects.toBeInstanceOf(ApiError);
  });
});

// ---- triggerIndex ----

describe("triggerIndex", () => {
  it("POST /api/projects/{p}/index (no repo) returns IndexTriggerResponse", async () => {
    const payload: IndexTriggerResponse = { accepted: true, repos: ["repo-a", "repo-b"] };
    mockFetch.mockResolvedValueOnce(mockResponse(202, payload));

    const result = await triggerIndex("proj1");

    expect(result.accepted).toBe(true);
    expect(result.repos).toEqual(["repo-a", "repo-b"]);

    const call = mockFetch.mock.calls[0];
    expect(call[0]).toMatch(/\/api\/projects\/proj1\/index$/);
    expect(call[1].method).toBe("POST");
  });

  it("POST /api/projects/{p}/index?repo=xxx when repo provided", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(202, { accepted: true, repos: ["xxx"] }));

    await triggerIndex("proj1", "xxx");

    const url: string = mockFetch.mock.calls[0][0];
    expect(url).toContain("repo=xxx");
  });
});

// ---- getIndexStatus ----

describe("getIndexStatus", () => {
  it("GET /api/projects/{p}/index/status returns IndexStatusResponse", async () => {
    const payload: IndexStatusResponse = {
      statuses: [
        {
          repo_name: "my-repo",
          state: "indexed",
          files: 120,
          chunks: 450,
          last_indexed_at: "2026-06-12T10:00:00Z",
          ts_files: 110,
          fallback_files: 10,
        },
      ],
    };
    mockFetch.mockResolvedValueOnce(mockResponse(200, payload));

    const result = await getIndexStatus("proj1");

    expect(result.statuses).toHaveLength(1);
    expect(result.statuses[0].repo_name).toBe("my-repo");
    expect(result.statuses[0].files).toBe(120);
    expect(result.statuses[0].state).toBe("indexed");

    const call = mockFetch.mock.calls[0];
    expect(call[0]).toContain("/api/projects/proj1/index/status");
    expect(call[1].method).toBeUndefined(); // GET
  });

  it("returns empty statuses array when no repos indexed", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(200, { statuses: [] }));

    const result = await getIndexStatus("proj1");
    expect(result.statuses).toEqual([]);
  });
});
