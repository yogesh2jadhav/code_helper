import { describe, expect, it } from "vitest";
import { parseInline, parseMarkdown } from "../lib/markdown";
import { describeRange, inRange, nextSelection } from "../lib/selection";
import { groupByPackage, shortName } from "../lib/tree";
import { describeError } from "../services/api";
import type { ClassSummary } from "../types/api";

const cls = (fqn: string, pkg: string | null): ClassSummary => ({
  id: fqn, fqn, name: fqn.split(".").pop()!, kind: "class", package: pkg, file: "x.java", file_id: "f",
  start_line: 1, end_line: 2, is_test: false, method_count: 1,
});

describe("markdown", () => {
  it("parses inline code, bold and citations", () => {
    expect(parseInline("a `b` **c** [E1, E2] d")).toEqual([
      { kind: "text", text: "a " },
      { kind: "code", text: "b" },
      { kind: "text", text: " " },
      { kind: "bold", text: "c" },
      { kind: "text", text: " " },
      { kind: "cite", labels: ["E1", "E2"] },
      { kind: "text", text: " d" },
    ]);
  });
  it("does not treat [x] or [E] as citations", () => {
    expect(parseInline("see [x] and [E]")).toEqual([{ kind: "text", text: "see [x] and [E]" }]);
  });
  it("parses headings, lists, code fences and quotes", () => {
    const blocks = parseMarkdown("## 1. Title\ntext\nmore\n\n- a\n- b\n\n1. x\n2. y\n\n```\ncode **not bold**\n```\n> note");
    expect(blocks.map((b) => b.kind)).toEqual(["heading", "paragraph", "list", "list", "code", "quote"]);
    expect(blocks[0]).toMatchObject({ level: 2, inlines: [{ kind: "text", text: "Title" }] });
    expect(blocks[1]).toMatchObject({ inlines: [{ kind: "text", text: "text more" }] });
    expect(blocks[2]).toMatchObject({ ordered: false });
    expect(blocks[3]).toMatchObject({ ordered: true });
    expect(blocks[4]).toEqual({ kind: "code", text: "code **not bold**" });
  });
  it("keeps HTML as plain text", () => {
    expect(parseMarkdown("<script>alert(1)</script>")).toEqual([
      { kind: "paragraph", inlines: [{ kind: "text", text: "<script>alert(1)</script>" }] },
    ]);
  });
});

describe("selection", () => {
  it("selects a line, extends with shift in either direction", () => {
    expect(nextSelection(null, 5, false)).toEqual({ start: 5, end: 5 });
    expect(nextSelection({ start: 5, end: 5 }, 9, true)).toEqual({ start: 5, end: 9 });
    expect(nextSelection({ start: 5, end: 5 }, 2, true)).toEqual({ start: 2, end: 5 });
    expect(nextSelection({ start: 5, end: 9 }, 7, false)).toEqual({ start: 7, end: 7 });
    expect(nextSelection(null, 4, true)).toEqual({ start: 4, end: 4 });
  });
  it("tests membership and describes ranges", () => {
    expect(inRange({ start: 2, end: 4 }, 2)).toBe(true);
    expect(inRange({ start: 2, end: 4 }, 5)).toBe(false);
    expect(inRange(null, 1)).toBe(false);
    expect(describeRange({ start: 3, end: 3 })).toBe("L3");
    expect(describeRange({ start: 3, end: 8 })).toBe("L3-8");
  });
});

describe("tree", () => {
  const all = [cls("b.Zed", "b"), cls("a.Beta", "a"), cls("a.Alpha", "a"), cls("Plain", null)];
  it("groups by package and sorts", () => {
    expect(groupByPackage(all).map((g) => [g.name, g.classes.map((c) => c.fqn)])).toEqual([
      ["(default package)", ["Plain"]],
      ["a", ["a.Alpha", "a.Beta"]],
      ["b", ["b.Zed"]],
    ]);
  });
  it("filters case-insensitively and drops empty groups", () => {
    expect(groupByPackage(all, "ALP").map((g) => g.name)).toEqual(["a"]);
    expect(groupByPackage(all, "nothing")).toEqual([]);
  });
  it("shortens nested names", () => {
    expect(shortName(cls("com.x.Outer.Inner", "com.x"))).toBe("Outer.Inner");
    expect(shortName(cls("Plain", null))).toBe("Plain");
  });
});

describe("describeError", () => {
  it("handles string, validation list, object and missing details", () => {
    expect(describeError(404, { detail: "unknown method" })).toBe("unknown method");
    expect(describeError(422, { detail: [{ msg: "bad" }, { msg: "worse" }] })).toBe("bad; worse");
    expect(describeError(409, { detail: { message: "busy", job_id: "j" } })).toBe("busy");
    expect(describeError(500, null)).toBe("request failed (500)");
  });
});
