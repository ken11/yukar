/**
 * thread-utils — Pure-function utilities for thread display logic.
 *
 * Extracted from components so they can be tested directly with Vitest.
 * No dependency on React / Next.js.
 */

/**
 * Pure function that returns whether the currently viewed thread is the "active trial".
 *
 * "Active trial" = the thread pointed to by managerThreadId is currently being viewed and is not archived.
 * Even with role=manager, old trials that do not match managerThreadId are read-only (false).
 *
 * The only path to showing the composer is through
 * `activityState.managerThreadId` in thread-page-client.tsx (`activeThreadId` → `SET_MANAGER_THREAD_ID` dispatch).
 * The archived exclusion in `applyTreeInit` is a correction to tree display nodes and is unrelated to the composer.
 *
 * @param threadId     The id of the currently viewed thread
 * @param managerThreadId  activityState.managerThreadId (the caller already falls back to "manager" when null)
 * @param isArchived   thread.status === "archived"
 */
export function computeIsActiveTrial(
  threadId: string,
  managerThreadId: string,
  isArchived: boolean,
): boolean {
  return !isArchived && threadId === managerThreadId;
}
