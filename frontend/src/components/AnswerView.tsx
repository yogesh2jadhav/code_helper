import type { AnswerInfo, Citation } from "../types/api";
import Markdown from "./Markdown";

interface Props {
  info: AnswerInfo;
  citations: Citation[];
  onJump: (citation: Citation) => void;
}

/** One model (or deterministic) answer: provenance badge, warnings, markdown, clickable evidence. */
export default function AnswerView({ info, citations, onJump }: Props) {
  const byLabel = new Map(citations.map((c) => [c.label, c]));
  const used = info.parsed.citations_used.filter((l) => byLabel.has(l));
  return (
    <div className="answer">
      <div className="badges">
        {info.answer_source === "llm" ? (
          <span className="badge llm">LLM · {info.model}</span>
        ) : (
          <span className="badge det">Analysis only</span>
        )}
        <span className="badge muted">{(info.duration_ms / 1000).toFixed(1)}s</span>
      </div>
      {info.llm_error && (
        <div className="notice" role="status">
          The language model was unavailable: {info.llm_error}
        </div>
      )}
      {info.warnings.map((w) => (
        <div className="notice warn" key={w} role="status">
          {w}
        </div>
      ))}
      <Markdown
        text={info.answer}
        known={new Set(byLabel.keys())}
        onCite={(label) => {
          const c = byLabel.get(label);
          if (c) onJump(c);
        }}
      />
      {used.length > 0 && (
        <details className="evidence-list">
          <summary>Evidence ({used.length})</summary>
          <ul>
            {used.map((label) => {
              const c = byLabel.get(label)!;
              return (
                <li key={label}>
                  <button type="button" className="cite" onClick={() => onJump(c)}>
                    {label}
                  </button>{" "}
                  <span className="muted">
                    {c.source_type} · {c.file}:{c.start_line}
                    {c.end_line !== c.start_line && `-${c.end_line}`}
                  </span>
                </li>
              );
            })}
          </ul>
        </details>
      )}
    </div>
  );
}
