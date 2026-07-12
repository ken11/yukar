/**
 * thread-utils — Pure-function utilities for thread display logic.
 *
 * Extracted from components so they can be tested directly with Vitest.
 * No dependency on React / Next.js.
 */

/**
 * Pure function that returns whether the currently viewed thread is the "active trial".
 *
 * "Active trial" = the thread pointed to by activeTrialId is currently being viewed and is not archived.
 * Even with role=manager, old trials that do not match activeTrialId are read-only (false).
 *
 * The only path to showing the composer is through
 * `activityState.activeTrialId` in thread-page-client.tsx (`activeThreadId` → `SET_ACTIVE_TRIAL_ID` dispatch).
 * activeTrialId is sourced from epic.active_thread_id only (P4) — never from
 * RunState.thread_id, which is the run's own thread (currentRun).
 * The archived exclusion in `applyTreeInit` is a correction to tree display nodes and is unrelated to the composer.
 *
 * @param threadId     The id of the currently viewed thread
 * @param activeTrialId  activityState.activeTrialId (the caller already falls back to "manager" when null)
 * @param isArchived   thread.status === "archived"
 */
export function computeIsActiveTrial(
  threadId: string,
  activeTrialId: string,
  isArchived: boolean,
): boolean {
  return !isArchived && threadId === activeTrialId;
}
