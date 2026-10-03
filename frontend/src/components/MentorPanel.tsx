import { useEffect, useState } from "react";
import { describeRange, type LineRange } from "../lib/selection";
import { api } from "../services/api";
import type {
  ChatResult,
  Citation,
  ExplainResult,
  MethodKnowledge,
  MethodSummary,
  Related,
  TraceExplanation,
  WhyExplanation,
} from "../types/api";
import AnswerView from "./AnswerView";
import KnowledgePanel from "./KnowledgePanel";
import Markdown from "./Markdown";

export type Tab = "explain" | "trace" | "rules" | "deps" | "knowledge";
const TABS: [Tab, string][] = [
  ["explain", "Explain"],
  ["trace", "Trace Data"],
  ["rules", "Business Rules"],
  ["deps", "Dependencies"],
  ["knowledge", "Knowledge"],
];

interface Props {
  repositoryId: string;
  method: MethodSummary | null;
  selection: LineRange | null;
  onJump: (file: string, line: number) => void;
  onOpenMethod: (method: MethodSummary) => void;
}

function useAsync<T>(run: () => Promise<T>) {
  const [state, setState] = useState<{ busy: boolean; error: string | null; value: T | null }>({
    busy: false,
    error: null,
    value: null,
  });
  const exec = async () => {
    setState({ busy: true, error: null, value: null });
    try {
      setState({ busy: false, error: null, value: await run() });
    } catch (e) {
      setState({ busy: false, error: e instanceof Error ? e.message : String(e), value: null });
    }
  };
  return { ...state, exec, reset: () => setState({ busy: false, error: null, value: null }) };
}

function Failure({ message }: { message: string }) {
  return (
    <div className="error" role="alert">
      {message}
    </div>
  );
}

export default function MentorPanel({ repositoryId: _repo, method, selection, onJump, onOpenMethod }: Props) {
  const [tab, setTab] = useState<Tab>("explain");
  const [knowledge, setKnowledge] = useState<MethodKnowledge | null>(null);
  const [knowledgeError, setKnowledgeError] = useState<string | null>(null);

  useEffect(() => {
    setKnowledge(null);
    setKnowledgeError(null);
    if (!method) return;
    let cancelled = false;
    api
      .knowledge(method.id)
      .then((k) => !cancelled && setKnowledge(k))
      .catch((e: unknown) => !cancelled && setKnowledgeError(e instanceof Error ? e.message : String(e)));
    return () => {
      cancelled = true;
    };
  }, [method?.id]);

  if (!method) return <aside className="mentor empty">Select a method to start.</aside>;

  const jumpTo = (c: Citation) => onJump(c.file, c.start_line);
  return (
    <aside className="mentor">
      <header>
        <h3>{method.signature}</h3>
        <div className="muted small">{method.class_fqn}</div>
      </header>
      <div role="tablist" className="tabs">
        {TABS.map(([id, label]) => (
          <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? "active" : ""} onClick={() => setTab(id)}>
            {label}
          </button>
        ))}
      </div>
      <div className="tab-body" key={method.id}>
        {knowledgeError && <Failure message={knowledgeError} />}
        {tab === "explain" && <ExplainTab method={method} onJump={jumpTo} />}
        {tab === "trace" && <TraceTab method={method} knowledge={knowledge} onJump={jumpTo} />}
        {tab === "rules" && <RulesTab method={method} knowledge={knowledge} selection={selection} onJump={jumpTo} onLine={(l) => onJump(method.file, l)} />}
        {tab === "deps" && <DepsTab method={method} onOpen={onOpenMethod} />}
        {tab === "knowledge" &&
          (knowledge ? <KnowledgePanel knowledge={knowledge} onJumpLine={(l, f) => onJump(f ?? method.file, l)} /> : <div className="empty">Loading…</div>)}
      </div>
    </aside>
  );
}

// ---- Explain + follow-up chat ------------------------------------------------------------------

function ExplainTab({ method, onJump }: { method: MethodSummary; onJump: (c: Citation) => void }) {
  const [useLlm, setUseLlm] = useState(true);
  const [depth, setDepth] = useState(2);
  const explain = useAsync<ExplainResult>(() =>
    api.explain(method.id, { depth, include_tests: true, include_docs: true, use_llm: useLlm }),
  );
  const result = explain.value;
  return (
    <div>
      <div className="controls">
        <button type="button" className="primary" disabled={explain.busy} onClick={explain.exec}>
          {explain.busy ? "Explaining…" : "Explain this method"}
        </button>
        <label>
          <input type="checkbox" checked={useLlm} onChange={(e) => setUseLlm(e.target.checked)} /> use language model
        </label>
        <label>
          depth{" "}
          <select value={depth} onChange={(e) => setDepth(Number(e.target.value))}>
            {[1, 2, 3].map((d) => (
              <option key={d}>{d}</option>
            ))}
          </select>
        </label>
      </div>
      {explain.error && <Failure message={explain.error} />}
      {result && (
        <>
          <AnswerView info={result} citations={result.evidence} onJump={onJump} />
          {result.unknowns.length > 0 && (
            <details className="unknowns">
              <summary>Not established ({result.unknowns.length})</summary>
              <ul>
                {result.unknowns.map((u) => (
                  <li key={u}>{u}</li>
                ))}
              </ul>
            </details>
          )}
          {result.conversation_id && <Chat conversationId={result.conversation_id} />}
        </>
      )}
    </div>
  );
}

function Chat({ conversationId }: { conversationId: string }) {
  const [turns, setTurns] = useState<{ q: string; a?: ChatResult; error?: string }[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);

  const send = async () => {
    const q = question.trim();
    if (!q || busy) return;
    setQuestion("");
    setBusy(true);
    setTurns((t) => [...t, { q }]);
    try {
      const a = await api.chat(conversationId, q);
      setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { ...x, a } : x)));
    } catch (e) {
      const error = e instanceof Error ? e.message : String(e);
      setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { ...x, error } : x)));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="chat">
      <h4>Ask a follow-up</h4>
      {turns.map((t, i) => (
        <div key={i} className="turn">
          <div className="q">{t.q}</div>
          {t.a && <Markdown text={t.a.answer} />}
          {t.a?.warnings.map((w) => (
            <div className="notice warn" key={w}>
              {w}
            </div>
          ))}
          {t.error && <Failure message={t.error} />}
          {!t.a && !t.error && <div className="muted">Thinking…</div>}
        </div>
      ))}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <input value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="e.g. What happens for express delivery?" aria-label="Follow-up question" />
        <button type="submit" disabled={busy || !question.trim()}>
          Ask
        </button>
      </form>
    </div>
  );
}

// ---- Trace -------------------------------------------------------------------------------------

function TraceTab({ method, knowledge, onJump }: { method: MethodSummary; knowledge: MethodKnowledge | null; onJump: (c: Citation) => void }) {
  const names = knowledge ? [...new Set([...knowledge.parameters.map((p) => p.name), ...knowledge.data_flow.variables.map((v) => v.name)])] : [];
  const [variable, setVariable] = useState("");
  const [useLlm, setUseLlm] = useState(true);
  const chosen = variable || names[0] || "";
  const trace = useAsync<TraceExplanation>(() => api.trace(method.id, chosen, 2, useLlm));
  const result = trace.value;
  return (
    <div>
      <div className="controls">
        <label>
          variable{" "}
          <select value={chosen} onChange={(e) => setVariable(e.target.value)} aria-label="Variable to trace">
            {names.map((n) => (
              <option key={n}>{n}</option>
            ))}
          </select>
        </label>
        <button type="button" className="primary" disabled={trace.busy || !chosen} onClick={trace.exec}>
          {trace.busy ? "Tracing…" : "Trace data"}
        </button>
        <label>
          <input type="checkbox" checked={useLlm} onChange={(e) => setUseLlm(e.target.checked)} /> use language model
        </label>
      </div>
      {trace.error && <Failure message={trace.error} />}
      {result && (
        <>
          <ol className="steps">
            {result.trace.steps.map((s, i) => {
              const c = result.trace.citations.find((x) => x.label === s.label);
              return (
                <li key={i} className={s.direction} style={{ marginLeft: s.depth * 12 }}>
                  <span className="badge muted">{s.direction}</span> {s.text}{" "}
                  {c && (
                    <button type="button" className="cite" onClick={() => onJump(c)}>
                      {c.label}
                    </button>
                  )}
                </li>
              );
            })}
          </ol>
          {result.trace.stops.map((s) => (
            <div key={s} className="muted small">
              Stops: {s}
            </div>
          ))}
          <AnswerView info={result} citations={result.trace.citations} onJump={onJump} />
        </>
      )}
    </div>
  );
}

// ---- Business rules and "why" -----------------------------------------------------------------

function RulesTab({
  method,
  knowledge,
  selection,
  onJump,
  onLine,
}: {
  method: MethodSummary;
  knowledge: MethodKnowledge | null;
  selection: LineRange | null;
  onJump: (c: Citation) => void;
  onLine: (line: number) => void;
}) {
  const [useLlm, setUseLlm] = useState(true);
  const why = useAsync<WhyExplanation>(() => api.why(method.id, selection?.start ?? null, selection?.end ?? null, useLlm));
  const result = why.value;
  return (
    <div>
      <p className="muted small">These are patterns found in the code. Whether they are business rules is not established.</p>
      {!knowledge ? (
        <div className="empty">Loading…</div>
      ) : knowledge.rule_candidates.length === 0 ? (
        <p>No rule candidates were found in this method.</p>
      ) : (
        <ul className="rules">
          {knowledge.rule_candidates.map((r) => (
            <li key={r.id}>
              <button type="button" className="line-link" onClick={() => onLine(r.start_line)}>
                L{r.start_line}
              </button>{" "}
              <b>{r.kind}</b> <span className={"badge " + r.confidence}>{r.confidence}</span>
              <div>{r.meaning ?? r.calculation ?? r.condition}</div>
              {r.action && <div className="muted">then {r.action}</div>}
              {r.otherwise && <div className="muted">otherwise {r.otherwise}</div>}
            </li>
          ))}
        </ul>
      )}
      <div className="controls">
        <button type="button" className="primary" disabled={why.busy} onClick={why.exec}>
          {why.busy ? "Working…" : selection ? `Why does ${describeRange(selection)} do this?` : "Why does this method do this?"}
        </button>
        <label>
          <input type="checkbox" checked={useLlm} onChange={(e) => setUseLlm(e.target.checked)} /> use language model
        </label>
      </div>
      {!selection && <div className="muted small">Click source lines (shift-click for a range) to ask about specific code.</div>}
      {why.error && <Failure message={why.error} />}
      {result && (
        <>
          <div className="why-grid">
            <WhyList title="Confirmed" tone="fact" items={result.why.confirmed.map((p) => p.text)} />
            <WhyList title="Likely (inference)" tone="inf" items={result.why.likely.map((p) => p.text)} />
            <WhyList title="Unknown" tone="unk" items={result.why.unknown} />
          </div>
          <AnswerView info={result} citations={result.why.citations} onJump={onJump} />
        </>
      )}
    </div>
  );
}

function WhyList({ title, tone, items }: { title: string; tone: string; items: string[] }) {
  return (
    <section className={"why " + tone}>
      <h4>{title}</h4>
      {items.length === 0 ? (
        <p className="muted small">none</p>
      ) : (
        <ul>
          {items.map((t) => (
            <li key={t}>{t}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ---- Dependencies ------------------------------------------------------------------------------

function DepsTab({ method, onOpen }: { method: MethodSummary; onOpen: (m: MethodSummary) => void }) {
  const [callees, setCallees] = useState<Related[] | null>(null);
  const [callers, setCallers] = useState<Related[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    Promise.all([api.callees(method.id), api.callers(method.id)])
      .then(([a, b]) => {
        if (!cancelled) {
          setCallees(a);
          setCallers(b);
        }
      })
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      cancelled = true;
    };
  }, [method.id]);
  if (error) return <Failure message={error} />;
  if (!callees || !callers) return <div className="empty">Loading…</div>;
  const list = (title: string, items: Related[]) => (
    <section>
      <h4>
        {title} ({items.length})
      </h4>
      {items.length === 0 && <p className="muted">None found in the repository.</p>}
      <ul>
        {items.map((r) => (
          <li key={r.method_id + r.depth} style={{ marginLeft: (r.depth - 1) * 12 }}>
            {r.method ? (
              <button type="button" className="line-link" onClick={() => onOpen(r.method!)}>
                {r.method.class_fqn.split(".").pop()}.{r.method.signature}
              </button>
            ) : (
              <code>{r.method_id}</code>
            )}
            {r.ambiguous && <span className="badge ambiguous">ambiguous</span>}
            {r.via_override && <span className="badge muted">override</span>}
            {r.method?.purpose && <div className="muted small">{r.method.purpose}</div>}
          </li>
        ))}
      </ul>
    </section>
  );
  return (
    <div>
      {list("Calls", callees)}
      {list("Called by", callers)}
    </div>
  );
}
