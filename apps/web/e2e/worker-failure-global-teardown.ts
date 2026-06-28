/**
 * Global teardown for Worker failure E2E scenario.
 * Removes the temp dir dedicated to the Worker failure scenario.
 */
import fs from "node:fs";
import { WORKER_FAILURE_SEED } from "./worker-failure-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(WORKER_FAILURE_SEED.base)) {
    fs.rmSync(WORKER_FAILURE_SEED.base, { recursive: true, force: true });
  }
  console.log("[worker-failure globalTeardown] cleaned up:", WORKER_FAILURE_SEED.base);
}
