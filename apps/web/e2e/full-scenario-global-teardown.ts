/**
 * Global teardown for the full-scenario E2E. Removes the scenario's temp dir.
 */
import fs from "node:fs";
import { FULL_SCENARIO_SEED } from "./full-scenario-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(FULL_SCENARIO_SEED.base)) {
    fs.rmSync(FULL_SCENARIO_SEED.base, { recursive: true, force: true });
  }
  console.log("[full-scenario globalTeardown] cleaned up:", FULL_SCENARIO_SEED.base);
}
