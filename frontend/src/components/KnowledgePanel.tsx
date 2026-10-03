import type { FlowNode, MethodKnowledge } from "../types/api";

interface Props {
  knowledge: MethodKnowledge;
  onJumpLine: (line: number, file?: string) => void;
}

function Lines({ n, onJumpLine }: { n: number; onJumpLine: (l: number) => void }) {
  return (
    <button type="button" className="line-link" onClick={() => onJumpLine(n)}>
      L{n}
    </button>
  );
}

function Flow({ nodes, onJumpLine, depth = 0 }: { nodes: FlowNode[]; onJumpLine: (l: number) => void; depth?: number }) {
  return (
    <ul className="flow" style={{ paddingLeft: depth ? 16 : 0 }}>
      {nodes.map((n, i) => (
        <li key={i}>
          <Lines n={n.start_line} onJumpLine={onJumpLine} /> <b>{n.kind}</b>
          {n.text ? <> <code>{n.text}</code></> : null}
          {n.children.length > 0 && <Flow nodes={n.children} onJumpLine={onJumpLine} depth={depth + 1} />}
          {n.branches.length > 0 && <Flow nodes={n.branches} onJumpLine={onJumpLine} depth={depth + 1} />}
        </li>
      ))}
    </ul>
  );
}

/** The knowledge model as the analyzer derived it. No language model is involved here. */
export default function KnowledgePanel({ knowledge: k, onJumpLine }: Props) {
  return (
    <div className="knowledge">
      <section>
        <h4>Purpose</h4>
        <p>
          {k.purpose.text}{" "}
          <span className={"badge " + (k.purpose.level === "fact" ? "fact" : "inf")}>
            {k.purpose.level} · from {k.purpose.basis}
          </span>
        </p>
        {k.purpose.structure && <p className="muted">{k.purpose.structure}</p>}
      </section>
      <section>
        <h4>Inputs</h4>
        {k.parameters.length === 0 ? (
          <p className="muted">No parameters.</p>
        ) : (
          <ul>
            {k.parameters.map((p) => (
              <li key={p.name}>
                <code>{p.type} {p.name}</code>
                {p.comes_from.map((c) => (
                  <div key={c} className="muted small">{c}</div>
                ))}
              </li>
            ))}
          </ul>
        )}
      </section>
      <section>
        <h4>Output</h4>
        <p>
          <code>{k.output.type ?? "unknown"}</code>
          {k.output.returns.length > 0 && <> — {k.output.returns.join("; ")}</>}
        </p>
        {k.output.side_effects.length > 0 && <p>Side effects: {k.output.side_effects.join("; ")}</p>}
      </section>
      <section>
        <h4>Control flow</h4>
        {k.control_flow.nodes.length === 0 ? <p className="muted">Straight-line code.</p> : <Flow nodes={k.control_flow.nodes} onJumpLine={onJumpLine} />}
      </section>
      <section>
        <h4>Data flow</h4>
        {k.data_flow.edges.length === 0 ? (
          <p className="muted">No data movement recorded.</p>
        ) : (
          <ul>
            {k.data_flow.edges.map((e, i) => (
              <li key={i}>
                <Lines n={e.line} onJumpLine={onJumpLine} /> {e.source.text || e.source.name} → <code>{e.target.name}</code>{" "}
                <span className="muted">({e.via})</span>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section>
        <h4>Business-rule candidates</h4>
        {k.rule_candidates.length === 0 ? (
          <p className="muted">None found.</p>
        ) : (
          <ul>
            {k.rule_candidates.map((r) => (
              <li key={r.id}>
                <Lines n={r.start_line} onJumpLine={onJumpLine} /> <b>{r.kind}</b> {r.meaning ?? r.calculation ?? r.condition}
                {r.action && <> → {r.action}</>}
              </li>
            ))}
          </ul>
        )}
        <p className="muted small">Patterns in the code; whether they are business rules is not established.</p>
      </section>
      <section>
        <h4>Calls ({k.callees.length}) / called by ({k.callers.length})</h4>
        <ul>
          {k.callees.map((c, i) => (
            <li key={"c" + i}>
              → <code>{c.name}</code> <span className={"badge " + c.status}>{c.status}</span>
              {c.reason && <span className="muted small"> {c.reason}</span>}
            </li>
          ))}
          {k.callers.map((c) => (
            <li key={c.method_id}>← <code>{c.method_id.split("#")[0].split(".").pop() + "." + c.method_id.split("#")[1]}</code></li>
          ))}
        </ul>
      </section>
      <section>
        <h4>Risks</h4>
        {k.risks.length === 0 ? (
          <p className="muted">None flagged.</p>
        ) : (
          <ul>
            {k.risks.map((r, i) => (
              <li key={i}><span className={"badge " + r.level}>{r.level}</span> {r.message}</li>
            ))}
          </ul>
        )}
      </section>
      <section>
        <h4>Unknowns</h4>
        {k.unknowns.length === 0 ? (
          <p className="muted">None.</p>
        ) : (
          <ul>
            {k.unknowns.map((u, i) => (
              <li key={i}>
                {u.line ? <><Lines n={u.line} onJumpLine={onJumpLine} />{" "}</> : null}
                {u.message}
              </li>
            ))}
          </ul>
        )}
      </section>
      <section>
        <h4>Evidence ({k.evidence.length})</h4>
        <ul>
          {k.evidence.map((e) => (
            <li key={e.id}>
              <b>{e.source_type}</b> <span className="muted">{e.relation}</span>{" "}
              <button type="button" className="line-link" onClick={() => onJumpLine(e.start_line, e.file)}>
                {e.file}:{e.start_line}
              </button>
              <pre className="snippet">{e.snippet}</pre>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
