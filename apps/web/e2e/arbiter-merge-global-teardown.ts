/**
 * Global teardown for Arbiter merge E2E scenario.
 * Deletes the temp dir dedicated to the arbiter-merge scenario.
 */
import fs from "node:fs";
import { ARBITER_MERGE_SEED } from "./arbiter-merge-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(ARBITER_MERGE_SEED.base)) {
    fs.rmSync(ARBITER_MERGE_SEED.base, { recursive: true, force: true });
  }
  console.log("[arbiter-merge globalTeardown] cleaned up:", ARBITER_MERGE_SEED.base);
}
