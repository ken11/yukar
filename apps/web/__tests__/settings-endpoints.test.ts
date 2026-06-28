/**
 * Wave 4c: agent-configs / skills / mcp endpoint tests
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  deleteSkill,
  getAgentConfig,
  getMcpConfig,
  getSkill,
  listAgentConfigs,
  listSkills,
  putAgentConfig,
  putMcpConfig,
  putSkill,
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

// ---- Agent Configs ----

describe("listAgentConfigs", () => {
  it("GETs /agent-configs and returns the record", async () => {
    const data = { manager: "be concise", worker: "", evaluator: "" };
    mockFetch.mockResolvedValueOnce(mockResponse(200, data));

    const result = await listAgentConfigs("proj1");
    expect(result).toEqual(data);
    expect(mockFetch.mock.calls[0][0]).toContain("/api/projects/proj1/agent-configs");
  });
});

describe("getAgentConfig", () => {
  it("GETs /agent-configs/worker and returns AgentConfig", async () => {
    const cfg = { role: "worker", instructions: "use pnpm" };
    mockFetch.mockResolvedValueOnce(mockResponse(200, cfg));

    const result = await getAgentConfig("proj1", "worker");
    expect(result.role).toBe("worker");
    expect(result.instructions).toBe("use pnpm");
    expect(mockFetch.mock.calls[0][0]).toContain("/api/projects/proj1/agent-configs/worker");
  });

  it("throws ApiError on 404", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(404, { detail: "not found" }));

    await expect(getAgentConfig("proj1", "manager")).rejects.toBeInstanceOf(ApiError);
  });
});

describe("putAgentConfig", () => {
  it("PUTs instructions and returns updated AgentConfig", async () => {
    const cfg = { role: "evaluator", instructions: "check tests" };
    mockFetch.mockResolvedValueOnce(mockResponse(200, cfg));

    const result = await putAgentConfig("proj1", "evaluator", "check tests");
    expect(result.role).toBe("evaluator");
    expect(result.instructions).toBe("check tests");

    const call = mockFetch.mock.calls[0];
    expect(call[0]).toContain("/api/projects/proj1/agent-configs/evaluator");
    expect(call[1].method).toBe("PUT");
    expect(JSON.parse(call[1].body)).toEqual({ instructions: "check tests" });
  });
});

// ---- Skills ----

describe("listSkills", () => {
  it("GETs /skills and returns SkillMeta[]", async () => {
    const skills = [
      { name: "run-tests", description: "Run the test suite" },
      { name: "lint", description: "Run linter" },
    ];
    mockFetch.mockResolvedValueOnce(mockResponse(200, skills));

    const result = await listSkills("proj1");
    expect(result).toHaveLength(2);
    expect(result[0].name).toBe("run-tests");
    expect(mockFetch.mock.calls[0][0]).toContain("/api/projects/proj1/skills");
  });
});

describe("getSkill", () => {
  it("GETs /skills/{name} and returns full Skill", async () => {
    const skill = {
      name: "run-tests",
      description: "Run the test suite",
      content: "---\nname: run-tests\n---\n# Run Tests\n",
    };
    mockFetch.mockResolvedValueOnce(mockResponse(200, skill));

    const result = await getSkill("proj1", "run-tests");
    expect(result.name).toBe("run-tests");
    expect(result.content).toContain("# Run Tests");
    expect(mockFetch.mock.calls[0][0]).toContain("/api/projects/proj1/skills/run-tests");
  });

  it("URL-encodes skill names with special characters", async () => {
    mockFetch.mockResolvedValueOnce(
      mockResponse(200, { name: "my skill", description: "", content: "" }),
    );
    await getSkill("proj1", "my skill");
    expect(mockFetch.mock.calls[0][0]).toContain("my%20skill");
  });
});

describe("putSkill", () => {
  it("PUTs content and returns Skill", async () => {
    const skill = { name: "run-tests", description: "Run tests", content: "# content" };
    mockFetch.mockResolvedValueOnce(mockResponse(200, skill));

    const result = await putSkill("proj1", "run-tests", "# content");
    expect(result.name).toBe("run-tests");

    const call = mockFetch.mock.calls[0];
    expect(call[1].method).toBe("PUT");
    expect(JSON.parse(call[1].body)).toEqual({ content: "# content" });
  });
});

describe("deleteSkill", () => {
  it("DELETEs /skills/{name} and returns void on 204", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, status: 204, json: () => Promise.resolve(null) });

    const result = await deleteSkill("proj1", "run-tests");
    expect(result).toBeUndefined();

    const call = mockFetch.mock.calls[0];
    expect(call[1].method).toBe("DELETE");
    expect(call[0]).toContain("/api/projects/proj1/skills/run-tests");
  });
});

// ---- MCP ----

describe("getMcpConfig", () => {
  it("GETs /mcp and returns McpConfig", async () => {
    const config = {
      servers: [
        {
          name: "github",
          type: "stdio",
          url: null,
          command: "npx",
          args: ["-y", "@modelcontextprotocol/server-github"],
          // biome-ignore lint/suspicious/noTemplateCurlyInString: intentional test fixture matching BE behavior
          env: { GITHUB_TOKEN: "${GITHUB_TOKEN}" },
          allowed_tools: null,
          rejected_tools: null,
        },
      ],
    };
    mockFetch.mockResolvedValueOnce(mockResponse(200, config));

    const result = await getMcpConfig("proj1");
    expect(result.servers).toHaveLength(1);
    expect(result.servers?.[0].name).toBe("github");
    expect(result.servers?.[0].type).toBe("stdio");
    expect(mockFetch.mock.calls[0][0]).toContain("/api/projects/proj1/mcp");
  });

  it("returns empty servers on empty config", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(200, { servers: [] }));

    const result = await getMcpConfig("proj1");
    expect(result.servers).toHaveLength(0);
  });
});

describe("putMcpConfig", () => {
  it("PUTs McpConfig and returns updated config", async () => {
    const config = {
      servers: [
        {
          name: "my-server",
          type: "sse" as const,
          url: "https://mcp.example.com/sse",
          command: null,
          args: [],
          env: {},
          allowed_tools: ["read_file"],
          rejected_tools: null,
        },
      ],
    };
    mockFetch.mockResolvedValueOnce(mockResponse(200, config));

    const result = await putMcpConfig("proj1", config);
    expect(result.servers?.[0].name).toBe("my-server");

    const call = mockFetch.mock.calls[0];
    expect(call[1].method).toBe("PUT");
    const body = JSON.parse(call[1].body);
    expect(body.servers[0].type).toBe("sse");
    expect(body.servers[0].url).toBe("https://mcp.example.com/sse");
  });
});
