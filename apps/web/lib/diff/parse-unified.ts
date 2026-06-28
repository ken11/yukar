/**
 * Unified diff parser (custom implementation)
 * For the spec §7.7 Git Diff screen. Does not use @codemirror/merge.
 */

export type DiffLineType = "ctx" | "add" | "del" | "hunk" | "header";

export interface DiffLine {
  type: DiffLineType;
  oldNo?: number;
  newNo?: number;
  text: string;
}

export interface DiffFile {
  oldPath: string;
  newPath: string;
  lines: DiffLine[];
}

/**
 * Returns true when a `--- ` line is a file header rather than hunk content.
 * Heuristic: the immediately following line starts with `+++ ` (the `+++` header always
 * follows the `---` header in a well-formed unified diff).
 */
function isFileHeader(lines: string[], index: number): boolean {
  return lines[index + 1]?.startsWith("+++ ") === true;
}

/**
 * Parse the file paths from a `diff --git a/X b/Y` line.
 * Returns [oldPath, newPath] with the `a/` / `b/` prefixes stripped, or null if the
 * line does not match the expected format.
 *
 * Limitation: paths that themselves contain ` b/` are ambiguous without length hints;
 * we use the last occurrence of ` b/` as the separator, which covers the overwhelming
 * majority of real-world diffs where oldPath and newPath are identical.
 */
function parseDiffGitLine(line: string): [string, string] | null {
  // Expected: "diff --git a/<oldPath> b/<newPath>"
  const prefix = "diff --git ";
  if (!line.startsWith(prefix)) return null;
  const rest = line.slice(prefix.length); // "a/<old> b/<new>"
  if (!rest.startsWith("a/")) return null;
  // Find " b/" separator — use last occurrence to handle spaces in paths more robustly
  // when oldPath and newPath are identical (the common case).
  const sepIdx = rest.lastIndexOf(" b/");
  if (sepIdx === -1) return null;
  const oldPath = rest.slice(2, sepIdx); // strip leading "a/"
  const newPath = rest.slice(sepIdx + 3); // strip leading " b/"
  return [oldPath, newPath];
}

export function parseUnifiedDiff(raw: string): DiffFile[] {
  const files: DiffFile[] = [];
  let current: DiffFile | null = null;
  let oldLine = 0;
  let newLine = 0;
  // Track whether we are inside a hunk (after the first @@ line)
  let inHunk = false;
  // Track whether the current block was opened by a `diff --git` line.
  // When true, a following `--- ` line updates the placeholder paths in-place
  // if the path matches (or is /dev/null for new/deleted files).
  // When false (plain unified diff), `--- ` opens a brand-new block.
  let hasGitHeader = false;

  const lines = raw.split("\n");

  for (let idx = 0; idx < lines.length; idx++) {
    const line = lines[idx];

    // "\ No newline at end of file" — informational marker, skip silently
    if (line === "\\ No newline at end of file") {
      continue;
    }

    // `diff --git a/X b/Y` — start a placeholder DiffFile for this block.
    // If `---`/`+++` headers follow, they will overwrite oldPath/newPath with the
    // authoritative values (e.g. for renames with content changes, or new/deleted files).
    // If no `---`/`+++` pair follows (binary / rename-only / mode-only), the
    // placeholder is pushed as-is so the block is not silently dropped.
    if (line.startsWith("diff --git ")) {
      if (current) {
        files.push(current);
      }
      const parsed = parseDiffGitLine(line);
      if (parsed) {
        const [oldPath, newPath] = parsed;
        current = { oldPath, newPath, lines: [] };
      } else {
        // Malformed diff --git line: create an empty placeholder to avoid losing the block.
        current = { oldPath: "", newPath: "", lines: [] };
      }
      inHunk = false;
      hasGitHeader = true;
      // File header `--- ` detection: only treat as header when the next line is `+++ `.
      // This correctly disambiguates a deletion whose content starts with `-- ` (which would
      // appear as `--- ` in the diff stream) from a genuine file header.
    } else if (line.startsWith("--- ") && isFileHeader(lines, idx)) {
      const oldPath = line.slice(4).replace(/^a\//, "");
      // If the current block was opened by a `diff --git` line and we are not yet in a
      // hunk, the `---` line provides authoritative path information for that block.
      // Two cases match the placeholder:
      //   1. Path matches: `oldPath` equals the placeholder's oldPath or newPath
      //      (the common case and renames).
      //   2. New file: `oldPath === "/dev/null"` — the placeholder path from `diff --git`
      //      is the canonical name; we keep it and only update via the `+++` line.
      // Any other `---` (different file path, not /dev/null) starts a fresh plain-diff block.
      const placeholderMatches =
        current !== null &&
        hasGitHeader &&
        !inHunk &&
        (current.oldPath === oldPath || current.newPath === oldPath || oldPath === "/dev/null");
      if (placeholderMatches && current !== null) {
        // Update oldPath from the authoritative `---` header — but only if the `---`
        // carries a real path (i.e. not /dev/null). For new files (`--- /dev/null`),
        // the placeholder's oldPath from `diff --git` is already correct.
        if (oldPath !== "/dev/null") {
          current.oldPath = oldPath;
        }
        current.lines.push({ type: "header", text: line });
      } else {
        // No matching placeholder: push the current block (if any) and open a fresh one.
        if (current) {
          files.push(current);
        }
        current = { oldPath, newPath: "", lines: [] };
        current.lines.push({ type: "header", text: line });
        hasGitHeader = false;
      }
      inHunk = false;
    } else if (line.startsWith("+++ ") && current && !inHunk) {
      // `+++` header — only valid before the first hunk of a file block.
      // For deleted files: `+++ /dev/null` — keep the placeholder's newPath from
      // `diff --git` which already holds the canonical name.
      const parsedNewPath = line.slice(4).replace(/^b\//, "");
      if (parsedNewPath !== "/dev/null") {
        current.newPath = parsedNewPath;
      }
      current.lines.push({ type: "header", text: line });
    } else if (line.startsWith("@@ ") && current) {
      // Hunk header: @@ -45,12 +45,16 @@
      const match = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
      if (match) {
        oldLine = Number.parseInt(match[1], 10);
        newLine = Number.parseInt(match[2], 10);
      }
      current.lines.push({ type: "hunk", text: line });
      inHunk = true;
    } else if (line.startsWith("+") && current) {
      current.lines.push({ type: "add", newNo: newLine, text: line.slice(1) });
      newLine++;
    } else if (line.startsWith("-") && current) {
      current.lines.push({ type: "del", oldNo: oldLine, text: line.slice(1) });
      oldLine++;
    } else if (line.startsWith(" ") && current) {
      current.lines.push({ type: "ctx", oldNo: oldLine, newNo: newLine, text: line.slice(1) });
      oldLine++;
      newLine++;
    }
    // Any other line (e.g. "index …", "similarity index …", "Binary files …",
    // "old mode …", "new mode …", "rename from/to …") is ignored after the placeholder
    // has already been created by the `diff --git` line above.
  }

  if (current) {
    files.push(current);
  }

  return files;
}
