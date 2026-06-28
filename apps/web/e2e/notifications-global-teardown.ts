/**
 * Global teardown for notifications E2E scenario.
 */
import fs from "node:fs";
import { NOTIF_SEED } from "./notifications-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(NOTIF_SEED.base)) {
    fs.rmSync(NOTIF_SEED.base, { recursive: true, force: true });
  }
  console.log("[notifications globalTeardown] cleaned up:", NOTIF_SEED.base);
}
