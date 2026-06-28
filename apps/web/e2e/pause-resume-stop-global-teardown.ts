/**
 * Global teardown for pause/resume/stop E2E scenario.
 * Removes the temp dir dedicated to the pause/resume/stop scenario.
 */
import fs from "node:fs";
import { PAUSE_RESUME_SEED } from "./pause-resume-stop-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(PAUSE_RESUME_SEED.base)) {
    fs.rmSync(PAUSE_RESUME_SEED.base, { recursive: true, force: true });
  }
  console.log("[pause-resume globalTeardown] cleaned up:", PAUSE_RESUME_SEED.base);
}
