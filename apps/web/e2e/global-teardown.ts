/**
 * Playwright globalTeardown.
 * Removes the deterministic temp directory created by globalSetup.
 */
import fs from "node:fs";
import { SEED } from "./seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(SEED.base)) {
    fs.rmSync(SEED.base, { recursive: true, force: true });
  }
  console.log("[e2e globalTeardown] cleaned up:", SEED.base);
}
