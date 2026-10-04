import { useEffect, useRef, useState } from "react";
import { api } from "../services/api";
import type { Job, LLMStatus, Repository, RepositoryStats } from "../types/api";

interface Props {
  repositories: Repository[];
  repositoryId: string | null;
  onSelect: (id: string) => void;
  onIndexed: () => void;
}

export default function RepositoryPage({ repositories, repositoryId, onSelect, onIndexed }: Props) {
  const [stats, setStats] = useState<RepositoryStats | null>(null);
  const [llm, setLlm] = useState<LLMStatus | null>(null);
  const [path, setPath] = useState("");
  const [force, setForce] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    api.llmStatus().then(setLlm).catch(() => setLlm(null));
  }, []);
  useEffect(() => {
    setStats(null);
    if (repositoryId) api.stats(repositoryId).then(setStats).catch(() => setStats(null));
  }, [repositoryId, job?.state]);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const poll = (id: string) => {
    api
      .job(id)
      .then((j) => {
        setJob(j);
        if (j.state === "running") timer.current = window.setTimeout(() => poll(id), 800);
        else if (j.state === "succeeded") onIndexed();
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  };

  const start = async () => {
    setError(null);
    try {
      const j = await api.startIndex(path.trim(), force);
      setJob(j);
      poll(j.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="page repository">
      <h2>Repository</h2>
      <section className="card">
        <h3>Index a Java project</h3>
        <p className="muted small">The source is only read, never modified. Everything stays on this machine.</p>
        <div className="controls">
          <input
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="/path/to/java/project (blank = SOURCE_ROOT)"
            aria-label="Project path"
            className="grow"
          />
          <label>
            <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} /> re-analyze everything
          </label>
          <button type="button" className="primary" onClick={start} disabled={job?.state === "running"}>
            Index
          </button>
        </div>
        {error && <div className="error" role="alert">{error}</div>}
        {job && (
          <div className="job" aria-live="polite">
            <div>
              <b>{job.state}</b> · {job.stage}
              {job.total > 0 && <> · {job.done}/{job.total}</>}
            </div>
            {job.total > 0 && <progress value={job.done} max={job.total} />}
            {job.error && <div className="error">{job.error}</div>}
          </div>
        )}
      </section>

      <section className="card">
        <h3>Indexed repositories</h3>
        {repositories.length === 0 && <p className="muted">Nothing indexed yet.</p>}
        <ul className="repo-list">
          {repositories.map((r) => (
            <li key={r.id}>
              <button type="button" className={r.id === repositoryId ? "active" : ""} onClick={() => onSelect(r.id)}>
                {r.root_path}
              </button>
              <span className="muted small"> {r.file_count} files</span>
            </li>
          ))}
        </ul>
        {stats && (
          <dl className="stats">
            {Object.entries(stats.counts).map(([k, v]) => (
              <div key={k}>
                <dt>{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
        )}
      </section>

      <section className="card">
        <h3>Language model</h3>
        {!llm ? (
          <p className="muted">Status unavailable.</p>
        ) : llm.reachable && llm.model_installed ? (
          <p>
            <span className="badge fact">ready</span> {llm.model}
          </p>
        ) : (
          <p>
            <span className="badge unk">not ready</span> {llm.error ?? `model ${llm.model} is not installed`}. Explanations fall back to the analysis-only
            answer.
          </p>
        )}
      </section>
    </div>
  );
}
