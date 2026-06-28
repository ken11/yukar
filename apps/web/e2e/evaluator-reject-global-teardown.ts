/**
 * Global teardown for Evaluator reject → retry → accept E2E scenario.
 * Removes the temp dir dedicated to the evaluator-reject scenario.
 */
import fs from "node:fs";
import { EVALUATOR_REJECT_SEED } from "./evaluator-reject-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(EVALUATOR_REJECT_SEED.base)) {
    fs.rmSync(EVALUATOR_REJECT_SEED.base, { recursive: true, force: true });
  }
  console.log("[evaluator-reject globalTeardown] cleaned up:", EVALUATOR_REJECT_SEED.base);
}
