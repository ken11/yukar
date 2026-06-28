/**
 * Global teardown for conflict-resolve E2E scenario.
 * Removes the temp dir dedicated to the conflict-resolve scenario.
 */
import fs from "node:fs";
import { CONFLICT_RESOLVE_SEED } from "./conflict-resolve-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(CONFLICT_RESOLVE_SEED.base)) {
    fs.rmSync(CONFLICT_RESOLVE_SEED.base, { recursive: true, force: true });
  }
  console.log("[conflict-resolve globalTeardown] cleaned up:", CONFLICT_RESOLVE_SEED.base);
}
