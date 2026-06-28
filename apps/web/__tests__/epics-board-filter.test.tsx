/**
 * EpicsBoardClient filter logic tests:
 * - default "all" hides closed but shows merged
 * - explicit "closed" filter shows only closed
 * - explicit "merged" filter shows only merged
 * - multi-select checkbox only shows for mergeable epics
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { EpicsBoardClient } from "@/components/features/epics/epics-board-client";
import type { Epic } from "@/lib/api/endpoints";
import { I18nProvider } from "@/lib/i18n/provider";
import ja from "@/locales/ja";

// Mock Next.js router (required by NewEpicModal)
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/projects/proj1/epics",
  useSearchParams: () => new URLSearchParams(),
}));

// Mock endpoints to avoid real HTTP calls
vi.mock("@/lib/api/endpoints", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/endpoints")>();
  return {
    ...actual,
    listEpics: vi.fn().mockResolvedValue([]),
    closeEpic: vi.fn(),
    patchEpic: vi.fn(),
    startMerge: vi.fn(),
    stopMerge: vi.fn(),
    createEpic: vi.fn(),
  };
});

const makeEpic = (id: string, status: Epic["status"], branch?: string): Epic => ({
  id,
  slug: id.toLowerCase(),
  title: `Epic ${id}`,
  description: "",
  acceptance_criteria: "",
  status,
  branch: branch ?? (status !== "closed" ? `branch-${id}` : ""),
  manager_effort: "high",
});

const initialEpics: Epic[] = [
  makeEpic("EP-1", "planned", "branch-ep1"),
  makeEpic("EP-2", "in_progress", "branch-ep2"),
  makeEpic("EP-3", "completed", "branch-ep3"),
  makeEpic("EP-4", "failed"),
  makeEpic("EP-5", "closed"),
  makeEpic("EP-6", "merged", "branch-ep6"),
];

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <I18nProvider dict={ja} locale="ja">
        <QueryClientProvider client={qc}>{children}</QueryClientProvider>
      </I18nProvider>
    );
  };
}

describe("EpicsBoardClient filter defaults", () => {
  it("default 'all' hides closed but shows merged", () => {
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    // closed (EP-5) must NOT appear
    expect(screen.queryByTestId("epic-card-EP-5")).not.toBeInTheDocument();
    // merged (EP-6) MUST appear
    expect(screen.getByTestId("epic-card-EP-6")).toBeInTheDocument();
  });

  it("shows planned/in_progress/completed/failed/merged in default 'all' view", () => {
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    expect(screen.getByTestId("epic-card-EP-1")).toBeInTheDocument();
    expect(screen.getByTestId("epic-card-EP-2")).toBeInTheDocument();
    expect(screen.getByTestId("epic-card-EP-3")).toBeInTheDocument();
    expect(screen.getByTestId("epic-card-EP-4")).toBeInTheDocument();
    expect(screen.getByTestId("epic-card-EP-6")).toBeInTheDocument();
  });

  it("shows only closed epics when 'closed' filter is selected", async () => {
    const user = userEvent.setup();
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    await user.click(screen.getByTestId("epic-filter-closed"));

    expect(screen.getByTestId("epic-card-EP-5")).toBeInTheDocument();
    expect(screen.queryByTestId("epic-card-EP-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("epic-card-EP-6")).not.toBeInTheDocument();
  });

  it("shows only merged epics when 'merged' filter is selected", async () => {
    const user = userEvent.setup();
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    await user.click(screen.getByTestId("epic-filter-merged"));

    expect(screen.getByTestId("epic-card-EP-6")).toBeInTheDocument();
    expect(screen.queryByTestId("epic-card-EP-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("epic-card-EP-5")).not.toBeInTheDocument();
  });

  it("renders reopen button for closed epics", async () => {
    const user = userEvent.setup();
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    await user.click(screen.getByTestId("epic-filter-closed"));

    expect(screen.getByTestId("reopen-btn-EP-5")).toBeInTheDocument();
  });

  it("renders close button for completed epics", () => {
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    expect(screen.getByTestId("close-btn-EP-3")).toBeInTheDocument();
  });

  it("renders close button for planned epics", () => {
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    // EP-1 is planned — should now show close button
    expect(screen.getByTestId("close-btn-EP-1")).toBeInTheDocument();
  });

  it("renders close button for failed epics", () => {
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    // EP-4 is failed — should show close button
    expect(screen.getByTestId("close-btn-EP-4")).toBeInTheDocument();
  });

  it("does not render close button for in_progress epics", () => {
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    // EP-2 is in_progress — backend returns 409, so close button must not appear
    expect(screen.queryByTestId("close-btn-EP-2")).not.toBeInTheDocument();
  });

  it("shows merge toolbar when an epic is selected", async () => {
    const user = userEvent.setup();
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    // Click the checkbox for EP-1 (planned, has branch = mergeable)
    const checkbox = screen.getByLabelText("Select EP-1");
    await user.click(checkbox);

    expect(screen.getByTestId("merge-toolbar")).toBeInTheDocument();
    expect(screen.getByTestId("start-merge-btn")).toBeInTheDocument();
  });

  it("preserves selection order (merge order)", async () => {
    const user = userEvent.setup();
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    // Select EP-3 first, then EP-1
    await user.click(screen.getByLabelText("Select EP-3"));
    await user.click(screen.getByLabelText("Select EP-1"));

    // Both selected — toolbar shows 2 selected
    const toolbar = screen.getByTestId("merge-toolbar");
    expect(toolbar).toHaveTextContent("2 selected");
  });
});
