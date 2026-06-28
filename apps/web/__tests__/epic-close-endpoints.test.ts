/**
 * Epic Close / Reopen / Merge endpoint tests (Feature 1 + Feature 2).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  closeEpic,
  listEpics,
  patchEpic,
  startMerge,
  stopMerge,
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

const baseEpic = {
  id: "EP-1",
  title: "Test",
  status: "completed" as const,
  project_id: "proj1",
};

// ---- listEpics include_closed ----

describe("listEpics", () => {
  it("defaults to no include_closed query param", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(200, []));
    await listEpics("proj1");
    const url: string = mockFetch.mock.calls[0][0];
    expect(url).not.toContain("include_closed");
  });

  it("appends include_closed=true when requested", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(200, []));
    await listEpics("proj1", true);
    const url: string = mockFetch.mock.calls[0][0];
    expect(url).toContain("include_closed=true");
  });
});

// ---- closeEpic ----

describe("closeEpic", () => {
  it("POSTs to .../close and returns Epic", async () => {
    const closed = { ...baseEpic, status: "closed" };
    mockFetch.mockResolvedValueOnce(mockResponse(200, closed));

    const result = await closeEpic("proj1", "EP-1");
    expect(result.status).toBe("closed");

    const call = mockFetch.mock.calls[0];
    expect(call[0]).toContain("/api/projects/proj1/epics/EP-1/close");
    expect(call[1].method).toBe("POST");
  });

  it("throws ApiError 409 when run is active", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(409, { detail: "run active" }));
    await expect(closeEpic("proj1", "EP-1")).rejects.toBeInstanceOf(ApiError);
  });
});

// ---- patchEpic ----

describe("patchEpic", () => {
  it("PATCHes status to planned (reopen)", async () => {
    const reopened = { ...baseEpic, status: "planned" };
    mockFetch.mockResolvedValueOnce(mockResponse(200, reopened));

    const result = await patchEpic("proj1", "EP-1", { status: "planned" });
    expect(result.status).toBe("planned");

    const call = mockFetch.mock.calls[0];
    expect(call[0]).toContain("/api/projects/proj1/epics/EP-1");
    expect(call[1].method).toBe("PATCH");
    const body = JSON.parse(call[1].body);
    expect(body.status).toBe("planned");
  });

  it("allows patching only status field", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(200, { ...baseEpic, status: "planned" }));
    await patchEpic("proj1", "EP-1", { status: "planned" });
    const body = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(Object.keys(body)).toEqual(["status"]);
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
