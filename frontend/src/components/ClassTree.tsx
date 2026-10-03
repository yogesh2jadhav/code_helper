import { useMemo, useState } from "react";
import { groupByPackage, shortName } from "../lib/tree";
import { api } from "../services/api";
import type { ClassSummary, MethodSummary } from "../types/api";

interface Props {
  classes: ClassSummary[];
  selectedMethodId: string | null;
  onSelectMethod: (method: MethodSummary) => void;
}

export default function ClassTree({ classes, selectedMethodId, onSelectMethod }: Props) {
  const [filter, setFilter] = useState("");
  const [open, setOpen] = useState<Record<string, MethodSummary[] | "loading" | "error">>({});
  const groups = useMemo(() => groupByPackage(classes, filter), [classes, filter]);

  const toggle = (c: ClassSummary) => {
    if (open[c.id]) {
      setOpen(({ [c.id]: _gone, ...rest }) => rest);
      return;
    }
    setOpen((o) => ({ ...o, [c.id]: "loading" }));
    api
      .classMethods(c.id)
      .then((methods) => setOpen((o) => ({ ...o, [c.id]: methods })))
      .catch(() => setOpen((o) => ({ ...o, [c.id]: "error" })));
  };

  return (
    <nav className="tree" aria-label="Classes">
      <input
        type="search"
        placeholder="Filter classes…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        aria-label="Filter classes"
      />
      {groups.length === 0 && <div className="empty">No classes match.</div>}
      {groups.map((g) => (
        <section key={g.name}>
          <h4 className="package">{g.name}</h4>
          <ul>
            {g.classes.map((c) => {
              const methods = open[c.id];
              return (
                <li key={c.id}>
                  <button
                    type="button"
                    className={"class-row" + (c.is_test ? " is-test" : "")}
                    aria-expanded={!!methods}
                    onClick={() => toggle(c)}
                  >
                    <span className="twisty">{methods ? "▾" : "▸"}</span>
                    {shortName(c)}
                    <span className="count">{c.method_count}</span>
                  </button>
                  {methods === "loading" && <div className="empty small">Loading…</div>}
                  {methods === "error" && <div className="error small">Could not load methods.</div>}
                  {Array.isArray(methods) && (
                    <ul className="methods">
                      {methods.map((m) => (
                        <li key={m.id}>
                          <button
                            type="button"
                            className={"method-row" + (m.id === selectedMethodId ? " active" : "")}
                            onClick={() => onSelectMethod(m)}
                            title={m.purpose ?? undefined}
                          >
                            {m.signature}
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </nav>
  );
}
