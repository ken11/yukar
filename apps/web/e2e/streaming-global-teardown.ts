/**
 * Global teardown for MessageTurn streaming E2E scenario.
 * Removes the temp dir dedicated to the streaming scenario.
 */
import fs from "node:fs";
import { STREAMING_SEED } from "./streaming-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(STREAMING_SEED.base)) {
    fs.rmSync(STREAMING_SEED.base, { recursive: true, force: true });
  }
  console.log("[streaming globalTeardown] cleaned up:", STREAMING_SEED.base);
}
