import type {
  ChatResult,
  ClassSummary,
  ExplainResult,
  HealthResponse,
  Job,
  LLMStatus,
  MethodKnowledge,
  MethodSummary,
  Related,
  Repository,
  RepositoryStats,
  SourceView,
  TraceExplanation,
  WhyExplanation,
} from "../types/api";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Turn FastAPI's {"detail": ...} (a string, an object, or a validation list) into a message. */
export function describeError(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => (d as { msg?: string }).msg ?? "invalid input").join("; ");
  }
  if (detail && typeof detail === "object") {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }
  return `request failed (${status})`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch (e) {
    throw new ApiError(0, `cannot reach the backend (${e instanceof Error ? e.message : String(e)})`);
  }
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    throw new ApiError(response.status, describeError(response.status, body), (body as { detail?: unknown } | null)?.detail);
  }
  return body as T;
}

const post = <T>(path: string, body: unknown): Promise<T> =>
  request<T>(path, { method: "POST", body: JSON.stringify(body) });

const qs = (params: Record<string, string | number | boolean | undefined | null>): string => {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  return entries.length ? "?" + new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString() : "";
};

export const api = {
  health: () => request<HealthResponse>("/health"),
  repositories: () => request<Repository[]>("/api/repositories"),
  stats: (repo: string) => request<RepositoryStats>(`/api/repositories/${repo}/stats`),
  classes: (repo: string, q?: string) =>
    request<ClassSummary[]>(`/api/repositories/${repo}/classes${qs({ q, limit: 2000 })}`),
  searchMethods: (repo: string, q: string) =>
    request<MethodSummary[]>(`/api/repositories/${repo}/methods${qs({ q })}`),
  classMethods: (classId: string) => request<MethodSummary[]>(`/api/classes/${classId}/methods`),
  knowledge: (methodId: string) => request<MethodKnowledge>(`/api/methods/${methodId}/knowledge`),
  callers: (methodId: string, depth = 1) =>
    request<Related[]>(`/api/methods/${methodId}/callers${qs({ depth })}`),
  callees: (methodId: string, depth = 1) =>
    request<Related[]>(`/api/methods/${methodId}/callees${qs({ depth })}`),
  source: (fileId: string, start?: number, end?: number) =>
    request<SourceView>(`/api/source/${fileId}${qs({ start, end })}`),
  sourceByPath: (repo: string, path: string, start?: number, end?: number) =>
    request<SourceView>(`/api/repositories/${repo}/source${qs({ path, start, end })}`),
  explain: (methodId: string, options: { depth: number; include_tests: boolean; include_docs: boolean; use_llm: boolean }) =>
    post<ExplainResult>(`/api/methods/${methodId}/explain`, options),
  trace: (methodId: string, variable: string, depth: number, useLlm: boolean) =>
    post<TraceExplanation>(`/api/methods/${methodId}/trace`, { variable, depth, use_llm: useLlm }),
  why: (methodId: string, start: number | null, end: number | null, useLlm: boolean) =>
    post<WhyExplanation>(`/api/methods/${methodId}/why`, { start_line: start, end_line: end, use_llm: useLlm }),
  chat: (conversationId: string, question: string) =>
    post<ChatResult>(`/api/conversations/${conversationId}/messages`, { question }),
  startIndex: (path: string, force: boolean) => post<Job>("/api/repositories/index", { path, force }),
  job: (id: string) => request<Job>(`/api/jobs/${id}`),
  llmStatus: () => request<LLMStatus>("/api/llm/status"),
};
