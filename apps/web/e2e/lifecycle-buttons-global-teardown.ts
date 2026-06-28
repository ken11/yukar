/**
 * Global teardown for lifecycle-buttons E2E scenario.
 */
import fs from "node:fs";
import { LIFECYCLE_SEED } from "./lifecycle-buttons-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(LIFECYCLE_SEED.base)) {
    fs.rmSync(LIFECYCLE_SEED.base, { recursive: true, force: true });
  }
  console.log("[lifecycle globalTeardown] cleaned up:", LIFECYCLE_SEED.base);
}
