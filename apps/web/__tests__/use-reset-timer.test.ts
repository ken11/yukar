import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useResetTimer } from "../lib/hooks/use-reset-timer";

describe("useResetTimer", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("cb fires 2 seconds after calling schedule", () => {
    const { result } = renderHook(() => useResetTimer());
    const cb = vi.fn();

    act(() => {
      result.current(cb);
    });
    expect(cb).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it("consecutive schedule calls reset the timer (the previous timer is cancelled)", () => {
    const { result } = renderHook(() => useResetTimer());
    const cb = vi.fn();

    act(() => {
      result.current(cb);
    });
    // Re-schedule after 1 second
    act(() => {
      vi.advanceTimersByTime(1000);
      result.current(cb);
    });
    // 2 seconds from first call (1 second from re-schedule) — not fired yet
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(cb).not.toHaveBeenCalled();

    // Fires 2 seconds after re-schedule
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it("pending cb does not fire after unmount (cleanup)", () => {
    const { result, unmount } = renderHook(() => useResetTimer());
    const cb = vi.fn();

    act(() => {
      result.current(cb);
    });

    // Unmount before the timer fires
    unmount();

    // cb is not called even after advancing the timer
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(cb).not.toHaveBeenCalled();
  });

  it("custom delay can be specified with opts.ms", () => {
    const { result } = renderHook(() => useResetTimer());
    const cb = vi.fn();

    act(() => {
      result.current(cb, { ms: 500 });
    });
    act(() => {
      vi.advanceTimersByTime(499);
    });
    expect(cb).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it("timers with different keys operate independently (scheduling key A does not cancel key B)", () => {
    const { result } = renderHook(() => useResetTimer());
    const cbA = vi.fn();
    const cbB = vi.fn();

    // Schedule key "a" (fires after 2 seconds)
    act(() => {
      result.current(cbA, { key: "a" });
    });

    // Advance 1 second
    act(() => {
      vi.advanceTimersByTime(1000);
    });

    // Schedule key "b" (fires after another 2 seconds)
    act(() => {
      result.current(cbB, { key: "b" });
    });

    // Advance another 1 second (total 2 seconds) → key "a" should fire, key "b" should not yet
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(cbA).toHaveBeenCalledTimes(1);
    expect(cbB).not.toHaveBeenCalled();

    // Advance another 1 second (total 3 seconds) → key "b" fires too
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(cbA).toHaveBeenCalledTimes(1);
    expect(cbB).toHaveBeenCalledTimes(1);
  });

  it("all pending timers for multiple keys are cancelled on unmount", () => {
    const { result, unmount } = renderHook(() => useResetTimer());
    const cbA = vi.fn();
    const cbB = vi.fn();
    const cbDefault = vi.fn();

    act(() => {
      result.current(cbA, { key: "a" });
      result.current(cbB, { key: "b" });
      result.current(cbDefault);
    });

    unmount();

    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(cbA).not.toHaveBeenCalled();
    expect(cbB).not.toHaveBeenCalled();
    expect(cbDefault).not.toHaveBeenCalled();
  });
});
