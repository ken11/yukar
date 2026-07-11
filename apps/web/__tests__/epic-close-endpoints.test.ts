/**
 * Epic complete / reopen / merge endpoint tests (1-bit epic lifecycle).
 *
 * The old POST /close and the in_review "approve" collapsed into a single
 * user-owned toggle: PATCH { status: "completed" } / PATCH { status: "open" }.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, listEpics, patchEpic, startMerge, stopMerge } from "../lib/api/endpoints";

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

const baseEpic = {
  id: "EP-1",
  title: "Test",
  status: "open" as const,
  project_id: "proj1",
};

// ---- listEpics include_completed ----

describe("listEpics", () => {
  it("defaults to no include_completed query param", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(200, []));
    await listEpics("proj1");
    const url: string = mockFetch.mock.calls[0][0];
    expect(url).not.toContain("include_completed");
  });

  it("appends include_completed=true when requested", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(200, []));
    await listEpics("proj1", true);
    const url: string = mockFetch.mock.calls[0][0];
    expect(url).toContain("include_completed=true");
  });
});

// ---- patchEpic (complete / reopen) ----

describe("patchEpic status toggle", () => {
  it("PATCHes status to completed (the single finish action)", async () => {
    const completed = { ...baseEpic, status: "completed" };
    mockFetch.mockResolvedValueOnce(mockResponse(200, completed));

    const result = await patchEpic("proj1", "EP-1", { status: "completed" });
    expect(result.status).toBe("completed");

    const call = mockFetch.mock.calls[0];
    expect(call[0]).toContain("/api/projects/proj1/epics/EP-1");
    expect(call[1].method).toBe("PATCH");
    const body = JSON.parse(call[1].body);
    expect(body.status).toBe("completed");
  });

  it("PATCHes status to open (reopen)", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(200, baseEpic));

    const result = await patchEpic("proj1", "EP-1", { status: "open" });
    expect(result.status).toBe("open");

    const body = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(Object.keys(body)).toEqual(["status"]);
    expect(body.status).toBe("open");
  });

  it("throws ApiError 409 when completing while a run is active", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(409, { detail: "run active" }));
    await expect(patchEpic("proj1", "EP-1", { status: "completed" })).rejects.toBeInstanceOf(
      ApiError,
    );
  });
});

// ---- startMerge ----

describe("startMerge", () => {
  it("POSTs epic_ids and returns StartMergeResponse", async () => {
    const response = { run_id: "arbiter-run-1", status: "started" };
    mockFetch.mockResolvedValueOnce(mockResponse(202, response));

    const result = await startMerge("proj1", ["EP-1", "EP-2"]);
    expect(result.run_id).toBe("arbiter-run-1");
    expect(result.status).toBe("started");

    const call = mockFetch.mock.calls[0];
    expect(call[0]).toContain("/api/projects/proj1/merge");
    expect(call[1].method).toBe("POST");
    const body = JSON.parse(call[1].body);
    expect(body.epic_ids).toEqual(["EP-1", "EP-2"]);
  });

  it("throws ApiError 409 when arbiter is busy", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(409, { detail: "arbiter already running" }));
    await expect(startMerge("proj1", ["EP-1"])).rejects.toBeInstanceOf(ApiError);
  });
});

// ---- stopMerge ----

describe("stopMerge", () => {
  it("POSTs to merge/stop and returns StopMergeResponse", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(200, { status: "stopped" }));

    const result = await stopMerge("proj1");
    expect(result.status).toBe("stopped");

    const call = mockFetch.mock.calls[0];
    expect(call[0]).toContain("/api/projects/proj1/merge/stop");
    expect(call[1].method).toBe("POST");
  });
});
