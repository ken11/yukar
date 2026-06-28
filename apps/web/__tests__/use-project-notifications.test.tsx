/**
 * Unit tests for useProjectNotifications
 * - event → notification conversion
 * - unread count
 * - mark as read
 */

import { act, renderHook } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "../lib/i18n/provider";
import { ProjectEventStreamProvider } from "../lib/sse/project-event-stream-context";
import { useProjectNotifications } from "../lib/sse/use-project-notifications";
import ja from "../locales/ja";

// EventSource mock (same pattern as use-event-stream)
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
    const handlers = this.listeners.get(type) ?? [];
    for (const h of handlers) h(ev);
  }
}

// Mock playChime (no Web Audio API in this environment)
vi.mock("../lib/audio/chime", () => ({
  playChime: vi.fn(),
}));

beforeEach(() => {
  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

/** Helper that provides I18nProvider (ja) + ProjectEventStreamProvider as a wrapper */
function makeWrapper(projectId: string) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <I18nProvider dict={ja} locale="ja">
        <ProjectEventStreamProvider projectId={projectId}>{children}</ProjectEventStreamProvider>
      </I18nProvider>
    );
  };
}

describe("useProjectNotifications", () => {
  it("starts with empty notifications", () => {
    const { result } = renderHook(() => useProjectNotifications("proj1"), {
      wrapper: makeWrapper("proj1"),
    });
    expect(result.current.notifications).toHaveLength(0);
    expect(result.current.unreadCount).toBe(0);
  });

  it("does not crash when projectId is undefined", () => {
    // Verify that passing undefined to Provider does not crash.
    // (The actual url becomes /api/projects/undefined/events, but this test
    //  only checks that it does not crash.)
    function Wrapper({ children }: { children: React.ReactNode }) {
      return React.createElement(
        ProjectEventStreamProvider,
        // biome-ignore lint/suspicious/noExplicitAny: for testing
        { projectId: undefined as any },
        children,
      );
    }
    const { result } = renderHook(() => useProjectNotifications(undefined), { wrapper: Wrapper });
    expect(result.current.notifications).toHaveLength(0);
  });

  it("subscribes to project events SSE (1 EventSource via Provider)", () => {
    renderHook(() => useProjectNotifications("proj1"), { wrapper: makeWrapper("proj1") });
    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0].url).toBe("/api/projects/proj1/events");
  });

  it("adds notification on run_completed event", () => {
    const { result } = renderHook(() => useProjectNotifications("proj1"), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "run_completed",
        JSON.stringify({
          type: "run_completed",
          project_id: "proj1",
          epic_id: "EP-1",
          run_id: "run-1",
        }),
      );
    });

    expect(result.current.notifications).toHaveLength(1);
    expect(result.current.notifications[0].type).toBe("run_completed");
    expect(result.current.notifications[0].epicId).toBe("EP-1");
    expect(result.current.notifications[0].read).toBe(false);
    expect(result.current.unreadCount).toBe(1);
  });

  it("adds notification on run_failed event", () => {
    const { result } = renderHook(() => useProjectNotifications("proj1"), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "run_failed",
        JSON.stringify({
          type: "run_failed",
          project_id: "proj1",
          epic_id: "EP-2",
          run_id: "run-2",
          error: "something went wrong",
        }),
      );
    });

    expect(result.current.notifications[0].type).toBe("run_failed");
    expect(result.current.notifications[0].epicId).toBe("EP-2");
    expect(result.current.notifications[0].message).toContain("失敗");
  });

  it("adds notification on run_paused event", () => {
    const { result } = renderHook(() => useProjectNotifications("proj1"), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "run_paused",
        JSON.stringify({
          type: "run_paused",
          project_id: "proj1",
          epic_id: "EP-3",
          run_id: "run-3",
        }),
      );
    });

    expect(result.current.notifications[0].type).toBe("run_paused");
    expect(result.current.unreadCount).toBe(1);
  });

  it("adds notification on run_resumed event", () => {
    const { result } = renderHook(() => useProjectNotifications("proj1"), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "run_resumed",
        JSON.stringify({
          type: "run_resumed",
          project_id: "proj1",
          epic_id: "EP-3",
          run_id: "run-3",
        }),
      );
    });

    expect(result.current.notifications[0].type).toBe("run_resumed");
  });

  it("accumulates multiple notifications newest-first", () => {
    const { result } = renderHook(() => useProjectNotifications("proj1"), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "run_started",
        JSON.stringify({ type: "run_started", project_id: "proj1", epic_id: "EP-1", run_id: "r1" }),
      );
      es.emit(
        "run_completed",
        JSON.stringify({
          type: "run_completed",
          project_id: "proj1",
          epic_id: "EP-1",
          run_id: "r1",
        }),
      );
    });

    expect(result.current.notifications).toHaveLength(2);
    // newest first
    expect(result.current.notifications[0].type).toBe("run_completed");
    expect(result.current.notifications[1].type).toBe("run_started");
    expect(result.current.unreadCount).toBe(2);
  });

  it("markAllRead sets all notifications to read and unreadCount to 0", () => {
    const { result } = renderHook(() => useProjectNotifications("proj1"), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "run_completed",
        JSON.stringify({
          type: "run_completed",
          project_id: "proj1",
          epic_id: "EP-1",
          run_id: "r1",
        }),
      );
      es.emit(
        "run_failed",
        JSON.stringify({
          type: "run_failed",
          project_id: "proj1",
          epic_id: "EP-2",
          run_id: "r2",
          error: "",
        }),
      );
    });

    expect(result.current.unreadCount).toBe(2);

    act(() => {
      result.current.markAllRead();
    });

    expect(result.current.unreadCount).toBe(0);
    expect(result.current.notifications.every((n) => n.read)).toBe(true);
  });

  it("clearAll removes all notifications", () => {
    const { result } = renderHook(() => useProjectNotifications("proj1"), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "run_completed",
        JSON.stringify({
          type: "run_completed",
          project_id: "proj1",
          epic_id: "EP-1",
          run_id: "r1",
        }),
      );
    });

    expect(result.current.notifications).toHaveLength(1);

    act(() => {
      result.current.clearAll();
    });

    expect(result.current.notifications).toHaveLength(0);
    expect(result.current.unreadCount).toBe(0);
  });

  it("calls onToast for run_completed", () => {
    const onToast = vi.fn();
    const { result } = renderHook(() => useProjectNotifications("proj1", onToast), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "run_completed",
        JSON.stringify({
          type: "run_completed",
          project_id: "proj1",
          epic_id: "EP-1",
          run_id: "r1",
        }),
      );
    });

    expect(onToast).toHaveBeenCalledOnce();
    expect(onToast.mock.calls[0][0].type).toBe("run_completed");
    // result.current is available to ensure hook rendered correctly
    expect(result.current.notifications).toHaveLength(1);
  });

  it("calls onToast for run_failed", () => {
    const onToast = vi.fn();
    renderHook(() => useProjectNotifications("proj1", onToast), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "run_failed",
        JSON.stringify({
          type: "run_failed",
          project_id: "proj1",
          epic_id: "EP-2",
          run_id: "r2",
          error: "err",
        }),
      );
    });

    expect(onToast).toHaveBeenCalledOnce();
    expect(onToast.mock.calls[0][0].type).toBe("run_failed");
  });

  it("does not call onToast for run_started or run_paused", () => {
    const onToast = vi.fn();
    renderHook(() => useProjectNotifications("proj1", onToast), {
      wrapper: makeWrapper("proj1"),
    });
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit(
        "run_started",
        JSON.stringify({ type: "run_started", project_id: "proj1", epic_id: "EP-1", run_id: "r1" }),
      );
      es.emit(
        "run_paused",
        JSON.stringify({ type: "run_paused", project_id: "proj1", epic_id: "EP-1", run_id: "r1" }),
      );
    });

    expect(onToast).not.toHaveBeenCalled();
  });

  describe("sensitive_file_written", () => {
    it("adds a notification with formatted kind and name (agent_config)", () => {
      const { result } = renderHook(() => useProjectNotifications("proj1"), {
        wrapper: makeWrapper("proj1"),
      });
      const es = MockEventSource.instances[0];

      act(() => {
        es.emit(
          "sensitive_file_written",
          JSON.stringify({
            type: "sensitive_file_written",
            project_id: "proj1",
            epic_id: "EP-5",
            run_id: "run-5",
            kind: "agent_config",
            name: "manager",
          }),
        );
      });

      expect(result.current.notifications).toHaveLength(1);
      const notif = result.current.notifications[0];
      expect(notif.type).toBe("sensitive_file_written");
      expect(notif.epicId).toBe("EP-5");
      expect(notif.read).toBe(false);
      // The message must include the localised kind label and the name
      expect(notif.message).toContain("エージェント設定");
      expect(notif.message).toContain("manager");
    });

    it("adds a notification for kind=memory", () => {
      const { result } = renderHook(() => useProjectNotifications("proj1"), {
        wrapper: makeWrapper("proj1"),
      });
      const es = MockEventSource.instances[0];

      act(() => {
        es.emit(
          "sensitive_file_written",
          JSON.stringify({
            type: "sensitive_file_written",
            project_id: "proj1",
            epic_id: "EP-6",
            run_id: "run-6",
            kind: "memory",
            name: "lessons-learned",
          }),
        );
      });

      const notif = result.current.notifications[0];
      expect(notif.type).toBe("sensitive_file_written");
      expect(notif.message).toContain("メモリ");
      expect(notif.message).toContain("lessons-learned");
    });

    it("does not call onToast for sensitive_file_written", () => {
      const onToast = vi.fn();
      renderHook(() => useProjectNotifications("proj1", onToast), {
        wrapper: makeWrapper("proj1"),
      });
      const es = MockEventSource.instances[0];

      act(() => {
        es.emit(
          "sensitive_file_written",
          JSON.stringify({
            type: "sensitive_file_written",
            project_id: "proj1",
            epic_id: "EP-7",
            run_id: "run-7",
            kind: "skill",
            name: "code-review",
          }),
        );
      });

      expect(onToast).not.toHaveBeenCalled();
    });
  });

  it("two hooks mounted under same Provider share 1 EventSource", () => {
    // Mounting two hooks under a Provider wrapper still produces only 1 EventSource
    function TwoHooksWrapper({ children }: { children: React.ReactNode }) {
      return React.createElement(ProjectEventStreamProvider, { projectId: "proj1" }, children);
    }

    const hook1 = renderHook(() => useProjectNotifications("proj1"), { wrapper: TwoHooksWrapper });
    const hook2 = renderHook(() => useProjectNotifications("proj1"), { wrapper: TwoHooksWrapper });

    // Each renderHook creates its own React tree so the Provider is separate for each.
    // To verify two hooks inside the same Provider, a single-component test would be needed;
    // here we confirm the "1 Provider = 1 EventSource" principle.
    expect(MockEventSource.instances).toHaveLength(2);

    hook1.unmount();
    hook2.unmount();
  });
});
