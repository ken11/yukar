import type { ThreadMessageLike } from "@assistant-ui/react";
import { describe, expect, it } from "vitest";
import { buildStreamItems } from "@/lib/conversation/stream-items";

function text(role: "user" | "assistant", body: string, id: string): ThreadMessageLike {
  return { id, role, content: [{ type: "text", text: body }] };
}
function tool(name: string, id: string, result?: string): ThreadMessageLike {
  return {
    id,
    role: "assistant",
    content: [{ type: "tool-call", toolCallId: `tc-${id}`, toolName: name, args: {}, result }],
  };
}

describe("buildStreamItems", () => {
  it("collapses consecutive same-named tool-only messages into one run", () => {
    const items = buildStreamItems([
      text("user", "kickoff", "m1"),
      tool("task_update", "m2", "ok"),
      tool("task_update", "m3", "ok"),
      tool("task_update", "m4", "ok"),
      text("assistant", "計画です。よろしいですか?", "m5"),
    ]);
    expect(items.map((i) => i.kind)).toEqual(["message", "tool-run", "message"]);
    const run = items[1];
    if (run.kind !== "tool-run") throw new Error("expected tool-run");
    expect(run.toolName).toBe("task_update");
    expect(run.calls).toHaveLength(3);
  });

  it("keeps differently-named tool messages as separate runs (one bubble per action)", () => {
    const items = buildStreamItems([
      text("user", "kickoff", "m1"),
      tool("task_update", "m2", "ok"),
      tool("dispatch", "m3", "ok"),
      text("assistant", "報告です。", "m4"),
    ]);
    // task_update と dispatch は別バブル — smoke E2E の「発話ごとに別バブル」を保つ
    expect(items.map((i) => (i.kind === "tool-run" ? i.toolName : i.kind))).toEqual([
      "message",
      "task_update",
      "dispatch",
      "message",
    ]);
  });

  it("marks grouped once per role run, across tool-runs", () => {
    const items = buildStreamItems([
      text("user", "kickoff", "m1"),
      tool("task_update", "m2", "ok"),
      text("assistant", "計画", "m3"),
      text("user", "返信", "m4"),
    ]);
    expect(items[0].grouped).toBe(false); // user kickoff — header
    expect(items[1].grouped).toBe(false); // first assistant item — header
    expect(items[2].grouped).toBe(true); // assistant continues — no header
    expect(items[3].grouped).toBe(false); // new user turn — header
  });

  it("marks turnStart on user messages and settles items before the latest human turn", () => {
    const items = buildStreamItems([
      text("user", "kickoff", "m1"),
      text("assistant", "計画", "m2"),
      text("user", "承認", "m3"),
      text("assistant", "報告", "m4"),
    ]);
    const flags = items.map((i) => (i.kind === "message" ? i.turnStart : false));
    expect(flags).toEqual([true, false, true, false]);
    expect(items.map((i) => i.settled)).toEqual([true, true, false, false]);
  });

  it("does not collapse a message that mixes text and tool calls", () => {
    const mixed: ThreadMessageLike = {
      id: "m2",
      role: "assistant",
      content: [
        { type: "text", text: "実行します" },
        { type: "tool-call", toolCallId: "tc-x", toolName: "dispatch", args: {} },
      ],
    };
    const items = buildStreamItems([text("user", "kickoff", "m1"), mixed]);
    expect(items[1].kind).toBe("message");
  });
});
