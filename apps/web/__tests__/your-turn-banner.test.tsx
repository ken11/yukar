/**
 * Your-turn banner (lifecycle redesign) tests:
 * - The banner shows when the run parked on this thread (isYourTurn) and
 *   uses the neutral wording — no hardcoded role name ("Manager …" is gone).
 * - No banner when it is not the user's turn, and the run-failed banner wins.
 * - No synthetic "__awaiting__" bubble is ever rendered (the redesign removed it); the
 *   question is simply the last persisted assistant message.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EpicRunProvider } from "@/components/chrome/epic-run-context";
import { ThreadChatInner } from "@/components/features/conversation/thread-chat-inner";
import type { ThreadEntry } from "@/lib/api/endpoints";
import { emptyStreamState } from "@/lib/assistant-ui/runtime";
import { I18nProvider } from "@/lib/i18n/provider";
import ja from "@/locales/ja";

const managerThread: ThreadEntry = {
  id: "manager",
  title: "Manager",
  role: "manager",
  status: "active",
  task: null,
  repo: null,
  parent_thread_id: null,
};

function renderChat(props: Partial<Parameters<typeof ThreadChatInner>[0]> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const contextValue = {
    projectId: "proj1",
    epicId: "EP-1",
    project: null,
    epic: null,
    activityState: {
      runStatus: "waiting" as const,
      pausePending: false,
      runError: null,
      yourTurn: { threadId: "manager" },
      activeTrialId: "manager",
      currentRun: { threadId: "manager", role: "manager" as const },
      treeState: { manager: null, workers: {}, evaluators: {}, taskToWorker: {} },
      liveBuffers: {},
    },
    setPausePending: vi.fn(),
    clearLiveBuffer: vi.fn(),
    setMobileChromeHidden: vi.fn(),
  };
  return render(
    <I18nProvider dict={ja} locale="ja">
      <QueryClientProvider client={qc}>
        <EpicRunProvider value={contextValue}>
          <ThreadChatInner
            thread={managerThread}
            messages={[]}
            streamState={emptyStreamState()}
            isRunning={false}
            runFailed={false}
            runError={null}
            isYourTurn={false}
            onSendMessage={vi.fn()}
            isSending={false}
            isActiveTrial={false}
            {...props}
          />
        </EpicRunProvider>
      </QueryClientProvider>
    </I18nProvider>,
  );
}

beforeEach(() => {
  // jsdom lacks scrollIntoView (used by the auto-scroll effect)
  Element.prototype.scrollIntoView = vi.fn();
  // getTasks etc. fired by children must not hit the network
  vi.stubGlobal("fetch", () => Promise.resolve({ ok: false, json: () => Promise.reject() }));
});

describe("your-turn banner", () => {
  it("shows the neutral your-turn wording when the run parked on this thread", () => {
    renderChat({ isYourTurn: true });
    const banner = screen.getByText(ja.conversation.awaitingBanner);
    expect(banner).toBeTruthy();
    // Neutral wording — the old banner hardcoded the role ("the Manager is
    // waiting for your approval / answer"); that phrasing is gone.
    expect(ja.conversation.awaitingBanner).not.toContain("Manager");
  });

  it("does not show the banner when it is not the user's turn", () => {
    renderChat({ isYourTurn: false });
    expect(screen.queryByText(ja.conversation.awaitingBanner)).toBeNull();
  });

  it("the run-failed banner wins over the your-turn banner", () => {
    renderChat({ isYourTurn: true, runFailed: true, runError: "boom" });
    expect(screen.queryByText(ja.conversation.awaitingBanner)).toBeNull();
    expect(screen.getByText(`▲ ${ja.conversation.runFailedTitle}`)).toBeTruthy();
  });

  it("shows the Reviewer wording when the parked run rides a reviewer thread", () => {
    const reviewerThread: ThreadEntry = {
      id: "rev-1",
      title: "Reviewer",
      role: "reviewer",
      status: "active",
      task: null,
      repo: null,
      parent_thread_id: null,
    };
    renderChat({ thread: reviewerThread, isYourTurn: true });
    // Role-aware wording: the banner names the Reviewer, not the neutral text.
    expect(screen.getByText(ja.conversation.awaitingBannerReviewer)).toBeTruthy();
    expect(screen.queryByText(ja.conversation.awaitingBanner)).toBeNull();
  });

  it("renders persisted messages as-is — no synthetic '__awaiting__' bubble", () => {
    renderChat({
      isYourTurn: true,
      messages: [
        {
          id: "42",
          role: "assistant",
          content: [{ type: "text", text: "Which repo should I touch?" }],
        },
      ],
    });
    // The question is the agent's final persisted message, rendered normally.
    expect(screen.getByText("Which repo should I touch?")).toBeTruthy();
    expect(document.querySelector('[data-testid="agent-message"]')).toBeTruthy();
  });
});
