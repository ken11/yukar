import { describe, expect, it } from "vitest";
import { joinCommandLine, splitCommandLine } from "./command-line";

describe("splitCommandLine", () => {
  it("splits on whitespace", () => {
    expect(splitCommandLine("pnpm dev --port 3000")).toEqual(["pnpm", "dev", "--port", "3000"]);
  });

  it("keeps double-quoted args with spaces as one token, quotes stripped", () => {
    expect(splitCommandLine('node -e "console.log(1 + 1)"')).toEqual([
      "node",
      "-e",
      "console.log(1 + 1)",
    ]);
  });

  it("keeps single-quoted args with spaces as one token, quotes stripped", () => {
    expect(splitCommandLine("sh -c 'sleep 1 && echo ok'")).toEqual([
      "sh",
      "-c",
      "sleep 1 && echo ok",
    ]);
  });

  it("returns [] for an empty string", () => {
    expect(splitCommandLine("")).toEqual([]);
  });

  it("returns [] for whitespace only", () => {
    expect(splitCommandLine("   \t ")).toEqual([]);
  });

  it("collapses multiple spaces between tokens", () => {
    expect(splitCommandLine("pnpm   dev    --host")).toEqual(["pnpm", "dev", "--host"]);
  });

  it("passes {port} placeholders through untouched", () => {
    expect(splitCommandLine("pnpm dev --port {port} --api {port:api}")).toEqual([
      "pnpm",
      "dev",
      "--port",
      "{port}",
      "--api",
      "{port:api}",
    ]);
  });

  it("treats the rest of the line as one token on an unterminated quote", () => {
    expect(splitCommandLine('echo "hello wor')).toEqual(["echo", "hello wor"]);
  });

  it("joins quoted segments adjacent to plain text into one token", () => {
    expect(splitCommandLine('--name="my app"')).toEqual(["--name=my app"]);
  });

  it("preserves an empty quoted token", () => {
    expect(splitCommandLine('cmd ""')).toEqual(["cmd", ""]);
  });
});

describe("joinCommandLine", () => {
  it("joins plain tokens with single spaces", () => {
    expect(joinCommandLine(["pnpm", "dev", "--port", "3000"])).toBe("pnpm dev --port 3000");
  });

  it("quotes tokens containing whitespace", () => {
    expect(joinCommandLine(["node", "-e", "console.log(1 + 1)"])).toBe(
      'node -e "console.log(1 + 1)"',
    );
  });

  it("quotes empty tokens", () => {
    expect(joinCommandLine(["cmd", ""])).toBe('cmd ""');
  });

  it("leaves {port} placeholders unquoted", () => {
    expect(joinCommandLine(["pnpm", "dev", "--port", "{port}"])).toBe("pnpm dev --port {port}");
  });

  it("quotes and escapes tokens containing quote chars but no whitespace", () => {
    expect(joinCommandLine(['--define:FOO="bar"'])).toBe('"--define:FOO=\\"bar\\""');
    expect(joinCommandLine(["require('./x')"])).toBe(`"require('./x')"`);
  });
});

describe("roundtrip", () => {
  it("split(join(tokens)) returns the original tokens", () => {
    const cases: string[][] = [
      ["pnpm", "dev", "--port", "{port}"],
      ["node", "-e", "console.log(1 + 1)"],
      ["sh", "-c", "sleep 1 && echo ok"],
      ["uvicorn", "app:main", "--host", "127.0.0.1"],
      ["cmd", ""],
    ];
    for (const tokens of cases) {
      expect(splitCommandLine(joinCommandLine(tokens))).toEqual(tokens);
    }
  });

  it("round-trips single tokens that contain quotes, whitespace, or are empty", () => {
    const tokens = ['--define:FOO="bar"', "require('./x')", `a"b`, "a b", "{port}", ""];
    for (const tok of tokens) {
      expect(splitCommandLine(joinCommandLine([tok]))).toEqual([tok]);
    }
  });

  it("round-trips tokens containing backslashes (and backslash-then-quote)", () => {
    // Regression: joinCommandLine must escape backslashes, and splitCommandLine
    // must honor \\ — otherwise `a\"b` round-trips to `a\\b` (quote lost, one
    // backslash doubled) and a no-op re-save silently corrupts the token.
    const tokens = [
      `a\\"b`, // a, backslash, quote, b
      "C:\\path\\to\\bin",
      "back\\slash",
      "trailing\\",
      `mix "q" and \\ slash`,
    ];
    for (const tok of tokens) {
      expect(splitCommandLine(joinCommandLine([tok]))).toEqual([tok]);
    }
  });
});
