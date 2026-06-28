/**
 * Global teardown for docs edit/save/reload persistence E2E scenario.
 */
import fs from "node:fs";
import { DOCS_SEED } from "./docs-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(DOCS_SEED.base)) {
    fs.rmSync(DOCS_SEED.base, { recursive: true, force: true });
  }
  console.log("[docs globalTeardown] cleaned up:", DOCS_SEED.base);
}
