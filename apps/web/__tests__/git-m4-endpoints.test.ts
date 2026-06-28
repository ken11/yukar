/**
 * M4 Git endpoints + extractConflicts unit tests
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  extractConflicts,
  getGitDiffSummary,
  gitPrune,
  gitResolve,
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

// ---- extractConflicts ----

describe("extractConflicts", () => {
  it("returns conflicts array from 409 body.detail.conflicts", () => {
    const err = new ApiError(
      409,
      { detail: { message: "conflict", conflicts: ["src/a.ts", "src/b.ts"] } },
      "Conflict",
    );
    expect(extractConflicts(err)).toEqual(["src/a.ts", "src/b.ts"]);
  });

  it("returns empty array when status is not 409", () => {
    const err = new ApiError(500, { detail: "server error" }, "Server error");
    expect(extractConflicts(err)).toEqual([]);
  });

  it("returns empty array when body has no conflicts", () => {
    const err = new ApiError(409, { detail: "some conflict" }, "Conflict");
    expect(extractConflicts(err)).toEqual([]);
  });

  it("returns empty array when body is null", () => {
    const err = new ApiError(409, null, "Conflict");
    expect(extractConflicts(err)).toEqual([]);
  });

  it("returns empty array when detail.conflicts is not an array", () => {
    const err = new ApiError(409, { detail: { conflicts: "not-an-array" } }, "Conflict");
    expect(extractConflicts(err)).toEqual([]);
  });
});

// ---- gitResolve ----

describe("gitResolve", () => {
  it("POSTs to /git/resolve and returns ResolveStarted", async () => {
    const response = { run_id: "run-abc", status: "started" };
    mockFetch.mockResolvedValueOnce(mockResponse(202, response));

    const result = await gitResolve("proj1", "epic1", { repo: "my-repo" });

    expect(result.run_id).toBe("run-abc");
    expect(result.status).toBe("started");

    const call = mockFetch.mock.calls[0];
    expect(call[0]).toContain("/api/projects/proj1/epics/epic1/git/resolve");
    expect(call[1].method).toBe("POST");
    expect(JSON.parse(call[1].body)).toMatchObject({ repo: "my-repo" });
  });

  it("throws ApiError on 409 (run already active)", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(409, { detail: "run already active" }));

    let caught: unknown;
    try {
      await gitResolve("proj1", "epic1", { repo: "my-repo" });
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).status).toBe(409);
  });
});

// ---- gitPrune ----

describe("gitPrune", () => {
  it("POSTs to /git/prune and returns RepoPruneResult[]", async () => {
    const results = [
      { repo: "repo-a", worktree_removed: true, branch_deleted: true, error: null },
      { repo: "repo-b", worktree_removed: false, branch_deleted: false, error: "unmerged" },
    ];
    mockFetch.mockResolvedValueOnce(mockResponse(200, results));

    const data = await gitPrune("proj1", "epic1", { force: false, repos: null });

    expect(data).toHaveLength(2);
    expect(data[0].repo).toBe("repo-a");
    expect(data[0].worktree_removed).toBe(true);
    expect(data[1].error).toBe("unmerged");

    const call = mockFetch.mock.calls[0];
    expect(call[0]).toContain("/api/projects/proj1/epics/epic1/git/prune");
    expect(call[1].method).toBe("POST");
    expect(JSON.parse(call[1].body)).toMatchObject({ force: false });
  });

  it("POSTs with force=true for force prune", async () => {
    mockFetch.mockResolvedValueOnce(
      mockResponse(200, [
        { repo: "repo-a", worktree_removed: true, branch_deleted: true, error: null },
      ]),
    );

    await gitPrune("proj1", "epic1", { force: true, repos: null });

    const call = mockFetch.mock.calls[0];
    expect(JSON.parse(call[1].body)).toMatchObject({ force: true });
  });

  it("throws ApiError 409 when run is active", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(409, { detail: "run active" }));

    let caught: unknown;
    try {
      await gitPrune("proj1", "epic1", { force: false, repos: null });
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).status).toBe(409);
  });
});

// ---- getGitDiffSummary ----

describe("getGitDiffSummary", () => {
  it("GETs /git/diff/summary with mode query param and returns DiffSummary", async () => {
    const summary = {
      repos: [
        { repo: "repo-a", files: 3, added: 10, deleted: 2 },
        { repo: "repo-b", files: 1, added: 5, deleted: 0 },
      ],
      total_files: 4,
      total_added: 15,
      total_deleted: 2,
    };
    mockFetch.mockResolvedValueOnce(mockResponse(200, summary));

    const result = await getGitDiffSummary("proj1", "epic1", "epic");

    expect(result.total_added).toBe(15);
    expect(result.total_deleted).toBe(2);
    expect(result.repos).toHaveLength(2);
    expect(result.repos[0].repo).toBe("repo-a");

    const call = mockFetch.mock.calls[0];
    expect(call[0]).toContain("/api/projects/proj1/epics/epic1/git/diff/summary");
    expect(call[0]).toContain("mode=epic");
  });

  it("defaults to working mode", async () => {
    mockFetch.mockResolvedValueOnce(
      mockResponse(200, { repos: [], total_files: 0, total_added: 0, total_deleted: 0 }),
    );

    await getGitDiffSummary("proj1", "epic1");

    const call = mockFetch.mock.calls[0];
    expect(call[0]).toContain("mode=working");
  });
});
