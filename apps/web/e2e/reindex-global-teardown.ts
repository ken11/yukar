/**
 * Global teardown for reindex E2E scenario.
 */
import fs from "node:fs";
import { REINDEX_SEED } from "./reindex-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(REINDEX_SEED.base)) {
    fs.rmSync(REINDEX_SEED.base, { recursive: true, force: true });
  }
  console.log("[reindex globalTeardown] cleaned up:", REINDEX_SEED.base);
}
