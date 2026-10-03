import { useCallback, useEffect, useState } from "react";
import ExplorerPage from "./pages/ExplorerPage";
import RepositoryPage from "./pages/RepositoryPage";
import { api } from "./services/api";
import type { Repository } from "./types/api";

type Page = "repository" | "explorer";

export default function App() {
  const [page, setPage] = useState<Page>("repository");
  const [repositories, setRepositories] = useState<Repository[]>([]);
  const [repositoryId, setRepositoryId] = useState<string | null>(null);
  const [backend, setBackend] = useState<"checking" | "ok" | "down">("checking");

  const refresh = useCallback(() => {
    api
      .repositories()
      .then((list) => {
        setRepositories(list);
        setRepositoryId((current) => current ?? list[0]?.id ?? null);
        setBackend("ok");
      })
      .catch(() => setBackend("down"));
  }, []);
  useEffect(refresh, [refresh]);

  return (
    <div className="app">
      <header className="topbar">
        <h1>Code Helper</h1>
        <nav>
          <button className={page === "repository" ? "active" : ""} onClick={() => setPage("repository")}>
            Repository
          </button>
          <button className={page === "explorer" ? "active" : ""} onClick={() => setPage("explorer")} disabled={!repositoryId}>
            Code Explorer
          </button>
        </nav>
        <span className={"status " + backend} data-testid="backend-status">
          {backend === "ok" ? "backend connected" : backend === "down" ? "backend unreachable" : "checking…"}
        </span>
      </header>
      <main>
        {backend === "down" && (
          <div className="error" role="alert">
            Cannot reach the backend. Start it with <code>make backend</code>.
          </div>
        )}
        {page === "repository" && (
          <RepositoryPage
            repositories={repositories}
            repositoryId={repositoryId}
            onSelect={(id) => {
              setRepositoryId(id);
              setPage("explorer");
            }}
            onIndexed={refresh}
          />
        )}
        {page === "explorer" && repositoryId && <ExplorerPage repositoryId={repositoryId} />}
      </main>
    </div>
  );
}
