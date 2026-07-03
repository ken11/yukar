/**
 * Global teardown for the repos add/delete (CRUD) E2E scenario.
 */
import fs from "node:fs";
import { REPOS_CRUD_SEED } from "./repos-crud-seed";

export default async function globalTeardown(): Promise<void> {
  if (fs.existsSync(REPOS_CRUD_SEED.base)) {
    fs.rmSync(REPOS_CRUD_SEED.base, { recursive: true, force: true });
  }
  console.log("[repos-crud globalTeardown] cleaned up:", REPOS_CRUD_SEED.base);
}
