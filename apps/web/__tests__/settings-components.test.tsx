/**
 * Wave 4c component tests: AgentConfigsSection, SkillsSection, McpSection
 * Tests focus on mutation calls and form interactions.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentConfigsSection } from "@/components/features/settings/agent-configs-section";
import { McpSection } from "@/components/features/settings/mcp-section";
import { SkillsSection } from "@/components/features/settings/skills-section";
import type { McpConfig, Skill, SkillMeta } from "@/lib/api/endpoints";
import {
  deleteSkill,
  listSkills,
  putAgentConfig,
  putMcpConfig,
  putSkill,
} from "@/lib/api/endpoints";
import { I18nProvider } from "@/lib/i18n/provider";
import ja from "@/locales/ja";

vi.mock("@/lib/api/endpoints", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/endpoints")>();
  return {
    ...actual,
    putAgentConfig: vi.fn(),
    listSkills: vi.fn(),
    getSkill: vi.fn(),
    putSkill: vi.fn(),
    deleteSkill: vi.fn(),
    getMcpConfig: vi.fn(),
    putMcpConfig: vi.fn(),
  };
});

// CodeMirror runs in a browser env; mock it to avoid JSDOM issues.
vi.mock("@/components/features/editor/code-mirror-editor", () => ({
  CodeMirrorEditor: ({ value, onChange }: { value: string; onChange?: (v: string) => void }) => (
    <textarea data-testid="cm-editor" value={value} onChange={(e) => onChange?.(e.target.value)} />
  ),
}));

afterEach(() => {
  vi.restoreAllMocks();
});

function wrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <I18nProvider dict={ja} locale="ja">
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      </I18nProvider>
    );
  };
}

// ---- AgentConfigsSection ----

describe("AgentConfigsSection", () => {
  it("calls putAgentConfig with correct role and instructions on Save", async () => {
    vi.mocked(putAgentConfig).mockResolvedValue({
      role: "worker",
      instructions: "use pnpm",
    });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    const initialConfigs = [
      { role: "manager" as const, instructions: "" },
      { role: "worker" as const, instructions: "" },
      { role: "evaluator" as const, instructions: "" },
    ];

    render(<AgentConfigsSection projectId="proj1" initialConfigs={initialConfigs} />, {
      wrapper: wrapper(qc),
    });

    // Worker tab is selected by default
    const textarea = screen.getByRole("textbox");
    await user.clear(textarea);
    await user.type(textarea, "use pnpm");

    await user.click(screen.getByRole("button", { name: /保存する worker/i }));

    await waitFor(() => {
      expect(putAgentConfig).toHaveBeenCalledWith("proj1", "worker", "use pnpm");
    });
  });

  it("switches to Manager role tab and saves", async () => {
    vi.mocked(putAgentConfig).mockResolvedValue({
      role: "manager",
      instructions: "be concise",
    });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    render(
      <AgentConfigsSection
        projectId="proj1"
        initialConfigs={[
          { role: "manager" as const, instructions: "be concise" },
          { role: "worker" as const, instructions: "" },
          { role: "evaluator" as const, instructions: "" },
        ]}
      />,
      { wrapper: wrapper(qc) },
    );

    await user.click(screen.getByRole("tab", { name: /manager/i }));
    await user.click(screen.getByRole("button", { name: /保存する manager/i }));

    await waitFor(() => {
      expect(putAgentConfig).toHaveBeenCalledWith("proj1", "manager", "be concise");
    });
  });
});

// ---- SkillsSection ----

describe("SkillsSection", () => {
  it("renders skill list from initialSkills", () => {
    vi.mocked(listSkills).mockResolvedValue([]);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const skills: SkillMeta[] = [
      { name: "run-tests", description: "Run test suite" },
      { name: "lint", description: "Run linter" },
    ];

    render(<SkillsSection projectId="proj1" initialSkills={skills} />, {
      wrapper: wrapper(qc),
    });

    expect(screen.getByText("run-tests")).toBeInTheDocument();
    expect(screen.getByText("lint")).toBeInTheDocument();
  });

  it("calls putSkill when creating a new skill", async () => {
    const newSkill: Skill = {
      name: "my-skill",
      description: "",
      content: "---\nname: my-skill\n---\n# my-skill\n",
    };
    vi.mocked(putSkill).mockResolvedValue(newSkill);
    vi.mocked(listSkills).mockResolvedValue([]);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    render(<SkillsSection projectId="proj1" initialSkills={[]} />, {
      wrapper: wrapper(qc),
    });

    await user.click(screen.getByRole("button", { name: /new skill/i }));

    const nameInput = screen.getByPlaceholderText(/e\.g\. run-tests/i);
    await user.clear(nameInput);
    await user.type(nameInput, "my-skill");

    await user.click(screen.getByRole("button", { name: /save skill/i }));

    await waitFor(() => {
      expect(putSkill).toHaveBeenCalledWith(
        "proj1",
        "my-skill",
        expect.stringContaining("my-skill"),
      );
    });
  });

  it("calls deleteSkill when Delete is clicked and confirmed in dialog", async () => {
    vi.mocked(deleteSkill).mockResolvedValue(undefined);
    vi.mocked(listSkills).mockResolvedValue([{ name: "run-tests", description: "" }]);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    const skills: SkillMeta[] = [{ name: "run-tests", description: "" }];

    // Pre-populate cache so the delete button is shown without needing fetch
    qc.setQueryData(["skills", "proj1", "run-tests"], {
      name: "run-tests",
      description: "",
      content: "# run-tests",
    } satisfies Skill);

    render(<SkillsSection projectId="proj1" initialSkills={skills} />, {
      wrapper: wrapper(qc),
    });

    // Simulate clicking the existing skill so it's "selected"
    await user.click(screen.getByText("run-tests"));

    // Click the Delete button to open the confirmation dialog
    const deleteButton = await screen.findByRole("button", { name: /^delete$/i });
    await user.click(deleteButton);

    // Confirm in the dialog
    const confirmButton = await screen.findByTestId("confirm-delete-skill-btn");
    await user.click(confirmButton);

    await waitFor(() => {
      expect(deleteSkill).toHaveBeenCalledWith("proj1", "run-tests");
    });
  });
});

// ---- McpSection ----

describe("McpSection", () => {
  const emptyConfig: McpConfig = { servers: [] };

  it("renders empty state with Add Server button", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<McpSection projectId="proj1" initialConfig={emptyConfig} />, {
      wrapper: wrapper(qc),
    });
    expect(screen.getByRole("button", { name: /add server/i })).toBeInTheDocument();
  });

  it("adds a server and calls putMcpConfig on Save", async () => {
    vi.mocked(putMcpConfig).mockResolvedValue({
      servers: [
        {
          name: "github",
          type: "stdio",
          url: null,
          command: "npx",
          args: [],
          env: {},
          allowed_tools: null,
          rejected_tools: null,
        },
      ],
    });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    render(<McpSection projectId="proj1" initialConfig={emptyConfig} />, {
      wrapper: wrapper(qc),
    });

    await user.click(screen.getByRole("button", { name: /add server/i }));

    // Fill in server name
    const nameInput = screen.getByPlaceholderText("e.g. github");
    await user.clear(nameInput);
    await user.type(nameInput, "github");

    // Fill in command (stdio type by default)
    const commandInput = screen.getByPlaceholderText("e.g. npx");
    await user.clear(commandInput);
    await user.type(commandInput, "npx");

    await user.click(screen.getByRole("button", { name: /保存する/i }));

    await waitFor(() => {
      expect(putMcpConfig).toHaveBeenCalledWith(
        "proj1",
        expect.objectContaining({
          servers: expect.arrayContaining([
            expect.objectContaining({ name: "github", type: "stdio" }),
          ]),
        }),
      );
    });
  });

  it("renders existing servers from initialConfig", () => {
    const config: McpConfig = {
      servers: [
        {
          name: "my-mcp",
          type: "sse",
          url: "https://mcp.example.com/sse",
          command: null,
          args: [],
          env: {},
          allowed_tools: null,
          rejected_tools: null,
        },
      ],
    };

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<McpSection projectId="proj1" initialConfig={config} />, {
      wrapper: wrapper(qc),
    });

    expect(screen.getByText("my-mcp")).toBeInTheDocument();
    // The URL field is rendered because type=sse
    expect(screen.getByDisplayValue("https://mcp.example.com/sse")).toBeInTheDocument();
  });
});
