/**
 * Global teardown for hitl-reply E2E scenario.
 * Removes the temp dir dedicated to the hitl-reply scenario.
 */
import fs from "node:fs";
import { HITL_REPLY_SEED } from "./hitl-reply-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(HITL_REPLY_SEED.base)) {
    fs.rmSync(HITL_REPLY_SEED.base, { recursive: true, force: true });
  }
  console.log("[hitl-reply globalTeardown] cleaned up:", HITL_REPLY_SEED.base);
}
