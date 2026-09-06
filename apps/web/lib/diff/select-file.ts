// Picks the parsed diff block that belongs to the file selected in the tree.
// Pure (no React) so the matching rules can be unit-tested on their own.

import type { DiffFile } from "./parse-unified";

/**
 * True when *tail* is `full` itself or a trailing part of it that starts on a
 * path-segment boundary.  Plain `String.endsWith` is NOT enough: it makes
 * `main.tf` a "match" for `modules/vpc/main.tf`, which is exactly how a
 * repository-root file used to shadow every same-named file below it.
 */
function isPathSuffix(full: string, tail: string): boolean {
  return full === tail || full.endsWith(`/${tail}`);
}

/**
 * Find the diff block for *selectedFile*.
 *
 * The changed-file list and the unified diff come from the same `git diff`
 * invocation, so an exact path match is the normal case and the only one that
 * can never pick the wrong file.  The suffix fallback exists solely for diffs
 * whose paths carry an extra prefix (e.g. git's `diff.mnemonicPrefix`), and it
 * is deliberately conservative: segment boundaries only, and a candidate is
 * accepted only when it is the *sole* one — an ambiguous match returns
 * undefined so the viewer shows nothing rather than another file's content.
 *
 * Returns undefined when nothing matches; callers must not fall back to the
 * first block, which would put a different file's diff under the selected
 * file's name.
 */
export function selectDiffFile(
  files: readonly DiffFile[],
  selectedFile: string,
): DiffFile | undefined {
  if (files.length === 0) return undefined;
  // No selection yet (first paint before the list arrives): show the first block.
  if (selectedFile === "") return files[0];

  const exact = files.find((f) => f.newPath === selectedFile || f.oldPath === selectedFile);
  if (exact) return exact;

  const candidates = files.filter(
    (f) =>
      isPathSuffix(f.newPath, selectedFile) ||
      isPathSuffix(selectedFile, f.newPath) ||
      isPathSuffix(f.oldPath, selectedFile) ||
      isPathSuffix(selectedFile, f.oldPath),
  );
  return candidates.length === 1 ? candidates[0] : undefined;
}
