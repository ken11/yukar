/**
 * Wave 5: agent-profiles / repos / commands endpoint tests
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  deleteAgentProfile,
  getAgentProfile,
  listAgentProfiles,
  listRepos,
  putAgentProfile,
  putRepoCommands,
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

// ---- Agent Profiles ----

describe("listAgentProfiles", () => {
  it("GETs /agent-profiles and returns AgentProfile[]", async () => {
    const data = [
      {
        name: "frontend-worker",
        description: "Frontend tasks",
        base_role: "worker",
        instructions: "use pnpm",
        skills: ["run-tests"],
        mcp_servers: [],
        commands: { allow: ["pnpm test"], deny: [] },
      },
    ];
    mockFetch.mockResolvedValueOnce(mockResponse(200, data));

    const result = await listAgentProfiles("proj1");
    expect(result).toHaveLength(1);
    expect(result[0].name).toBe("frontend-worker");
    expect(mockFetch.mock.calls[0][0]).toContain("/api/projects/proj1/agent-profiles");
  });

  it("returns empty array when no profiles", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(200, []));
    const result = await listAgentProfiles("proj1");
    expect(result).toHaveLength(0);
  });
});

describe("getAgentProfile", () => {
  it("GETs /agent-profiles/{name} and returns profile", async () => {
    const profile = {
      name: "backend-worker",
      description: "Backend tasks",
      base_role: "worker",
      instructions: "use pytest",
      skills: [],
      mcp_servers: [],
    };
    mockFetch.mockResolvedValueOnce(mockResponse(200, profile));

    const result = await getAgentProfile("proj1", "backend-worker");
    expect(result.name).toBe("backend-worker");
    expect(mockFetch.mock.calls[0][0]).toContain(
      "/api/projects/proj1/agent-profiles/backend-worker",
    );
  });

  it("throws ApiError on 404", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(404, { detail: "not found" }));
    await expect(getAgentProfile("proj1", "missing")).rejects.toBeInstanceOf(ApiError);
  });

  it("URL-encodes profile names with special chars", async () => {
    mockFetch.mockResolvedValueOnce(
      mockResponse(200, {
        name: "my profile",
        description: "",
        base_role: "worker",
        instructions: "",
      }),
    );
    await getAgentProfile("proj1", "my profile");
    expect(mockFetch.mock.calls[0][0]).toContain("my%20profile");
  });
});

describe("putAgentProfile", () => {
  it("PUTs profile body and returns updated profile", async () => {
    const profile = {
      name: "frontend-worker",
      description: "Frontend",
      base_role: "worker" as const,
      instructions: "use pnpm",
      skills: ["run-tests"],
      mcp_servers: [],
      commands: { allow: ["pnpm test"], deny: [] },
    };
    mockFetch.mockResolvedValueOnce(mockResponse(200, profile));

    const result = await putAgentProfile("proj1", "frontend-worker", profile);
    expect(result.name).toBe("frontend-worker");

    const call = mockFetch.mock.calls[0];
    expect(call[0]).toContain("/api/projects/proj1/agent-profiles/frontend-worker");
    expect(call[1].method).toBe("PUT");
    const body = JSON.parse(call[1].body);
    expect(body.base_role).toBe("worker");
    expect(body.commands.allow).toEqual(["pnpm test"]);
  });
});

describe("deleteAgentProfile", () => {
  it("DELETEs /agent-profiles/{name} and returns void on 204", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, status: 204, json: () => Promise.resolve(null) });

    const result = await deleteAgentProfile("proj1", "frontend-worker");
    expect(result).toBeUndefined();

    const call = mockFetch.mock.calls[0];
    expect(call[1].method).toBe("DELETE");
    expect(call[0]).toContain("/api/projects/proj1/agent-profiles/frontend-worker");
  });
});

// ---- Repos ----

describe("listRepos", () => {
  it("GETs /repos and returns Repo[]", async () => {
    const repos = [
      {
        name: "my-repo",
        path: "/Users/you/git/my-repo",
        default_branch: "main",
        commands: { allow: ["pnpm test"], deny: [] },
      },
    ];
    mockFetch.mockResolvedValueOnce(mockResponse(200, repos));

    const result = await listRepos("proj1");
    expect(result).toHaveLength(1);
    expect(result[0].name).toBe("my-repo");
    expect(mockFetch.mock.calls[0][0]).toContain("/api/projects/proj1/repos");
  });
});

describe("putRepoCommands", () => {
  it("PUTs commands body and returns updated Repo", async () => {
    const repo = {
      name: "my-repo",
      path: "/Users/you/git/my-repo",
      default_branch: "main",
      commands: { allow: ["pnpm test", "pnpm lint"], deny: ["rm -rf"] },
    };
    mockFetch.mockResolvedValueOnce(mockResponse(200, repo));

    const result = await putRepoCommands("proj1", "my-repo", {
      allow: ["pnpm test", "pnpm lint"],
      deny: ["rm -rf"],
    });
    expect(result.name).toBe("my-repo");
    expect(result.commands?.allow).toEqual(["pnpm test", "pnpm lint"]);

    const call = mockFetch.mock.calls[0];
    expect(call[0]).toContain("/api/projects/proj1/repos/my-repo/commands");
    expect(call[1].method).toBe("PUT");
    const body = JSON.parse(call[1].body);
    expect(body.allow).toEqual(["pnpm test", "pnpm lint"]);
    expect(body.deny).toEqual(["rm -rf"]);
  });

  it("URL-encodes repo names with special chars", async () => {
    const repo = {
      name: "my repo",
      path: "/p",
      default_branch: "main",
      commands: { allow: [], deny: [] },
    };
    mockFetch.mockResolvedValueOnce(mockResponse(200, repo));
    await putRepoCommands("proj1", "my repo", { allow: [], deny: [] });
    expect(mockFetch.mock.calls[0][0]).toContain("my%20repo");
  });
});
