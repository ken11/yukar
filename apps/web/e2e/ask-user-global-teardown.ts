/**
 * Global teardown for ask_user E2E scenario.
 * Removes the temp dir dedicated to the ask_user scenario.
 */
import fs from "node:fs";
import { ASK_USER_SEED } from "./ask-user-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(ASK_USER_SEED.base)) {
    fs.rmSync(ASK_USER_SEED.base, { recursive: true, force: true });
  }
  console.log("[ask-user globalTeardown] cleaned up:", ASK_USER_SEED.base);
}
