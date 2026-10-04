import { useEffect, useRef, useState } from "react";
import { inRange, nextSelection, type LineRange } from "../lib/selection";
import { api } from "../services/api";
import type { SourceView } from "../types/api";

interface Props {
  repositoryId: string;
  path: string | null;
  /** Lines of the method being studied; drawn with a tinted background. */
  method?: LineRange | null;
  /** The user's current selection; controlled by the parent. */
  selection: LineRange | null;
  onSelect: (range: LineRange) => void;
  /** Line to scroll to (e.g. after clicking a citation). `nonce` re-triggers scrolling to the same line. */
  focus?: { line: number; nonce: number } | null;
}

export default function SourceViewer({ repositoryId, path, method, selection, onSelect, focus }: Props) {
  const [view, setView] = useState<SourceView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!path) {
      setView(null);
      return;
    }
    let cancelled = false;
    setError(null);
    api
      .sourceByPath(repositoryId, path)
      .then((v) => !cancelled && setView(v))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      cancelled = true;
    };
  }, [repositoryId, path]);

  const target = focus?.line ?? method?.start;
  useEffect(() => {
    if (!view || !target) return;
    const el = container.current?.querySelector(`[data-line="${target}"]`);
    // scrollIntoView does not exist in every DOM implementation (jsdom), so guard the call.
    if (el && typeof el.scrollIntoView === "function") el.scrollIntoView({ block: "center" });
  }, [view, target, focus?.nonce]);

  if (!path) return <div className="empty">Select a method to see its source.</div>;
  if (error) return <div className="error" role="alert">{error}</div>;
  if (!view) return <div className="empty">Loading source…</div>;

  const lines = view.text.split("\n");
  return (
    <div className="source" ref={container} aria-label={`Source of ${view.relative_path}`}>
      <div className="source-path">{view.relative_path}</div>
      <div className="source-lines">
        {lines.map((text, i) => {
          const line = view.start_line + i;
          const classes = ["line"];
          if (inRange(method, line)) classes.push("in-method");
          if (inRange(selection, line)) classes.push("selected");
          return (
            <div
              key={line}
              data-line={line}
              className={classes.join(" ")}
              onClick={(e) => onSelect(nextSelection(selection, line, e.shiftKey))}
            >
              <span className="gutter">{line}</span>
              <span className="code">{text || " "}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
