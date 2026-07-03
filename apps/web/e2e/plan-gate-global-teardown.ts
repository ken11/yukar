/**
 * Global teardown for the plan-approval-gate E2E scenario (bug ⑤).
 * Removes the scenario's temp dir.
 */
import fs from "node:fs";
import { PLAN_GATE_SEED } from "./plan-gate-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(PLAN_GATE_SEED.base)) {
    fs.rmSync(PLAN_GATE_SEED.base, { recursive: true, force: true });
  }
  console.log("[plan-gate globalTeardown] cleaned up:", PLAN_GATE_SEED.base);
}
