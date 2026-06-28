/**
 * Global teardown for budget exceeded E2E scenario.
 * Removes the temp dir dedicated to the budget-exceeded scenario.
 */
import fs from "node:fs";
import { BUDGET_SEED } from "./budget-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(BUDGET_SEED.base)) {
    fs.rmSync(BUDGET_SEED.base, { recursive: true, force: true });
  }
  console.log("[budget globalTeardown] cleaned up:", BUDGET_SEED.base);
}
