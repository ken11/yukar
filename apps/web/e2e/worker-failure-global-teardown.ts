/**
 * Global teardown for Worker failure E2E scenario.
 * Removes the temp dir dedicated to the Worker failure scenario.
 */
import fs from "node:fs";
import { WORKER_FAILURE_SEED } from "./worker-failure-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(WORKER_FAILURE_SEED.base)) {
    // The failed-worker run leaves git worktrees under base, and the API server
    // may still hold handles when teardown runs — retry on the resulting
    // transient ENOTEMPTY/EBUSY. Cleanup of a temp dir must never fail the run,
    // so tolerate a residual error (the OS reaps /tmp eventually).
    try {
      fs.rmSync(WORKER_FAILURE_SEED.base, {
        recursive: true,
        force: true,
        maxRetries: 5,
        retryDelay: 200,
      });
    } catch (err) {
      console.warn("[worker-failure globalTeardown] cleanup incomplete (ignored):", err);
      return;
    }
  }
  console.log("[worker-failure globalTeardown] cleaned up:", WORKER_FAILURE_SEED.base);
}
