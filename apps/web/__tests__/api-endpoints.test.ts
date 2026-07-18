/**
 * Type-level behavior tests for endpoints.ts (fetch mock)
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  createProject,
  deleteEpicScreenshot,
  epicScreenshotUrl,
  getSettings,
  listEpicScreenshots,
  listProjects,
} from "../lib/api/endpoints";

// Mock global.fetch
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

describe("listProjects", () => {
  it("returns array of projects on 200", async () => {
    const projects = [
      {
        id: "p1",
        name: "test",
        status: "active",
        epic_counter: 0,
        repos: [],
      },
    ];
    mockFetch.mockResolvedValueOnce(mockResponse(200, projects));

    const result = await listProjects();
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe("p1");
    expect(result[0].name).toBe("test");

    // Should call the right path with no-store cache
    const call = mockFetch.mock.calls[0];
    expect(call[0]).toContain("/api/projects");
    expect(call[1]).toMatchObject({ cache: "no-store" });
    // GET requests don't have method set explicitly (defaults to GET)
    expect(call[1].method).toBeUndefined();
  });

  it("throws ApiError on non-2xx", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(500, { detail: "internal error" }));

    let caught: unknown;
    try {
      await listProjects();
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).status).toBe(500);
  });
});

describe("createProject", () => {
  it("posts with correct body and returns project", async () => {
    const project = {
      id: "my-proj",
      name: "My Project",
      status: "active",
      epic_counter: 0,
      repos: [],
    };
    mockFetch.mockResolvedValueOnce(mockResponse(201, project));

    const result = await createProject({
      id: "my-proj",
      name: "My Project",
      repos: [],
    });

    expect(result.id).toBe("my-proj");

    const call = mockFetch.mock.calls[0];
    expect(call[1].method).toBe("POST");
    expect(JSON.parse(call[1].body)).toMatchObject({ id: "my-proj", name: "My Project" });
  });
});

describe("getSettings", () => {
  it("returns settings object", async () => {
    const settings = {
      workspace_root: "~/yukar-projects",
      llm: { provider: "bedrock", model_id: "test-model" },
    };
    mockFetch.mockResolvedValueOnce(mockResponse(200, settings));

    const result = await getSettings();
    expect(result.workspace_root).toBe("~/yukar-projects");
    expect(result.llm?.provider).toBe("bedrock");
  });
});

describe("epic screenshots", () => {
  it("lists screenshots for an epic", async () => {
    const shots = [{ filename: "20260718-143022-web.jpg", size_bytes: 4096, captured_at: "x" }];
    mockFetch.mockResolvedValueOnce(mockResponse(200, shots));

    const result = await listEpicScreenshots("p1", "EP-1");
    expect(result).toHaveLength(1);
    expect(result[0].filename).toBe("20260718-143022-web.jpg");

    const call = mockFetch.mock.calls[0];
    expect(call[0]).toContain("/api/projects/p1/epics/EP-1/screenshots");
    expect(call[1].method).toBeUndefined();
  });

  it("builds a same-origin img URL with an encoded filename", () => {
    expect(epicScreenshotUrl("p1", "EP-1", "a b.jpg")).toBe(
      "/api/projects/p1/epics/EP-1/screenshots/a%20b.jpg",
    );
  });

  it("deletes a screenshot via DELETE", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(204, null));

    await deleteEpicScreenshot("p1", "EP-1", "shot.jpg");

    const call = mockFetch.mock.calls[0];
    expect(call[0]).toContain("/api/projects/p1/epics/EP-1/screenshots/shot.jpg");
    expect(call[1].method).toBe("DELETE");
  });
});

describe("ApiError", () => {
  it("captures status and body", () => {
    const err = new ApiError(422, { detail: "bad" }, "Validation Error");
    expect(err.status).toBe(422);
    expect(err.body).toEqual({ detail: "bad" });
    expect(err.name).toBe("ApiError");
  });

  it("is instanceof Error", () => {
    const err = new ApiError(404, null, "Not found");
    expect(err).toBeInstanceOf(Error);
    expect(err.message).toBe("Not found");
  });
});
