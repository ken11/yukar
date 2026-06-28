"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export interface UseSaveStateResult {
  saved: boolean;
  setSaved: (value: boolean) => void;
  saveError: string | null;
  setSaveError: (err: string | null) => void;
  /** Clears saved to false 2 seconds after a successful save (includes clearTimeout on unmount/re-run) */
  clearSavedAfter2s: () => void;
  /** Sets the error as a string (uses .message if err instanceof Error) */
  setError: (err: unknown, fallback?: string) => void;
}

/**
 * useSaveState — Hook that consolidates save UI state (saved / saveError / timer).
 *
 * #14: Unifies the saved/saveError useState, `err instanceof Error` check, and setTimeout 2s clear
 * that were reimplemented across 6 sections (settings-form / agent-profiles / mcp / agent-configs /
 * skills / repos). The setTimeout cleanup (clearTimeout on unmount) is encapsulated inside the hook.
 *
 * @param fallbackErrorMsg - Default message when err is not an Error instance
 */
export function useSaveState(fallbackErrorMsg = "Save failed"): UseSaveStateResult {
  const [saved, setSaved] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Clear any lingering timer on unmount to prevent setState leak
  useEffect(() => {
    return () => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
      }
    };
  }, []);

  const clearSavedAfter2s = useCallback(() => {
    // Cancel the existing timer and restart (handles consecutive saves)
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
    }
    setSaved(true);
    timerRef.current = setTimeout(() => {
      setSaved(false);
      timerRef.current = null;
    }, 2000);
  }, []);

  const setError = useCallback(
    (err: unknown, fallback = fallbackErrorMsg) => {
      setSaveError(err instanceof Error ? err.message : fallback);
    },
    [fallbackErrorMsg],
  );

  return {
    saved,
    setSaved,
    saveError,
    setSaveError,
    clearSavedAfter2s,
    setError,
  };
}
