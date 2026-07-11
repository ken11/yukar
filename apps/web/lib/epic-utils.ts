/**
 * Epic common utilities.
 *
 * #5: Consolidated isTerminalStatus() into a single location.
 * Promoted the isolated definition that previously existed only in epic-switcher.tsx to lib,
 * replacing inline checks in epics-board-client.tsx / command-palette.tsx / projects/[p]/page.tsx.
 */

import type { Epic, EpicWithRunSummary, ThreadEntry } from "./api/endpoints";

/**
 * "Your turn" (P4): the epic's conversation run parked in "waiting" and is
 * waiting for the user's reply. run_id must be non-empty — a synthesised
 * never-run state does not count. Pure current state (run_summary is derived
 * from state.yaml at list time); there is no unread persistence.
 *
 * Completed epics never show the badge: they are locked history, and after
 * P3 every conversation run settles in waiting, so without the open-status
 * condition every epic that ever ran would keep an inbox badge forever
 * after being completed.
 */
export function hasYourTurn(e: Pick<EpicWithRunSummary, "run_summary" | "status">): boolean {
  return e.status === "open" && e.run_summary?.status === "waiting" && !!e.run_summary.run_id;
}

/**
 * Returns whether an Epic is completed (the user-owned 1-bit lifecycle:
 * open ⇄ completed). Completed epics are excluded from resume/merge targets
 * and are pushed to the end of lists. "Merged" is a fact attribute
 * (epic.merged_at), not a terminal state — merged epics may stay open.
 */
export function isTerminalStatus(status: string | undefined): boolean {
  return status === "completed";
}

/**
 * Resolves the thread_id of the active manager trial.
 *
 * Priority:
 * 1. epic.active_thread_id (authoritative value guaranteed by the backend)
 * 2. The first thread from threads with role=manager && status!=="archived"
 *    (uses archived exclusion so it is picked up correctly even after completion/resolved)
 * 3. Fallback: "manager" (backward compat, e.g. immediately after a new Epic)
 */
export function resolveActiveManagerThreadId(
  epic: Pick<Epic, "active_thread_id"> | null | undefined,
  threads: ThreadEntry[],
): string {
  if (epic?.active_thread_id) return epic.active_thread_id;
  const activeManager = threads.find((t) => t.role === "manager" && t.status !== "archived");
  if (activeManager) return activeManager.id;
  return "manager";
}
