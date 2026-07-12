/**
 * Global teardown for the worker-only dispatch E2E scenario (P6).
 * Removes the temp dir dedicated to the worker-only scenario.
 */
import fs from "node:fs";
import { WORKER_ONLY_SEED } from "./worker-only-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(WORKER_ONLY_SEED.base)) {
    fs.rmSync(WORKER_ONLY_SEED.base, { recursive: true, force: true });
  }
  console.log("[worker-only globalTeardown] cleaned up:", WORKER_ONLY_SEED.base);
}
