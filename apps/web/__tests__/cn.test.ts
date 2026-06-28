import { describe, expect, it } from "vitest";
import { cn } from "../lib/cn";

describe("cn", () => {
  it("joins truthy strings", () => {
    expect(cn("a", "b", "c")).toBe("a b c");
  });

  it("filters falsy values", () => {
    expect(cn("a", false, null, undefined, "b")).toBe("a b");
  });

  it("returns empty string for all falsy", () => {
    expect(cn(false, null, undefined)).toBe("");
  });

  it("handles a single class", () => {
    expect(cn("hello")).toBe("hello");
  });
});
