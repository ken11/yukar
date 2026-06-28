/**
 * Shared text ↔ array helpers.
 *
 * linesToArray: split on newlines, trim each line, drop empty lines.
 * arrayToLines: join with newlines (undefined/null → treated as empty array).
 */

export function linesToArray(text: string): string[] {
  return text
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}

export function arrayToLines(items: readonly string[] | undefined): string {
  return (items ?? []).join("\n");
}
