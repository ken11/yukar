/**
 * Global teardown for the evaluator-only dispatch E2E scenario.
 * Removes the temp dir dedicated to the evaluator-only scenario.
 */
import fs from "node:fs";
import { EVALUATOR_ONLY_SEED } from "./evaluator-only-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(EVALUATOR_ONLY_SEED.base)) {
    fs.rmSync(EVALUATOR_ONLY_SEED.base, { recursive: true, force: true });
  }
  console.log("[evaluator-only globalTeardown] cleaned up:", EVALUATOR_ONLY_SEED.base);
}
