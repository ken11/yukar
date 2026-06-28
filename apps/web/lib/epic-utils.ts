/**
 * Epic common utilities.
 *
 * #5: Consolidated isTerminalStatus() into a single location.
 * Promoted the isolated definition that previously existed only in epic-switcher.tsx to lib,
 * replacing inline checks in epics-board-client.tsx / command-palette.tsx / projects/[p]/page.tsx.
 */

import type { Epic, ThreadEntry } from "./api/endpoints";

/**
 * Returns whether an Epic's status is a "terminal state".
 * closed / merged are excluded from resume/merge targets and are pushed to the end of the list.
 */
export function isTerminalStatus(status: string | undefined): boolean {
  return status === "closed" || status === "merged";
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
