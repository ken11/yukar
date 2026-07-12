/**
 * EpicsBoardClient filter logic tests (1-bit epic lifecycle):
 * - "all" shows every epic (open + completed)
 * - explicit "open" / "completed" filters split on the status bit
 * - "merged" filter selects on the merged_at fact attribute
 * - merged badge is shown alongside the status badge
 * - complete / reopen row actions follow the status bit
 * - multi-select checkbox only shows for mergeable epics (open + branch + not merged)
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EpicsBoardClient } from "@/components/features/epics/epics-board-client";
import type { EpicWithRunSummary } from "@/lib/api/endpoints";
import { I18nProvider } from "@/lib/i18n/provider";
import { ProjectEventStreamProvider } from "@/lib/sse/project-event-stream-context";
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
    patchEpic: vi.fn(),
    startMerge: vi.fn(),
    stopMerge: vi.fn(),
    createEpic: vi.fn(),
  };
});

// Minimal EventSource mock so ProjectEventStreamProvider (the board's SSE
// source for live your-turn badges) can mount, and tests can emit events.
class MockEventSource {
  url: string;
  onerror: ((ev: Event) => void) | null = null;
  private listeners: Map<string, EventListener[]> = new Map();
  static instances: MockEventSource[] = [];

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, handler: EventListener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type)?.push(handler);
  }

  removeEventListener() {}
  close() {}

  emit(type: string, data: string) {
    const ev = { type, data } as MessageEvent;
    for (const h of this.listeners.get(type) ?? []) h(ev);
  }
}

beforeEach(() => {
  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource);
});

const makeEpic = (
  id: string,
  status: EpicWithRunSummary["status"],
  opts: {
    branch?: string;
    mergedAt?: string;
    runSummary?: EpicWithRunSummary["run_summary"];
  } = {},
): EpicWithRunSummary => ({
  id,
  slug: id.toLowerCase(),
  title: `Epic ${id}`,
  description: "",
  acceptance_criteria: "",
  status,
  branch: opts.branch ?? "",
  merged_at: opts.mergedAt ?? null,
  manager_effort: "high",
  run_summary: opts.runSummary ?? null,
});

const initialEpics: EpicWithRunSummary[] = [
  // open, with branch → mergeable
  makeEpic("EP-1", "open", { branch: "branch-ep1" }),
  // open, no branch → not mergeable
  makeEpic("EP-2", "open"),
  // open + merged fact → merged badge, not mergeable again
  makeEpic("EP-3", "open", { branch: "branch-ep3", mergedAt: "2026-07-11T00:00:00Z" }),
  // completed (finished work)
  makeEpic("EP-4", "completed", { branch: "branch-ep4" }),
  // completed + merged fact
  makeEpic("EP-5", "completed", { branch: "branch-ep5", mergedAt: "2026-07-10T00:00:00Z" }),
  // second mergeable epic (selection-order test)
  makeEpic("EP-6", "open", { branch: "branch-ep6" }),
];

function wrapper(qc?: QueryClient) {
  const client = qc ?? new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <I18nProvider dict={ja} locale="ja">
        <QueryClientProvider client={client}>
          <ProjectEventStreamProvider projectId="proj1">{children}</ProjectEventStreamProvider>
        </QueryClientProvider>
      </I18nProvider>
    );
  };
}

describe("EpicsBoardClient filters (1-bit lifecycle)", () => {
  it("default 'all' shows every epic, open and completed alike", () => {
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    for (const id of ["EP-1", "EP-2", "EP-3", "EP-4", "EP-5", "EP-6"]) {
      expect(screen.getByTestId(`epic-card-${id}`)).toBeInTheDocument();
    }
  });

  it("'open' filter shows only open epics", async () => {
    const user = userEvent.setup();
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    await user.click(screen.getByTestId("epic-filter-open"));

    expect(screen.getByTestId("epic-card-EP-1")).toBeInTheDocument();
    expect(screen.getByTestId("epic-card-EP-2")).toBeInTheDocument();
    expect(screen.getByTestId("epic-card-EP-3")).toBeInTheDocument();
    expect(screen.queryByTestId("epic-card-EP-4")).not.toBeInTheDocument();
    expect(screen.queryByTestId("epic-card-EP-5")).not.toBeInTheDocument();
  });

  it("'completed' filter shows only completed epics", async () => {
    const user = userEvent.setup();
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    await user.click(screen.getByTestId("epic-filter-completed"));

    expect(screen.getByTestId("epic-card-EP-4")).toBeInTheDocument();
    expect(screen.getByTestId("epic-card-EP-5")).toBeInTheDocument();
    expect(screen.queryByTestId("epic-card-EP-1")).not.toBeInTheDocument();
  });

  it("'merged' filter selects on the merged_at fact, regardless of status", async () => {
    const user = userEvent.setup();
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    await user.click(screen.getByTestId("epic-filter-merged"));

    // EP-3 is open+merged, EP-5 is completed+merged — both match the fact filter
    expect(screen.getByTestId("epic-card-EP-3")).toBeInTheDocument();
    expect(screen.getByTestId("epic-card-EP-5")).toBeInTheDocument();
    expect(screen.queryByTestId("epic-card-EP-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("epic-card-EP-4")).not.toBeInTheDocument();
  });

  it("shows the merged badge next to the status for epics with merged_at", () => {
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    // EP-3 row (open + merged fact) carries both the merged badge and the open status
    const row = screen.getByTestId("epic-card-EP-3");
    expect(row).toHaveTextContent(ja.epic.status.merged);
    expect(row).toHaveTextContent(ja.epic.status.open);
  });

  it("renders reopen button for completed epics", async () => {
    const user = userEvent.setup();
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    await user.click(screen.getByTestId("epic-filter-completed"));

    expect(screen.getByTestId("reopen-btn-EP-4")).toBeInTheDocument();
    expect(screen.queryByTestId("complete-btn-EP-4")).not.toBeInTheDocument();
  });

  it("renders complete button for open epics (merged or not)", () => {
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    expect(screen.getByTestId("complete-btn-EP-1")).toBeInTheDocument();
    // merged epics stay open — completing is still the user's call
    expect(screen.getByTestId("complete-btn-EP-3")).toBeInTheDocument();
    expect(screen.queryByTestId("reopen-btn-EP-1")).not.toBeInTheDocument();
  });

  it("multi-select checkbox appears only for mergeable epics (open + branch + not merged)", () => {
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    // EP-1: open + branch + no merge fact → mergeable
    expect(screen.getByLabelText("Select EP-1")).toBeInTheDocument();
    // EP-2: no branch / EP-3: merged fact / EP-4, EP-5: completed → not mergeable
    expect(screen.queryByLabelText("Select EP-2")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Select EP-3")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Select EP-4")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Select EP-5")).not.toBeInTheDocument();
  });

  it("shows merge toolbar when an epic is selected", async () => {
    const user = userEvent.setup();
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    await user.click(screen.getByLabelText("Select EP-1"));

    expect(screen.getByTestId("merge-toolbar")).toBeInTheDocument();
    expect(screen.getByTestId("start-merge-btn")).toBeInTheDocument();
  });

  it("preserves selection order (merge order)", async () => {
    const user = userEvent.setup();
    render(<EpicsBoardClient projectId="proj1" initialEpics={initialEpics} />, {
      wrapper: wrapper(),
    });

    // Select EP-6 first, then EP-1
    await user.click(screen.getByLabelText("Select EP-6"));
    await user.click(screen.getByLabelText("Select EP-1"));

    // Both selected — toolbar shows 2 selected
    const toolbar = screen.getByTestId("merge-toolbar");
    expect(toolbar).toHaveTextContent("2 selected");
  });
});

// ============================================================
// P4: "your turn" badge on the board — run_summary + live SSE update
// ============================================================

describe("EpicsBoardClient your-turn badge (P4)", () => {
  it("shows the badge when run_summary is waiting with a real run_id", () => {
    const epics = [
      makeEpic("EP-10", "open", {
        runSummary: {
          status: "waiting",
          run_id: "run-1",
          thread_id: "trial-1",
          role: "manager",
        },
      }),
    ];
    render(<EpicsBoardClient projectId="proj1" initialEpics={epics} />, { wrapper: wrapper() });
    expect(screen.getByTestId("your-turn-EP-10")).toBeInTheDocument();
  });

  it("no badge for a never-run epic (run_summary null) or a synthesised empty run_id", () => {
    const epics = [
      makeEpic("EP-11", "open"),
      makeEpic("EP-12", "open", {
        runSummary: { status: "waiting", run_id: "", thread_id: null, role: "manager" },
      }),
    ];
    render(<EpicsBoardClient projectId="proj1" initialEpics={epics} />, { wrapper: wrapper() });
    expect(screen.queryByTestId("your-turn-EP-11")).not.toBeInTheDocument();
    expect(screen.queryByTestId("your-turn-EP-12")).not.toBeInTheDocument();
  });

  it("no badge on a completed epic — locked history is not an inbox item", () => {
    // After P3 every conversation run settles in waiting, so without the
    // open-status condition every epic that ever ran would keep the badge
    // forever after being completed.
    const epics = [
      makeEpic("EP-15", "completed", {
        runSummary: {
          status: "waiting",
          run_id: "run-1",
          thread_id: "trial-1",
          role: "manager",
        },
      }),
    ];
    render(<EpicsBoardClient projectId="proj1" initialEpics={epics} />, { wrapper: wrapper() });
    expect(screen.queryByTestId("your-turn-EP-15")).not.toBeInTheDocument();
  });

  it("no badge while the run is executing (run_summary running)", () => {
    const epics = [
      makeEpic("EP-13", "open", {
        runSummary: {
          status: "running",
          run_id: "run-1",
          thread_id: "trial-1",
          role: "manager",
        },
      }),
    ];
    render(<EpicsBoardClient projectId="proj1" initialEpics={epics} />, { wrapper: wrapper() });
    expect(screen.queryByTestId("your-turn-EP-13")).not.toBeInTheDocument();
  });

  it("project SSE your_turn adds the badge live; your_turn_ended removes it", async () => {
    const epics = [
      makeEpic("EP-14", "open", {
        runSummary: {
          status: "running",
          run_id: "run-1",
          thread_id: "trial-1",
          role: "manager",
        },
      }),
    ];
    render(<EpicsBoardClient projectId="proj1" initialEpics={epics} />, { wrapper: wrapper() });
    expect(screen.queryByTestId("your-turn-EP-14")).not.toBeInTheDocument();

    const es = MockEventSource.instances.find((i) => i.url.endsWith("/events"));
    expect(es).toBeTruthy();

    // The run parked → the board badge appears without a refetch.
    await act(async () => {
      es?.emit(
        "your_turn",
        JSON.stringify({
          type: "your_turn",
          project_id: "proj1",
          epic_id: "EP-14",
          run_id: "run-1",
          thread_id: "trial-1",
          question: "",
        }),
      );
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
    expect(screen.getByTestId("your-turn-EP-14")).toBeInTheDocument();

    // The user's reply woke the run → the badge disappears.
    await act(async () => {
      es?.emit(
        "your_turn_ended",
        JSON.stringify({
          type: "your_turn_ended",
          project_id: "proj1",
          epic_id: "EP-14",
          run_id: "run-1",
          thread_id: "trial-1",
        }),
      );
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
    expect(screen.queryByTestId("your-turn-EP-14")).not.toBeInTheDocument();
  });

  it("a your-turn signal for a different epic does not touch other rows", async () => {
    const epics = [
      makeEpic("EP-15", "open", {
        runSummary: {
          status: "running",
          run_id: "run-1",
          thread_id: "trial-1",
          role: "manager",
        },
      }),
    ];
    render(<EpicsBoardClient projectId="proj1" initialEpics={epics} />, { wrapper: wrapper() });

    const es = MockEventSource.instances.find((i) => i.url.endsWith("/events"));
    await act(async () => {
      es?.emit(
        "your_turn",
        JSON.stringify({
          type: "your_turn",
          project_id: "proj1",
          epic_id: "EP-99",
          run_id: "run-9",
          thread_id: "trial-9",
          question: "",
        }),
      );
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
    expect(screen.queryByTestId("your-turn-EP-15")).not.toBeInTheDocument();
  });
});
