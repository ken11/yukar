/**
 * Tests for useSystemStatusToast
 *
 * - Shows a warning toast when watch_enabled=true && watcher_ok=false
 * - Does not show a toast when the watcher is healthy (watcher_ok=true)
 * - Does not show a toast when watch_enabled=false
 * - Silently ignores fetch errors
 */

import { renderHook } from "@testing-library/react";
import type React from "react";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "../lib/i18n/provider";
import {
  _resetSystemStatusSessionGuard,
  useSystemStatusToast,
} from "../lib/sse/use-system-status-toast";
import ja from "../locales/ja";

// Mock the endpoints module so we don't make real HTTP requests.
vi.mock("../lib/api/endpoints", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../lib/api/endpoints")>();
  return {
    ...mod,
    getSystemStatus: vi.fn(),
  };
});

// Mock sonner toast
vi.mock("sonner", () => ({
  toast: {
    warning: vi.fn(),
    success: vi.fn(),
    error: vi.fn(),
  },
}));

import { getSystemStatus } from "../lib/api/endpoints";

const mockGetSystemStatus = vi.mocked(getSystemStatus);

function makeWrapper() {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <I18nProvider dict={ja} locale="ja">
        {children}
      </I18nProvider>
    );
  };
}

beforeEach(() => {
  // Reset the module-level session guard so each test starts fresh.
  _resetSystemStatusSessionGuard();
  vi.clearAllMocks();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("useSystemStatusToast", () => {
  it("shows a warning toast when watcher is enabled but failed to start", async () => {
    mockGetSystemStatus.mockResolvedValueOnce({
      indexer_watcher: {
        watch_enabled: true,
        watcher_ok: false,
        reason: "inotify limit reached",
        watched_repo_count: 0,
      },
    });

    const { unmount } = renderHook(() => useSystemStatusToast(), { wrapper: makeWrapper() });

    // Wait for the async fetch to complete
    await vi.waitFor(() => {
      expect(toast.warning).toHaveBeenCalledOnce();
    });

    const call = vi.mocked(toast.warning).mock.calls[0];
    const message = call[0] as string;
    // The message should include the locale string and the reason
    expect(message).toContain("ファイル監視");
    expect(message).toContain("inotify limit reached");

    unmount();
  });

  it("shows warning toast without reason when reason is null", async () => {
    mockGetSystemStatus.mockResolvedValueOnce({
      indexer_watcher: {
        watch_enabled: true,
        watcher_ok: false,
        reason: null,
        watched_repo_count: 0,
      },
    });

    const { unmount } = renderHook(() => useSystemStatusToast(), { wrapper: makeWrapper() });

    await vi.waitFor(() => {
      expect(toast.warning).toHaveBeenCalledOnce();
    });

    const call = vi.mocked(toast.warning).mock.calls[0];
    const message = call[0] as string;
    expect(message).toContain("ファイル監視");
    // No parenthetical reason suffix
    expect(message).not.toContain("(null)");

    unmount();
  });

  it("does not show a toast when watcher is healthy", async () => {
    mockGetSystemStatus.mockResolvedValueOnce({
      indexer_watcher: {
        watch_enabled: true,
        watcher_ok: true,
        reason: null,
        watched_repo_count: 3,
      },
    });

    const { unmount } = renderHook(() => useSystemStatusToast(), { wrapper: makeWrapper() });

    // Give time for async fetch
    await vi.waitFor(() => {
      expect(mockGetSystemStatus).toHaveBeenCalledOnce();
    });

    expect(toast.warning).not.toHaveBeenCalled();
    unmount();
  });

  it("does not show a toast when watch_enabled is false", async () => {
    mockGetSystemStatus.mockResolvedValueOnce({
      indexer_watcher: {
        watch_enabled: false,
        watcher_ok: false,
        reason: null,
        watched_repo_count: 0,
      },
    });

    const { unmount } = renderHook(() => useSystemStatusToast(), { wrapper: makeWrapper() });

    await vi.waitFor(() => {
      expect(mockGetSystemStatus).toHaveBeenCalledOnce();
    });

    expect(toast.warning).not.toHaveBeenCalled();
    unmount();
  });

  it("silently ignores fetch errors without showing a toast", async () => {
    mockGetSystemStatus.mockRejectedValueOnce(new Error("network error"));

    const { unmount } = renderHook(() => useSystemStatusToast(), { wrapper: makeWrapper() });

    await vi.waitFor(() => {
      expect(mockGetSystemStatus).toHaveBeenCalledOnce();
    });

    expect(toast.warning).not.toHaveBeenCalled();
    unmount();
  });

  it("fetches getSystemStatus exactly once even when the hook is mounted twice", async () => {
    // The module-level `checkedThisSession` guard must prevent a second fetch
    // regardless of how many hook instances are alive concurrently.
    mockGetSystemStatus.mockResolvedValue({
      indexer_watcher: {
        watch_enabled: true,
        watcher_ok: true,
        reason: null,
        watched_repo_count: 1,
      },
    });

    const wrapper = makeWrapper();
    const { unmount: unmount1 } = renderHook(() => useSystemStatusToast(), { wrapper });
    const { unmount: unmount2 } = renderHook(() => useSystemStatusToast(), { wrapper });

    await vi.waitFor(() => {
      expect(mockGetSystemStatus).toHaveBeenCalledOnce();
    });

    // Second mount must not have triggered an additional fetch.
    expect(mockGetSystemStatus).toHaveBeenCalledTimes(1);

    unmount1();
    unmount2();
  });
});
