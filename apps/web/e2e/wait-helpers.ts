/**
 * Standard wait conditions for the redesigned lifecycle (conversations never end).
 *
 * - Turn end:  GET run/state reaches "waiting" — the single resting state
 *   ("your turn"). Pair with an assertion on the latest assistant text when
 *   the run was already parked in waiting before the awaited turn (a reply
 *   wake), because "waiting" alone cannot distinguish before from after.
 * - Work done: run/state is "waiting" AND every task is done (non-empty).
 *
 * An epic that has never run synthesises run_id="" / status="waiting"
 * (GET /run/state default), so both helpers guard on a real run_id to avoid
 * matching the pre-start window right after clicking Start Run.
 */
import { expect, type Page } from "@playwright/test";

export interface RunStateBody {
  run_id: string;
  status: string;
  thread_id?: string | null;
}

export async function getRunState(
  page: Page,
  projectId: string,
  epicId: string,
): Promise<RunStateBody> {
  const res = await page.request.get(`/api/projects/${projectId}/epics/${epicId}/run/state`);
  return (await res.json()) as RunStateBody;
}

/** Turn end: poll until run/state is "waiting" with a real run_id. */
export async function waitForRunWaiting(
  page: Page,
  projectId: string,
  epicId: string,
  opts: { timeout?: number } = {},
): Promise<void> {
  await expect
    .poll(
      async () => {
        const s = await getRunState(page, projectId, epicId);
        return s.run_id ? s.status : "not-started";
      },
      { timeout: opts.timeout ?? 90_000, intervals: [500, 1000, 1000] },
    )
    .toBe("waiting");
}

/**
 * Work complete: the run is parked in "waiting" AND all tasks are done.
 * The task check excludes the pre-start synthetic-waiting window (no tasks
 * yet) and the mid-dispatch window (tasks not yet done).
 */
export async function waitForWorkDone(
  page: Page,
  projectId: string,
  epicId: string,
  opts: { timeout?: number } = {},
): Promise<void> {
  await expect
    .poll(
      async () => {
        const s = await getRunState(page, projectId, epicId);
        if (!s.run_id || s.status !== "waiting") return `run:${s.status}`;
        const tRes = await page.request.get(`/api/projects/${projectId}/epics/${epicId}/tasks`);
        const body = (await tRes.json()) as { tasks?: Array<{ id: string; status: string }> };
        const tasks = body.tasks ?? [];
        if (tasks.length === 0) return "no-tasks";
        return tasks.every((t) => t.status === "done") ? "waiting:all-done" : "tasks-pending";
      },
      { timeout: opts.timeout ?? 90_000, intervals: [500, 1000, 1000] },
    )
    .toBe("waiting:all-done");
}
