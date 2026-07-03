/**
 * Wave 5 component tests:
 * - AgentProfilesSection: save / delete mutations
 * - ProjectReposClient: save commands mutation, onError display
 * - NewProjectModal: commands sent in createProject body
 * - TaskList: agent badge display
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProjectReposClient } from "@/components/features/project-repos/project-repos-client";
import { AgentProfilesSection } from "@/components/features/settings/agent-profiles-section";
import { TaskList } from "@/components/features/tasks/task-list";
import type {
  AgentProfile,
  IndexStatusResponse,
  McpConfig,
  Repo,
  SkillMeta,
  Task,
} from "@/lib/api/endpoints";
import {
  createProject,
  deleteAgentProfile,
  getIndexStatus,
  listAgentProfiles,
  listRepos,
  putAgentProfile,
  putRepoCommands,
} from "@/lib/api/endpoints";
import { I18nProvider } from "@/lib/i18n/provider";
import ja from "@/locales/ja";

vi.mock("@/lib/api/endpoints", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/endpoints")>();
  return {
    ...actual,
    listAgentProfiles: vi.fn(),
    getAgentProfile: vi.fn(),
    putAgentProfile: vi.fn(),
    deleteAgentProfile: vi.fn(),
    listRepos: vi.fn(),
    putRepoCommands: vi.fn(),
    getIndexStatus: vi.fn(),
    triggerIndex: vi.fn(),
    listSkills: vi.fn(),
    getMcpConfig: vi.fn(),
    createProject: vi.fn(),
  };
});

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

// ---- Fixtures ----

const emptySkills: SkillMeta[] = [];
const emptyMcp: McpConfig = { servers: [] };
const emptyProfiles: AgentProfile[] = [];

const sampleProfile: AgentProfile = {
  name: "frontend-worker",
  description: "Frontend tasks only",
  base_role: "worker",
  instructions: "use pnpm",
  skills: [],
  mcp_servers: [],
  allowed_commands: ["pnpm test"],
};

const sampleRepo: Repo = {
  name: "my-repo",
  path: "/Users/you/git/my-repo",
  default_branch: "main",
  commands: { allow: ["pnpm test"], deny: [] },
};

const emptyIndexStatus: IndexStatusResponse = { statuses: [] };

// ---- AgentProfilesSection ----

describe("AgentProfilesSection", () => {
  it("renders empty state when no profiles", () => {
    vi.mocked(listAgentProfiles).mockResolvedValue([]);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <AgentProfilesSection
        projectId="proj1"
        initialProfiles={emptyProfiles}
        initialSkills={emptySkills}
        initialMcpConfig={emptyMcp}
        initialRepos={[sampleRepo]}
      />,
      { wrapper: wrapper(qc) },
    );

    expect(screen.getByRole("button", { name: /new profile/i })).toBeInTheDocument();
    expect(screen.getByText(/no profiles yet/i)).toBeInTheDocument();
  });

  it("renders existing profile list", () => {
    vi.mocked(listAgentProfiles).mockResolvedValue([sampleProfile]);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <AgentProfilesSection
        projectId="proj1"
        initialProfiles={[sampleProfile]}
        initialSkills={emptySkills}
        initialMcpConfig={emptyMcp}
        initialRepos={[sampleRepo]}
      />,
      { wrapper: wrapper(qc) },
    );

    // The list item button has data-testid so use it
    expect(screen.getByTestId("profile-list-item-frontend-worker")).toBeInTheDocument();
    // The base_role badge "worker" should appear (multiple "worker" labels expected: sidebar badge + form toggle)
    expect(screen.getAllByText("worker").length).toBeGreaterThan(0);
  });

  it("calls putAgentProfile when saving a new profile", async () => {
    vi.mocked(listAgentProfiles).mockResolvedValue([]);
    vi.mocked(putAgentProfile).mockResolvedValue({
      ...sampleProfile,
      name: "new-profile",
    });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    render(
      <AgentProfilesSection
        projectId="proj1"
        initialProfiles={emptyProfiles}
        initialSkills={emptySkills}
        initialMcpConfig={emptyMcp}
        initialRepos={[sampleRepo]}
      />,
      { wrapper: wrapper(qc) },
    );

    await user.click(screen.getByRole("button", { name: /new profile/i }));

    const nameInput = screen.getByTestId("profile-name-input");
    await user.clear(nameInput);
    await user.type(nameInput, "new-profile");

    const descInput = screen.getByTestId("profile-description-input");
    await user.type(descInput, "My new profile");

    await user.click(screen.getByRole("button", { name: /save profile/i }));

    await waitFor(() => {
      expect(putAgentProfile).toHaveBeenCalledWith(
        "proj1",
        "new-profile",
        expect.objectContaining({ name: "new-profile", description: "My new profile" }),
      );
    });
  });

  it("calls deleteAgentProfile when Delete is clicked and confirmed in dialog", async () => {
    vi.mocked(listAgentProfiles).mockResolvedValue([sampleProfile]);
    vi.mocked(deleteAgentProfile).mockResolvedValue(undefined);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    render(
      <AgentProfilesSection
        projectId="proj1"
        initialProfiles={[sampleProfile]}
        initialSkills={emptySkills}
        initialMcpConfig={emptyMcp}
        initialRepos={[sampleRepo]}
      />,
      { wrapper: wrapper(qc) },
    );

    // Select the existing profile
    await user.click(screen.getByTestId("profile-list-item-frontend-worker"));

    // Click the Delete button to open the confirmation dialog
    const deleteBtn = await screen.findByRole("button", { name: /^delete$/i });
    await user.click(deleteBtn);

    // Confirm in the dialog
    const confirmBtn = await screen.findByTestId("confirm-delete-profile-btn");
    await user.click(confirmBtn);

    await waitFor(() => {
      expect(deleteAgentProfile).toHaveBeenCalledWith("proj1", "frontend-worker");
    });
  });

  it("shows skills multi-select chips when skills are provided", () => {
    vi.mocked(listAgentProfiles).mockResolvedValue([]);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const skills: SkillMeta[] = [
      { name: "run-tests", description: "" },
      { name: "lint", description: "" },
    ];

    render(
      <AgentProfilesSection
        projectId="proj1"
        initialProfiles={[sampleProfile]}
        initialSkills={skills}
        initialMcpConfig={emptyMcp}
        initialRepos={[sampleRepo]}
      />,
      { wrapper: wrapper(qc) },
    );

    expect(screen.getByTestId("profile-skills-multiselect")).toBeInTheDocument();
    expect(screen.getByText("run-tests")).toBeInTheDocument();
    expect(screen.getByText("lint")).toBeInTheDocument();
  });

  it("shows repo-allowed commands as selectable chips in the commands multi-select", () => {
    vi.mocked(listAgentProfiles).mockResolvedValue([sampleProfile]);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <AgentProfilesSection
        projectId="proj1"
        initialProfiles={[sampleProfile]}
        initialSkills={emptySkills}
        initialMcpConfig={emptyMcp}
        initialRepos={[sampleRepo]}
      />,
      { wrapper: wrapper(qc) },
    );

    // The commands field is now a multi-select fed by the repo allow list,
    // not free-text textareas.
    const multiselect = screen.getByTestId("profile-commands-multiselect");
    expect(multiselect).toBeInTheDocument();
    // sampleRepo.commands.allow = ["pnpm test"] → the chip is offered.
    expect(multiselect).toHaveTextContent("pnpm test");
    // The removed free-text deny textarea must be gone.
    expect(screen.queryByTestId("profile-commands-deny-textarea")).not.toBeInTheDocument();
  });

  it("surfaces a stale selected command (no longer repo-allowed) as a removable chip", () => {
    // Profile selected "old-cmd", but the repo now only allows "pnpm test".
    const staleProfile: AgentProfile = {
      ...sampleProfile,
      name: "stale-worker",
      allowed_commands: ["old-cmd"],
    };
    vi.mocked(listAgentProfiles).mockResolvedValue([staleProfile]);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <AgentProfilesSection
        projectId="proj1"
        initialProfiles={[staleProfile]}
        initialSkills={emptySkills}
        initialMcpConfig={emptyMcp}
        initialRepos={[sampleRepo]}
      />,
      { wrapper: wrapper(qc) },
    );

    const multiselect = screen.getByTestId("profile-commands-multiselect");
    // The stale entry stays visible (and removable) rather than silently persisting.
    expect(multiselect).toHaveTextContent("old-cmd");
  });
});

// ---- ProjectReposClient (repo commands mutation coverage) ----
//
// Uses the real ProjectReposClient component (table layout with inline allow/deny
// textareas). getIndexStatus and triggerIndex are mocked; initialData prevents
// the query from fetching on mount.

describe("ProjectReposClient", () => {
  it("renders repo with allow/deny textareas", () => {
    vi.mocked(listRepos).mockResolvedValue([sampleRepo]);
    vi.mocked(getIndexStatus).mockResolvedValue(emptyIndexStatus);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <ProjectReposClient
        projectId="proj1"
        initialRepos={[sampleRepo]}
        initialIndexStatus={emptyIndexStatus}
      />,
      { wrapper: wrapper(qc) },
    );

    expect(screen.getByTestId("repo-row-my-repo")).toBeInTheDocument();
    expect(screen.getByText("my-repo")).toBeInTheDocument();
    // Initial allow text seeded from sampleRepo.commands.allow
    expect(screen.getByDisplayValue("pnpm test")).toBeInTheDocument();
  });

  it("calls putRepoCommands when Save is clicked", async () => {
    vi.mocked(listRepos).mockResolvedValue([sampleRepo]);
    vi.mocked(getIndexStatus).mockResolvedValue(emptyIndexStatus);
    vi.mocked(putRepoCommands).mockResolvedValue({
      ...sampleRepo,
      commands: { allow: ["pnpm test", "pnpm lint"], deny: [] },
    });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    render(
      <ProjectReposClient
        projectId="proj1"
        initialRepos={[sampleRepo]}
        initialIndexStatus={emptyIndexStatus}
      />,
      { wrapper: wrapper(qc) },
    );

    const allowTextarea = screen.getByTestId("repo-allow-textarea-my-repo");
    await user.clear(allowTextarea);
    await user.type(allowTextarea, "pnpm test\npnpm lint");

    await user.click(screen.getByTestId("save-repo-commands-btn-my-repo"));

    await waitFor(() => {
      expect(putRepoCommands).toHaveBeenCalledWith(
        "proj1",
        "my-repo",
        expect.objectContaining({
          allow: expect.arrayContaining(["pnpm test", "pnpm lint"]),
        }),
      );
    });
  });

  it("shows save error when putRepoCommands rejects", async () => {
    vi.mocked(listRepos).mockResolvedValue([sampleRepo]);
    vi.mocked(getIndexStatus).mockResolvedValue(emptyIndexStatus);
    vi.mocked(putRepoCommands).mockRejectedValue(new Error("Network error"));

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const user = userEvent.setup();

    render(
      <ProjectReposClient
        projectId="proj1"
        initialRepos={[sampleRepo]}
        initialIndexStatus={emptyIndexStatus}
      />,
      { wrapper: wrapper(qc) },
    );

    await user.click(screen.getByTestId("save-repo-commands-btn-my-repo"));

    await waitFor(() => {
      expect(screen.getByTestId("repo-save-error-my-repo")).toBeInTheDocument();
    });
    expect(screen.getByTestId("repo-save-error-my-repo")).toHaveTextContent("Network error");
  });
});

// ---- TaskList agent badge ----

describe("TaskList agent badge", () => {
  it("shows agent badge when task.agent is set", () => {
    const tasks: Task[] = [
      {
        id: "T001",
        title: "Implement feature",
        status: "in_progress",
        repo: "my-repo",
        agent: "frontend-worker",
        contract: "",
      },
    ];

    render(<TaskList tasks={tasks} />);

    expect(screen.getByText("frontend-worker")).toBeInTheDocument();
  });

  it("does not show agent badge when task.agent is not set", () => {
    const tasks: Task[] = [
      {
        id: "T001",
        title: "Implement feature",
        status: "todo",
        contract: "",
      },
    ];

    render(<TaskList tasks={tasks} />);

    // No agent badge text visible (beyond the task title)
    expect(screen.getByText("Implement feature")).toBeInTheDocument();
    // The smart_toy icon should not appear (we can't easily test icon text, but the container should not exist)
    expect(
      screen.queryByText((text) => text.toLowerCase().includes("frontend-worker")),
    ).not.toBeInTheDocument();
  });
});

// ---- NewProjectModal commands ----

describe("NewProjectModal commands", () => {
  it("sends commands in createProject body when set", async () => {
    // Import dynamically after mocks are set up
    const { NewProjectModal } = await import("@/components/features/projects/new-project-modal");
    vi.mocked(createProject).mockResolvedValue({
      id: "proj1",
      name: "test",
      status: "active",
      epic_counter: 0,
    });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    render(<NewProjectModal />, { wrapper: wrapper(qc) });

    // Open modal (i18n: "New Project" in en, "新規プロジェクト" in ja)
    await user.click(screen.getByRole("button", { name: /新規プロジェクト|new project/i }));

    // Fill project name
    await user.type(screen.getByTestId("project-name-input"), "test");

    // Fill repo path
    await user.type(screen.getByTestId("repo-path-input-0"), "/Users/you/git/test");

    // Expand commands section
    await user.click(screen.getByTestId("repo-commands-toggle-0"));

    // Fill allow list
    const allowTextarea = screen.getByTestId("repo-modal-allow-0");
    await user.type(allowTextarea, "pnpm test");

    // Submit
    const dialog = screen.getByRole("dialog");
    const submitBtn = Array.from(dialog.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Initialize"),
    );
    if (submitBtn) await user.click(submitBtn);

    await waitFor(() => {
      expect(createProject).toHaveBeenCalledWith(
        expect.objectContaining({
          repos: expect.arrayContaining([
            expect.objectContaining({
              commands: expect.objectContaining({
                allow: expect.arrayContaining(["pnpm test"]),
              }),
            }),
          ]),
        }),
      );
    });
  });
});
