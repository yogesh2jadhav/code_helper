import { useEffect, useState } from "react";
import ClassTree from "../components/ClassTree";
import MentorPanel from "../components/MentorPanel";
import SourceViewer from "../components/SourceViewer";
import type { LineRange } from "../lib/selection";
import { api } from "../services/api";
import type { ClassSummary, MethodSummary } from "../types/api";

export default function ExplorerPage({ repositoryId }: { repositoryId: string }) {
  const [classes, setClasses] = useState<ClassSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [method, setMethod] = useState<MethodSummary | null>(null);
  const [path, setPath] = useState<string | null>(null);
  const [selection, setSelection] = useState<LineRange | null>(null);
  const [focus, setFocus] = useState<{ line: number; nonce: number } | null>(null);

  useEffect(() => {
    setClasses([]);
    setMethod(null);
    setPath(null);
    setError(null);
    api.classes(repositoryId).then(setClasses).catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [repositoryId]);

  const open = (m: MethodSummary) => {
    setMethod(m);
    setPath(m.file);
    setSelection(null);
    setFocus({ line: m.start_line, nonce: Date.now() });
  };
  const jump = (file: string, line: number) => {
    setPath(file);
    setFocus({ line, nonce: Date.now() });
  };

  if (error) return <div className="error" role="alert">{error}</div>;
  return (
    <div className="explorer">
      <ClassTree classes={classes} selectedMethodId={method?.id ?? null} onSelectMethod={open} />
      <SourceViewer
        repositoryId={repositoryId}
        path={path}
        method={method && path === method.file ? { start: method.start_line, end: method.end_line } : null}
        selection={selection}
        onSelect={setSelection}
        focus={focus}
      />
      <MentorPanel repositoryId={repositoryId} method={method} selection={selection} onJump={jump} onOpenMethod={open} />
    </div>
  );
}
