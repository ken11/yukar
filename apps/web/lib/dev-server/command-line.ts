/**
 * Command line ↔ exec-token helpers for the dev-server launch config editor.
 *
 * The backend stores `DevService.command` as exec tokens (never a shell line),
 * so the UI lets users type a familiar one-line command and converts both ways.
 * This is a plain whitespace tokenizer with single/double quote support — no
 * shell semantics (no expansion, no escapes), so `{port}` placeholders pass
 * through untouched.
 */

/**
 * Split a command line into exec tokens.
 *
 * Whitespace separates tokens; single or double quotes group text (quotes are
 * stripped). Inside a double-quoted span, `\"` and `\\` are honored as a literal
 * `"` and `\` so tokens that themselves contain quotes or backslashes survive a
 * joinCommandLine round-trip. An unterminated quote consumes the rest of the
 * line as one token.
 */
export function splitCommandLine(line: string): string[] {
  const tokens: string[] = [];
  let current = "";
  let inToken = false;
  let quote: '"' | "'" | null = null;
  let escaped = false;

  for (const ch of line) {
    if (quote !== null) {
      if (escaped) {
        // \" → " and \\ → \ (the two joinCommandLine emits); any other sequence
        // keeps its backslash so unrelated input is preserved.
        current += ch === '"' ? '"' : ch === "\\" ? "\\" : `\\${ch}`;
        escaped = false;
        continue;
      }
      if (quote === '"' && ch === "\\") {
        escaped = true;
        continue;
      }
      if (ch === quote) {
        quote = null;
      } else {
        current += ch;
      }
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      inToken = true;
      continue;
    }
    if (ch === " " || ch === "\t") {
      if (inToken) {
        tokens.push(current);
        current = "";
        inToken = false;
      }
      continue;
    }
    current += ch;
    inToken = true;
  }
  if (escaped) current += "\\";
  if (inToken) tokens.push(current);
  return tokens;
}

/**
 * Join exec tokens back into an editable command line.
 *
 * Any token that splitCommandLine would not reproduce verbatim — one containing
 * whitespace, a quote, a backslash, or an empty token — is wrapped in double
 * quotes with embedded backslashes AND double-quotes backslash-escaped (in that
 * order), so a later splitCommandLine round-trips to the same tokens.
 */
export function joinCommandLine(tokens: string[]): string {
  return tokens
    .map((tok) =>
      tok === "" || /[\s"'\\]/.test(tok)
        ? `"${tok.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`
        : tok,
    )
    .join(" ");
}
