/**
 * diff test helpers (test-only)
 *
 * #46: moved from parse-unified.ts:93-124. For fixture generation — not included in the production bundle.
 */

/** Helper to assemble unified diff text from DiffRowFixture (for fixtures) */
export interface DiffRowInput {
  type: "ctx" | "add" | "del";
  oldNo?: number;
  newNo?: number;
  text: string;
}

export function buildUnifiedFromRows(
  oldPath: string,
  newPath: string,
  rows: DiffRowInput[],
): string {
  const ctxLines = rows.filter((r) => r.type === "ctx");
  const firstOld = ctxLines[0]?.oldNo ?? 1;
  const firstNew = ctxLines[0]?.newNo ?? 1;
  const addCount = rows.filter((r) => r.type === "add").length;
  const delCount = rows.filter((r) => r.type === "del").length;
  const totalOld = ctxLines.length + delCount;
  const totalNew = ctxLines.length + addCount;

  const hunkHeader = `@@ -${firstOld},${totalOld} +${firstNew},${totalNew} @@`;
  const body = rows
    .map((r) => {
      if (r.type === "add") return `+${r.text}`;
      if (r.type === "del") return `-${r.text}`;
      return ` ${r.text}`;
    })
    .join("\n");

  return `--- a/${oldPath}\n+++ b/${newPath}\n${hunkHeader}\n${body}`;
}
