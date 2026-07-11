/**
 * Plan approval (lifecycle redesign P2) tests:
 * - endpoints: approvePlan POST body / revokePlanApproval DELETE
 * - PlanApprovalBanner: visibility conditions (unapproved plan on the active
 *   trial), approvePlan call with the echoed backend hash, the approval
 *   message sent through the existing send path, and the stale-409 branch
 *   (refetch + notice, no message).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PlanApprovalBanner } from "@/components/features/conversation/plan-approval-banner";
import type { TasksResponse } from "@/lib/api/endpoints";
import { ApiError, approvePlan, getTasks } from "@/lib/api/endpoints";
import { I18nProvider } from "@/lib/i18n/provider";
import ja from "@/locales/ja";

// ---- endpoints ----

// The module-level vi.mock below is hoisted, so pull the real implementations
// for the endpoint tests explicitly.
const actualEndpoints =
  await vi.importActual<typeof import("@/lib/api/endpoints")>("@/lib/api/endpoints");

describe("plan approval endpoints", () => {
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

  it("approvePlan POSTs the echoed tasks_hash to /plan/approval", async () => {
    mockFetch.mockResolvedValueOnce(
      mockResponse(200, { tasks_hash: "abc123", approved_at: "2026-07-11T00:00:00Z" }),
    );
    await actualEndpoints.approvePlan("proj1", "EP-1", "abc123");
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toContain("/api/projects/proj1/epics/EP-1/plan/approval");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ tasks_hash: "abc123" });
  });

  it("approvePlan surfaces a stale snapshot as ApiError(409)", async () => {
    mockFetch.mockResolvedValueOnce(mockResponse(409, { detail: "Plan has changed" }));
    await expect(actualEndpoints.approvePlan("proj1", "EP-1", "stale")).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
    });
  });

  it("revokePlanApproval DELETEs /plan/approval (204 → void)", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 204,
      statusText: "204",
      json: () => Promise.reject(new Error("no body")),
    });
    await expect(actualEndpoints.revokePlanApproval("proj1", "EP-1")).resolves.toBeUndefined();
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toContain("/api/projects/proj1/epics/EP-1/plan/approval");
    expect(init.method).toBe("DELETE");
  });
});

// ---- PlanApprovalBanner ----

vi.mock("@/lib/api/endpoints", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/endpoints")>();
  return {
    ...actual,
    getTasks: vi.fn(),
    approvePlan: vi.fn(),
  };
});

vi.mock("sonner", () => ({
  toast: {
    error: vi.fn(),
    info: vi.fn(),
    success: vi.fn(),
  },
}));

function tasksResponse(overrides: Partial<TasksResponse> = {}): TasksResponse {
  return {
    tasks: [{ id: "T1", title: "Do the thing", status: "todo", contract: "does the thing" }],
    progress: { done: 0, total: 1 },
    plan_hash: "hash-1",
    approved_hash: null,
    plan_approved: false,
    ...overrides,
  };
}

function wrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <I18nProvider dict={ja} locale="ja">
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      </I18nProvider>
    );
  };
}

function newQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

describe("PlanApprovalBanner", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("shows the approve button while the current plan is unapproved", async () => {
    vi.mocked(getTasks).mockResolvedValue(tasksResponse());

    render(<PlanApprovalBanner projectId="proj1" epicId="EP-1" onSendMessage={vi.fn()} />, {
      wrapper: wrapper(newQueryClient()),
    });

    await waitFor(() => {
      expect(screen.getByTestId("approve-plan-btn")).toBeInTheDocument();
    });
  });

  it("renders nothing when the plan is already approved", async () => {
    vi.mocked(getTasks).mockResolvedValue(
      tasksResponse({ approved_hash: "hash-1", plan_approved: true }),
    );

    render(<PlanApprovalBanner projectId="proj1" epicId="EP-1" onSendMessage={vi.fn()} />, {
      wrapper: wrapper(newQueryClient()),
    });

    await waitFor(() => {
      expect(getTasks).toHaveBeenCalled();
    });
    expect(screen.queryByTestId("approve-plan-btn")).not.toBeInTheDocument();
  });

  it("renders nothing when there is no plan yet (zero tasks)", async () => {
    vi.mocked(getTasks).mockResolvedValue(
      tasksResponse({ tasks: [], progress: { done: 0, total: 0 } }),
    );

    render(<PlanApprovalBanner projectId="proj1" epicId="EP-1" onSendMessage={vi.fn()} />, {
      wrapper: wrapper(newQueryClient()),
    });

    await waitFor(() => {
      expect(getTasks).toHaveBeenCalled();
    });
    expect(screen.queryByTestId("approve-plan-btn")).not.toBeInTheDocument();
  });

  it("approves with the backend-computed hash and posts the approval message", async () => {
    vi.mocked(getTasks).mockResolvedValue(tasksResponse());
    vi.mocked(approvePlan).mockResolvedValue({
      tasks_hash: "hash-1",
      approved_at: "2026-07-11T00:00:00Z",
    });
    const onSendMessage = vi.fn();
    const user = userEvent.setup();

    render(<PlanApprovalBanner projectId="proj1" epicId="EP-1" onSendMessage={onSendMessage} />, {
      wrapper: wrapper(newQueryClient()),
    });

    await waitFor(() => {
      expect(screen.getByTestId("approve-plan-btn")).toBeInTheDocument();
    });
    await user.click(screen.getByTestId("approve-plan-btn"));

    await waitFor(() => {
      expect(approvePlan).toHaveBeenCalledWith("proj1", "EP-1", "hash-1");
    });
    await waitFor(() => {
      expect(onSendMessage).toHaveBeenCalledWith(ja.conversation.planApprovedMessage);
    });
  });

  it("on stale 409: refetches tasks, shows the stale notice, sends no message", async () => {
    const { toast } = await import("sonner");
    vi.mocked(getTasks).mockResolvedValue(tasksResponse());
    vi.mocked(approvePlan).mockRejectedValue(new ApiError(409, { detail: "changed" }, "API 409"));
    const onSendMessage = vi.fn();
    const user = userEvent.setup();

    render(<PlanApprovalBanner projectId="proj1" epicId="EP-1" onSendMessage={onSendMessage} />, {
      wrapper: wrapper(newQueryClient()),
    });

    await waitFor(() => {
      expect(screen.getByTestId("approve-plan-btn")).toBeInTheDocument();
    });
    const callsBeforeClick = vi.mocked(getTasks).mock.calls.length;
    await user.click(screen.getByTestId("approve-plan-btn"));

    await waitFor(() => {
      expect(toast.info).toHaveBeenCalledWith(ja.conversation.planStaleNotice);
    });
    // invalidateQueries on the tasks key → the plan is refetched
    await waitFor(() => {
      expect(vi.mocked(getTasks).mock.calls.length).toBeGreaterThan(callsBeforeClick);
    });
    expect(onSendMessage).not.toHaveBeenCalled();
  });

  it("on non-409 failure: shows an error toast and sends no message", async () => {
    const { toast } = await import("sonner");
    vi.mocked(getTasks).mockResolvedValue(tasksResponse());
    vi.mocked(approvePlan).mockRejectedValue(new Error("network down"));
    const onSendMessage = vi.fn();
    const user = userEvent.setup();

    render(<PlanApprovalBanner projectId="proj1" epicId="EP-1" onSendMessage={onSendMessage} />, {
      wrapper: wrapper(newQueryClient()),
    });

    await waitFor(() => {
      expect(screen.getByTestId("approve-plan-btn")).toBeInTheDocument();
    });
    await user.click(screen.getByTestId("approve-plan-btn"));

    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith(
        ja.conversation.planApproveFailed,
        expect.objectContaining({ description: "network down" }),
      );
    });
    expect(onSendMessage).not.toHaveBeenCalled();
  });
});
