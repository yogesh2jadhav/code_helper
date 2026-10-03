import { useEffect, useState } from "react";
import { getHealth } from "./services/api";

type State = { kind: "loading" } | { kind: "ok"; status: string } | { kind: "error"; message: string };

export default function App() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    getHealth()
      .then((h) => setState({ kind: "ok", status: h.status }))
      .catch((e: unknown) =>
        setState({ kind: "error", message: e instanceof Error ? e.message : String(e) }),
      );
  }, []);

  return (
    <main style={{ fontFamily: "system-ui, sans-serif", padding: "2rem" }}>
      <h1>Code Helper</h1>
      <p>Local developer knowledge-transfer system.</p>
      <p data-testid="backend-status">
        Backend:{" "}
        {state.kind === "loading" && "checking…"}
        {state.kind === "ok" && state.status}
        {state.kind === "error" && `unreachable (${state.message})`}
      </p>
    </main>
  );
}
