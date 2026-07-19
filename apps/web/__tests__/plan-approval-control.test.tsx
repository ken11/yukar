/**
 * PlanApprovalControl (tasks page) — the always-available approval lever.
 *
 * The conversation banner depends on the active-trial thread being open and
 * the cached snapshot being fresh; this control must work from the passed-in
 * backend truth alone:
 * - unapproved plan → approve button → POST + wake message to the active
 *   manager thread (getEpic → active_thread_id, fallback "manager")
 * - approved plan → revoke button → DELETE
 * - wake failure is NOT an approval failure (info toast, no error)
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PlanApprovalControl } from "@/components/features/tasks/plan-approval-control";
import type { Epic, TasksResponse } from "@/lib/api/endpoints";
import { approvePlan, getEpic, postMessage, revokePlanApproval } from "@/lib/api/endpoints";
import { I18nProvider } from "@/lib/i18n/provider";
import ja from "@/locales/ja";

vi.mock("@/lib/api/endpoints", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/endpoints")>();
  return {
    ...actual,
    approvePlan: vi.fn(),
    revokePlanApproval: vi.fn(),
    getEpic: vi.fn(),
    postMessage: vi.fn(),
  };
});

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), info: vi.fn(), success: vi.fn() },
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

function renderControl(tasksFile: TasksResponse) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<PlanApprovalControl projectId="proj1" epicId="EP-1" tasksFile={tasksFile} />, {
    wrapper: ({ children }) => (
      <I18nProvider dict={ja} locale="ja">
        <QueryClientProvider client={qc}>{children}</QueryClientProvider>
      </I18nProvider>
    ),
  });
}

describe("PlanApprovalControl", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders nothing without a plan", () => {
    renderControl(tasksResponse({ tasks: [] }));
    expect(screen.queryByTestId("tasks-approve-plan-btn")).not.toBeInTheDocument();
    expect(screen.queryByTestId("tasks-revoke-approval-btn")).not.toBeInTheDocument();
  });

  it("approves the displayed hash and posts the wake message to the active trial thread", async () => {
    vi.mocked(approvePlan).mockResolvedValue({
      tasks_hash: "hash-1",
      approved_at: "2026-07-19T00:00:00Z",
    });
    vi.mocked(getEpic).mockResolvedValue({ active_thread_id: "trial-7" } as Epic);
    vi.mocked(postMessage).mockResolvedValue({} as never);

    renderControl(tasksResponse());
    await userEvent.click(screen.getByTestId("tasks-approve-plan-btn"));

    await waitFor(() => {
      expect(approvePlan).toHaveBeenCalledWith("proj1", "EP-1", "hash-1");
      expect(postMessage).toHaveBeenCalledWith("proj1", "EP-1", "trial-7", {
        content: ja.conversation.planApprovedMessage,
        role: "user",
      });
    });
  });

  it("falls back to the 'manager' thread when the epic has no active_thread_id", async () => {
    vi.mocked(approvePlan).mockResolvedValue({
      tasks_hash: "hash-1",
      approved_at: "2026-07-19T00:00:00Z",
    });
    vi.mocked(getEpic).mockResolvedValue({ active_thread_id: null } as Epic);
    vi.mocked(postMessage).mockResolvedValue({} as never);

    renderControl(tasksResponse());
    await userEvent.click(screen.getByTestId("tasks-approve-plan-btn"));

    await waitFor(() => {
      expect(postMessage).toHaveBeenCalledWith(
        "proj1",
        "EP-1",
        "manager",
        expect.objectContaining({ role: "user" }),
      );
    });
  });

  it("a failed wake is an info notice, not an approval failure", async () => {
    const { toast } = await import("sonner");
    vi.mocked(approvePlan).mockResolvedValue({
      tasks_hash: "hash-1",
      approved_at: "2026-07-19T00:00:00Z",
    });
    vi.mocked(getEpic).mockResolvedValue({ active_thread_id: "trial-7" } as Epic);
    vi.mocked(postMessage).mockRejectedValue(new Error("409 reviewer executing"));

    renderControl(tasksResponse());
    await userEvent.click(screen.getByTestId("tasks-approve-plan-btn"));

    await waitFor(() => {
      expect(toast.info).toHaveBeenCalledWith(ja.tasks.approvedButWakeFailed);
    });
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("shows revoke for an approved plan and DELETEs the approval", async () => {
    vi.mocked(revokePlanApproval).mockResolvedValue();

    renderControl(tasksResponse({ approved_hash: "hash-1", plan_approved: true }));
    expect(screen.queryByTestId("tasks-approve-plan-btn")).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId("tasks-revoke-approval-btn"));
    await waitFor(() => {
      expect(revokePlanApproval).toHaveBeenCalledWith("proj1", "EP-1");
    });
  });
});
