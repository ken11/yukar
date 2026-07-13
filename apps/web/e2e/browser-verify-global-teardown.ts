/**
 * Global teardown for the browser verification E2E scenario.
 * Removes the temp dir dedicated to the browser-verify scenario.
 */
import fs from "node:fs";
import { BROWSER_VERIFY_SEED } from "./browser-verify-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(BROWSER_VERIFY_SEED.base)) {
    // The API server is still up while globalTeardown runs (Playwright stops
    // webServers afterwards) and may write into the workspace concurrently —
    // retry so a racing write cannot fail the run with ENOTEMPTY.
    fs.rmSync(BROWSER_VERIFY_SEED.base, {
      recursive: true,
      force: true,
      maxRetries: 10,
      retryDelay: 100,
    });
  }
  console.log("[browser-verify globalTeardown] cleaned up:", BROWSER_VERIFY_SEED.base);
}
