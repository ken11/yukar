"use client";

import { useSyncExternalStore } from "react";

/**
 * useIsDesktop — true at the Tailwind `md` breakpoint (≥768px) and up.
 *
 * Backed by useSyncExternalStore so it is SSR-safe: the server snapshot is
 * `true` (desktop), which matches both the primary/tested surface and the
 * first client render, so desktop never flashes. A real mobile client takes
 * exactly one post-hydration reflow (desktop → mobile).
 *
 * The 768px query is deliberately kept in lock-step with Tailwind's `md:`
 * utilities: this hook decides which chrome MOUNTS (mobile bands vs. the
 * desktop sidebar) so that no testid-bearing control is ever duplicated in the
 * DOM, while `md:` classes handle in-tree responsive styling.
 */
const QUERY = "(min-width: 768px)";

function subscribe(onChange: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const mql = window.matchMedia(QUERY);
  mql.addEventListener("change", onChange);
  return () => mql.removeEventListener("change", onChange);
}

function getSnapshot(): boolean {
  // jsdom (unit tests) has no matchMedia — fall back to the desktop default.
  if (typeof window === "undefined" || !window.matchMedia) return true;
  return window.matchMedia(QUERY).matches;
}

function getServerSnapshot(): boolean {
  return true;
}

export function useIsDesktop(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
