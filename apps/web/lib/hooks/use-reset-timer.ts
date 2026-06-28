"use client";

import { useCallback, useEffect, useRef } from "react";

/**
 * useResetTimer — Returns `schedule(cb, opts?)`. Executes cb after opts.ms (default 2000) ms.
 * Any pending timer for the same opts.key is cancelled and restarted. All timers are cleared
 * via clearTimeout on unmount.
 *
 * Omitting key gives a single shared timer (for single-slot indicators / debounce use).
 * Passing a key gives an independent timer per key (for cases where multiple resets run
 * concurrently, such as per-row "Saved ✓"). Use useSaveState for a single boolean flag.
 */
export function useResetTimer(): (cb: () => void, opts?: { ms?: number; key?: string }) => void {
  const timers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  useEffect(
    () => () => {
      for (const timer of timers.current.values()) clearTimeout(timer);
      timers.current.clear();
    },
    [],
  );
  return useCallback((cb: () => void, opts?: { ms?: number; key?: string }) => {
    const key = opts?.key ?? "__default__";
    const ms = opts?.ms ?? 2000;
    const existing = timers.current.get(key);
    if (existing !== undefined) clearTimeout(existing);
    timers.current.set(
      key,
      setTimeout(() => {
        timers.current.delete(key);
        cb();
      }, ms),
    );
  }, []);
}
