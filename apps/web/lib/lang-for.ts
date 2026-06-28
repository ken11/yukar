/** Returns the CodeMirror language mode for a given filename. */
export function langFor(filename: string): "markdown" | "yaml" {
  if (filename.endsWith(".yaml") || filename.endsWith(".yml")) return "yaml";
  return "markdown";
}
