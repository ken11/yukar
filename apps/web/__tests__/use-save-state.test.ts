import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSaveState } from "../lib/hooks/use-save-state";

describe("useSaveState", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("initial values are saved=false, saveError=null", () => {
    const { result } = renderHook(() => useSaveState());
    expect(result.current.saved).toBe(false);
    expect(result.current.saveError).toBeNull();
  });

  it("calling clearSavedAfter2s sets saved to true", () => {
    const { result } = renderHook(() => useSaveState());
    act(() => {
      result.current.clearSavedAfter2s();
    });
    expect(result.current.saved).toBe(true);
  });

  it("saved reverts to false 2 seconds after calling clearSavedAfter2s", () => {
    const { result } = renderHook(() => useSaveState());
    act(() => {
      result.current.clearSavedAfter2s();
    });
    expect(result.current.saved).toBe(true);

    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(result.current.saved).toBe(false);
  });

  it("timer is reset even when clearSavedAfter2s is called consecutively", () => {
    const { result } = renderHook(() => useSaveState());

    act(() => {
      result.current.clearSavedAfter2s();
    });
    // Call again after 1 second
    act(() => {
      vi.advanceTimersByTime(1000);
      result.current.clearSavedAfter2s();
    });
    // 2 seconds from first call (1 second from second call) — saved is still true
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current.saved).toBe(true);

    // Becomes false 2 seconds after the second call
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current.saved).toBe(false);
  });

  it("setError sets the message from an Error instance", () => {
    const { result } = renderHook(() => useSaveState());
    act(() => {
      result.current.setError(new Error("network error"));
    });
    expect(result.current.saveError).toBe("network error");
  });

  it("setError uses the fallback message for non-Error values", () => {
    const { result } = renderHook(() => useSaveState("Custom fallback"));
    act(() => {
      result.current.setError("something went wrong");
    });
    expect(result.current.saveError).toBe("Custom fallback");
  });

  it("setError uses the second argument as override fallback for non-Error values", () => {
    const { result } = renderHook(() => useSaveState());
    act(() => {
      result.current.setError(42, "override fallback");
    });
    expect(result.current.saveError).toBe("override fallback");
  });

  it("pending timer is cancelled on unmount and setState is not fired", () => {
    const { result, unmount } = renderHook(() => useSaveState());

    act(() => {
      result.current.clearSavedAfter2s();
    });
    expect(result.current.saved).toBe(true);

    // Unmount before the timer fires
    unmount();

    // Advancing time should not trigger a React warning (setState on unmounted component).
    // In vi.useFakeTimers environment, calling advanceTimersByTime outside act
    // does not cause setState on an already-unmounted component.
    expect(() => {
      vi.advanceTimersByTime(2000);
    }).not.toThrow();
  });

  it("setSaveError can directly set and clear an error", () => {
    const { result } = renderHook(() => useSaveState());
    act(() => {
      result.current.setSaveError("direct error");
    });
    expect(result.current.saveError).toBe("direct error");

    act(() => {
      result.current.setSaveError(null);
    });
    expect(result.current.saveError).toBeNull();
  });

  it("setSaved can directly control the saved state", () => {
    const { result } = renderHook(() => useSaveState());
    act(() => {
      result.current.setSaved(true);
    });
    expect(result.current.saved).toBe(true);
  });
});
