import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ClassTree from "../components/ClassTree";
import Markdown from "../components/Markdown";
import SourceViewer from "../components/SourceViewer";
import { ApiError, api } from "../services/api";
import type { ClassSummary, MethodSummary, SourceView } from "../types/api";

afterEach(() => vi.restoreAllMocks());

describe("Markdown", () => {
  it("renders HTML-looking text as text, never as elements", () => {
    const { container } = render(<Markdown text={"<img src=x onerror=alert(1)> **bold**"} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("<img src=x onerror=alert(1)>");
    expect(container.querySelector("strong")?.textContent).toBe("bold");
  });
  it("makes known citations clickable and leaves unknown ones plain", () => {
    const onCite = vi.fn();
    render(<Markdown text="Fact [E1] and [E2]." onCite={onCite} known={new Set(["E1"])} />);
    fireEvent.click(screen.getByRole("button", { name: "E1" }));
    expect(onCite).toHaveBeenCalledWith("E1");
    expect(screen.queryByRole("button", { name: "E2" })).toBeNull();
  });
});

const source: SourceView = {
  file_id: "f", relative_path: "src/A.java", package: "p", total_lines: 4, start_line: 1, end_line: 4,
  text: "class A {\n  int x;\n  int y;\n}",
};

describe("SourceViewer", () => {
  beforeEach(() => {
    vi.spyOn(api, "sourceByPath").mockResolvedValue(source);
  });
  it("renders numbered lines and marks the method and selection", async () => {
    const { container } = render(
      <SourceViewer repositoryId="r" path="src/A.java" method={{ start: 2, end: 3 }} selection={{ start: 3, end: 3 }} onSelect={() => {}} />,
    );
    await waitFor(() => expect(container.querySelectorAll(".line")).toHaveLength(4));
    const cls = (n: number) => container.querySelector(`[data-line="${n}"]`)!.className;
    expect(cls(1)).toBe("line");
    expect(cls(2)).toContain("in-method");
    expect(cls(3)).toContain("in-method");
    expect(cls(3)).toContain("selected");
    expect(api.sourceByPath).toHaveBeenCalledWith("r", "src/A.java");
  });
  it("reports clicks and shift-clicks as selections", async () => {
    const onSelect = vi.fn();
    const { container } = render(<SourceViewer repositoryId="r" path="src/A.java" selection={{ start: 2, end: 2 }} onSelect={onSelect} />);
    await waitFor(() => expect(container.querySelectorAll(".line")).toHaveLength(4));
    fireEvent.click(container.querySelector('[data-line="4"]')!);
    expect(onSelect).toHaveBeenLastCalledWith({ start: 4, end: 4 });
    fireEvent.click(container.querySelector('[data-line="4"]')!, { shiftKey: true });
    expect(onSelect).toHaveBeenLastCalledWith({ start: 2, end: 4 });
  });
  it("shows a placeholder without a path and an error when loading fails", async () => {
    const { rerender } = render(<SourceViewer repositoryId="r" path={null} selection={null} onSelect={() => {}} />);
    expect(screen.getByText(/Select a method/)).toBeInTheDocument();
    vi.spyOn(api, "sourceByPath").mockRejectedValue(new ApiError(404, "file not found"));
    rerender(<SourceViewer repositoryId="r" path="missing.java" selection={null} onSelect={() => {}} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("file not found");
  });
});

const klass = (fqn: string, pkg: string, count = 1): ClassSummary => ({
  id: "c_" + fqn, fqn, name: fqn, kind: "class", package: pkg, file: "x", file_id: "f", start_line: 1, end_line: 2, is_test: false, method_count: count,
});
const method = { id: "m1", signature: "run(int)", purpose: "Runs it" } as MethodSummary;

describe("ClassTree", () => {
  it("lists packages, loads methods on expand and selects one", async () => {
    vi.spyOn(api, "classMethods").mockResolvedValue([method]);
    const onSelect = vi.fn();
    render(<ClassTree classes={[klass("shop.Cart", "shop", 3), klass("util.Dates", "util")]} selectedMethodId={null} onSelectMethod={onSelect} />);
    expect(screen.getByText("shop")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Cart/ }));
    fireEvent.click(await screen.findByRole("button", { name: "run(int)" }));
    expect(onSelect).toHaveBeenCalledWith(method);
    expect(api.classMethods).toHaveBeenCalledWith("c_shop.Cart");
    fireEvent.click(screen.getByRole("button", { name: /Cart/ })); // collapse
    expect(screen.queryByRole("button", { name: "run(int)" })).toBeNull();
  });
  it("filters by name and reports an empty result", () => {
    render(<ClassTree classes={[klass("shop.Cart", "shop")]} selectedMethodId={null} onSelectMethod={() => {}} />);
    fireEvent.change(screen.getByLabelText("Filter classes"), { target: { value: "zzz" } });
    expect(screen.getByText("No classes match.")).toBeInTheDocument();
  });
  it("shows an error when methods cannot be loaded", async () => {
    vi.spyOn(api, "classMethods").mockRejectedValue(new Error("boom"));
    render(<ClassTree classes={[klass("shop.Cart", "shop")]} selectedMethodId={null} onSelectMethod={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /Cart/ }));
    expect(await screen.findByText("Could not load methods.")).toBeInTheDocument();
  });
});
