import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProjectsClient } from "@/components/features/projects/projects-client";
import { listProjects } from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/api/query-keys";
import { I18nProvider } from "@/lib/i18n/provider";
import ja from "@/locales/ja";

vi.mock("@/lib/api/endpoints", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/endpoints")>();
  return {
    ...actual,
    listProjects: vi.fn(),
  };
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ProjectsClient", () => {
  it("refetches projects after the list query is invalidated", async () => {
    vi.mocked(listProjects).mockResolvedValue([]);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <I18nProvider dict={ja} locale="ja">
        <QueryClientProvider client={queryClient}>
          <ProjectsClient initialProjects={[]} />
        </QueryClientProvider>
      </I18nProvider>,
    );

    // Empty state is rendered when there are no projects
    expect(screen.getByText("プロジェクトがありません")).toBeInTheDocument();

    await queryClient.invalidateQueries({ queryKey: queryKeys.projects.list() });

    await waitFor(() => expect(listProjects).toHaveBeenCalledOnce());
  });

  // Regression: the empty-state CTA button is the trigger for the
  // New Project dialog. It must forward Radix DialogTrigger's injected onClick
  // onto its <button>, otherwise clicking it does nothing.
  it("opens the New Project dialog when the empty-state register button is clicked", async () => {
    vi.mocked(listProjects).mockResolvedValue([]);
    const user = userEvent.setup();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <I18nProvider dict={ja} locale="ja">
        <QueryClientProvider client={queryClient}>
          <ProjectsClient initialProjects={[]} />
        </QueryClientProvider>
      </I18nProvider>,
    );

    // Dialog is closed initially.
    expect(screen.queryByPlaceholderText("my-project")).not.toBeInTheDocument();

    // Click the "register existing local repo" button in the empty state
    await user.click(screen.getByRole("button", { name: /既存ローカル repo を登録/i }));

    // The form fields inside the dialog are now visible.
    await waitFor(() => expect(screen.getByPlaceholderText("my-project")).toBeInTheDocument());
  });
});
