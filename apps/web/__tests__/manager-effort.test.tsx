/**
 * Manager effort UI tests:
 * - NewEpicModal: displaying the effort selector and how it is passed to createEpic
 * - ManagerEffortControl: displaying the current value and calling patchEpic
 *   - Immediate display via RSC initialData (no flash)
 *   - Toast notification on mutation failure
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EpicRunProvider } from "@/components/chrome/epic-run-context";
import { ManagerEffortControl } from "@/components/features/conversation/manager-effort-control";
import type { Epic } from "@/lib/api/endpoints";
import { createEpic, getEpic, patchEpic } from "@/lib/api/endpoints";
import { I18nProvider } from "@/lib/i18n/provider";
import ja from "@/locales/ja";

vi.mock("@/lib/api/endpoints", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/endpoints")>();
  return {
    ...actual,
    createEpic: vi.fn(),
    getEpic: vi.fn(),
    patchEpic: vi.fn(),
    listEpics: vi.fn(),
  };
});

// Mock next/navigation (NewEpicModal uses useRouter)
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

// Mock sonner toast
vi.mock("sonner", () => ({
  toast: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

afterEach(() => {
  vi.restoreAllMocks();
});

const baseEpic = {
  id: "EP-1",
  slug: "ep-1-test-epic",
  title: "Test epic",
  description: "",
  acceptance_criteria: "",
  status: "open" as const,
  branch: "",
  project_id: "proj1",
  manager_effort: "high" as const,
};

// Wrapper factory for ManagerEffortControl that includes EpicRunProvider
function effortWrapper(queryClient: QueryClient, contextEpic: Epic | null = baseEpic) {
  const contextValue = {
    projectId: "proj1",
    epicId: "EP-1",
    project: null,
    epic: contextEpic,
    activityState: {
      runStatus: "idle" as const,
      pausePending: false,
      runError: null,
      awaitingInput: null,
      managerThreadId: null,
      treeState: { manager: null, workers: {}, evaluators: {}, taskToWorker: {} },
      liveBuffers: {},
    },
    setPausePending: vi.fn(),
    clearLiveBuffer: vi.fn(),
    setMobileChromeHidden: vi.fn(),
  };
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <I18nProvider dict={ja} locale="ja">
        <QueryClientProvider client={queryClient}>
          <EpicRunProvider value={contextValue}>{children}</EpicRunProvider>
        </QueryClientProvider>
      </I18nProvider>
    );
  };
}

// Wrapper for NewEpicModal (EpicRunProvider not needed)
function modalWrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <I18nProvider dict={ja} locale="ja">
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      </I18nProvider>
    );
  };
}

// ---- NewEpicModal effort ----

describe("NewEpicModal effort selector", () => {
  it("renders three effort buttons with High selected by default", async () => {
    const { NewEpicModal } = await import("@/components/features/epics/new-epic-modal");

    vi.mocked(createEpic).mockResolvedValue(baseEpic);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    render(<NewEpicModal projectId="proj1" />, { wrapper: modalWrapper(qc) });

    // Open modal
    await user.click(screen.getByTestId("new-epic-btn"));

    // All three effort buttons should be visible
    expect(screen.getByTestId("new-epic-effort-high")).toBeInTheDocument();
    expect(screen.getByTestId("new-epic-effort-xhigh")).toBeInTheDocument();
    expect(screen.getByTestId("new-epic-effort-max")).toBeInTheDocument();

    // High is pressed by default
    expect(screen.getByTestId("new-epic-effort-high")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("new-epic-effort-xhigh")).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByTestId("new-epic-effort-max")).toHaveAttribute("aria-pressed", "false");
  });

  it("passes selected manager_effort to createEpic", async () => {
    const { NewEpicModal } = await import("@/components/features/epics/new-epic-modal");

    vi.mocked(createEpic).mockResolvedValue({ ...baseEpic, manager_effort: "max" });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    render(<NewEpicModal projectId="proj1" />, { wrapper: modalWrapper(qc) });

    await user.click(screen.getByTestId("new-epic-btn"));

    // Fill title (required)
    await user.type(screen.getByTestId("epic-title-input"), "My epic");

    // Switch to Max
    await user.click(screen.getByTestId("new-epic-effort-max"));
    expect(screen.getByTestId("new-epic-effort-max")).toHaveAttribute("aria-pressed", "true");

    // Submit via dialog submit button
    const dialog = screen.getByRole("dialog");
    const submitBtn = Array.from(dialog.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Create Epic"),
    );
    if (submitBtn) await user.click(submitBtn);

    await waitFor(() => {
      expect(createEpic).toHaveBeenCalledWith(
        "proj1",
        expect.objectContaining({ manager_effort: "max" }),
      );
    });
  });
});

// ---- ManagerEffortControl ----

describe("ManagerEffortControl", () => {
  it("shows current effort from getEpic", async () => {
    vi.mocked(getEpic).mockResolvedValue({ ...baseEpic, manager_effort: "xhigh" });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<ManagerEffortControl projectId="proj1" epicId="EP-1" />, {
      wrapper: effortWrapper(qc, baseEpic),
    });

    await waitFor(() => {
      expect(screen.getByTestId("effort-btn-xhigh")).toHaveAttribute("aria-pressed", "true");
    });
    expect(screen.getByTestId("effort-btn-high")).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByTestId("effort-btn-max")).toHaveAttribute("aria-pressed", "false");
  });

  it("calls patchEpic with new effort on button click", async () => {
    vi.mocked(getEpic).mockResolvedValue(baseEpic);
    vi.mocked(patchEpic).mockResolvedValue({ ...baseEpic, manager_effort: "max" });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    render(<ManagerEffortControl projectId="proj1" epicId="EP-1" />, {
      wrapper: effortWrapper(qc),
    });

    // Wait for data to load
    await waitFor(() => {
      expect(screen.getByTestId("effort-btn-high")).toHaveAttribute("aria-pressed", "true");
    });

    await user.click(screen.getByTestId("effort-btn-max"));

    await waitFor(() => {
      expect(patchEpic).toHaveBeenCalledWith("proj1", "EP-1", { manager_effort: "max" });
    });
  });

  it("does not call patchEpic when clicking the already-active effort", async () => {
    vi.mocked(getEpic).mockResolvedValue(baseEpic); // manager_effort: "high"
    vi.mocked(patchEpic).mockResolvedValue(baseEpic);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();

    render(<ManagerEffortControl projectId="proj1" epicId="EP-1" />, {
      wrapper: effortWrapper(qc),
    });

    await waitFor(() => {
      expect(screen.getByTestId("effort-btn-high")).toHaveAttribute("aria-pressed", "true");
    });

    // Click the already-active button
    await user.click(screen.getByTestId("effort-btn-high"));

    // patchEpic should NOT be called
    expect(patchEpic).not.toHaveBeenCalled();
  });

  // Fix 2: RSC-provided epic is used as initialData, showing the real value before any fetch
  it("shows RSC-provided effort immediately without flash (initialData from context)", () => {
    // The component shows the context value immediately, even if getEpic is not called / is delayed
    vi.mocked(getEpic).mockImplementation(() => new Promise(() => {})); // never resolves

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<ManagerEffortControl projectId="proj1" epicId="EP-1" />, {
      wrapper: effortWrapper(qc, { ...baseEpic, manager_effort: "xhigh" }),
    });

    // Thanks to initialData, xhigh is shown immediately without waiting for the fetch
    expect(screen.getByTestId("effort-btn-xhigh")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("effort-btn-high")).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByTestId("effort-btn-max")).toHaveAttribute("aria-pressed", "false");
  });

  // Fix 3: toast.error is called when a mutation fails (does not silently revert)
  it("shows error toast when patchEpic fails", async () => {
    const { toast } = await import("sonner");

    vi.mocked(getEpic).mockResolvedValue(baseEpic);
    vi.mocked(patchEpic).mockRejectedValue(new Error("network error"));

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const user = userEvent.setup();

    render(<ManagerEffortControl projectId="proj1" epicId="EP-1" />, {
      wrapper: effortWrapper(qc),
    });

    await waitFor(() => {
      expect(screen.getByTestId("effort-btn-high")).toHaveAttribute("aria-pressed", "true");
    });

    await user.click(screen.getByTestId("effort-btn-max"));

    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith(
        ja.conversation.effortUpdateFailed,
        expect.objectContaining({ description: "network error" }),
      );
    });
  });
});
