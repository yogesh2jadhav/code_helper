// Types mirror the backend's JSON. Only the fields the UI uses are declared.

export interface HealthResponse {
  status: string;
}

export interface Repository {
  id: string;
  root_path: string;
  last_scanned: string | null;
  file_count: number;
}

export interface RepositoryStats {
  repository_id: string;
  counts: Record<string, number>;
  last_run: Record<string, string | number | null> | null;
}

export interface ClassSummary {
  id: string;
  fqn: string;
  name: string;
  kind: string;
  package: string | null;
  file: string;
  file_id: string;
  start_line: number;
  end_line: number;
  is_test: boolean;
  method_count: number;
}

export interface MethodSummary {
  id: string;
  method_id: string;
  class_id: string;
  class_fqn: string;
  name: string;
  signature: string;
  kind: string;
  visibility: string | null;
  file: string;
  file_id: string;
  start_line: number;
  end_line: number;
  is_test: boolean;
  complexity: number | null;
  purpose: string | null;
  purpose_basis: string | null;
  callers_count: number;
  callees_count: number;
  rules_count: number;
  risks_count: number;
  unknowns_count: number;
}

export interface Purpose {
  text: string;
  basis: "comment" | "name" | "structure";
  level: "fact" | "inference";
  structure: string | null;
  evidence_ids: string[];
}

export interface ParameterInfo {
  name: string;
  type: string;
  var_args: boolean;
  comes_from: string[];
}

export interface CalleeInfo {
  method_id: string | null;
  name: string;
  owner_type: string | null;
  status: "resolved" | "ambiguous" | "unresolved";
  origin: string | null;
  site_lines: number[];
  candidates: string[];
  may_dispatch_to: string[];
  reason: string | null;
  purpose: string | null;
}

export interface CallerInfo {
  method_id: string;
  site_lines: number[];
  ambiguous: boolean;
  purpose: string | null;
}

export interface Risk {
  kind: string;
  level: "low" | "medium" | "high";
  message: string;
  line: number | null;
}

export interface Unknown {
  kind: string;
  message: string;
  line: number | null;
}

export interface RuleCandidate {
  id: string;
  kind: string;
  start_line: number;
  end_line: number;
  condition: string | null;
  meaning: string | null;
  scope: string | null;
  calculation: string | null;
  action: string | null;
  otherwise: string | null;
  literals: string[];
  confidence: "high" | "medium";
}

export interface FlowNode {
  kind: string;
  start_line: number;
  end_line: number;
  text: string | null;
  loop_kind: string | null;
  target: string | null;
  operator: string | null;
  children: FlowNode[];
  branches: FlowNode[];
}

export interface FlowEvent {
  kind: string;
  line: number;
  detail: string;
}

export interface VariableFlow {
  name: string;
  kind: string;
  type: string | null;
  symbol_id: string;
  declared_line: number;
  created: string | null;
  events: FlowEvent[];
}

export interface FlowRef {
  kind: string;
  name: string;
  text: string;
  line: number;
}

export interface DataFlowEdge {
  source: FlowRef;
  target: FlowRef;
  via: string;
  line: number;
}

export interface Evidence {
  id: string;
  source_type: string;
  class_name: string | null;
  method: string | null;
  file: string;
  start_line: number;
  end_line: number;
  snippet: string;
  confidence: string;
  relation: string;
}

export interface MethodKnowledge
  extends Omit<MethodSummary, "purpose" | "purpose_basis" | "complexity" | "callers_count" | "callees_count" | "rules_count" | "risks_count" | "unknowns_count"> {
  modifiers: string[];
  annotations: string[];
  parameters: ParameterInfo[];
  output: { type: string | null; returns: string[]; side_effects: string[] };
  callees: CalleeInfo[];
  callers: CallerInfo[];
  control_flow: { nodes: FlowNode[]; summary: Record<string, number> };
  data_flow: { variables: VariableFlow[]; edges: DataFlowEdge[] };
  rule_candidates: RuleCandidate[];
  evidence: Evidence[];
  risks: Risk[];
  unknowns: Unknown[];
  purpose: Purpose;
  complexity: { cyclomatic: number; nesting_depth: number; lines: number; statements: number; expressions: number };
  return_type: string | null;
}

export interface Citation {
  label: string;
  file: string;
  start_line: number;
  end_line: number;
  source_type: string;
  note: string;
}

export interface Stage {
  kind: string;
  title: string;
  description: string;
  start_line: number;
  end_line: number;
  refs: string[];
}

export interface ExplanationPlan {
  method_id: string;
  method_identity: string;
  purpose: { text: string; basis: string; level: string; refs: string[] };
  input_summary: string[];
  output_summary: string[];
  major_stages: Stage[];
  unknowns: string[];
  citations: Citation[];
}

export interface AnswerSection {
  key: string;
  title: string;
  text: string;
}

export interface ParsedAnswer {
  raw: string;
  sections: AnswerSection[];
  citations_used: string[];
  invalid_citations: string[];
  missing_sections: string[];
  states_unknowns: boolean;
}

export interface AnswerInfo {
  answer: string;
  answer_source: "llm" | "deterministic";
  parsed: ParsedAnswer;
  warnings: string[];
  model: string | null;
  llm_error: string | null;
  duration_ms: number;
}

export interface ExplainResult extends AnswerInfo {
  method: MethodSummary;
  explanation_plan: ExplanationPlan;
  evidence: Citation[];
  unknowns: string[];
  context_tokens: number;
  context_omitted: string[];
  conversation_id: string | null;
}

export interface TraceStep {
  direction: "upstream" | "within" | "downstream";
  kind: string;
  depth: number;
  method_id: string;
  text: string;
  file: string | null;
  line: number | null;
  label: string | null;
}

export interface TraceExplanation extends AnswerInfo {
  trace: {
    variable: string;
    variable_kind: string;
    type: string | null;
    steps: TraceStep[];
    stops: string[];
    citations: Citation[];
  };
}

export interface WhyPoint {
  text: string;
  refs: string[];
}

export interface WhyExplanation extends AnswerInfo {
  why: {
    start_line: number;
    end_line: number;
    selection: string;
    confirmed: WhyPoint[];
    likely: WhyPoint[];
    unknown: string[];
    citations: Citation[];
  };
}

export interface ChatResult {
  conversation_id: string;
  answer: string;
  warnings: string[];
  model: string | null;
}

export interface Related {
  method: MethodSummary | null;
  method_id: string;
  depth: number;
  ambiguous: boolean;
  via_override: boolean;
}

export interface SourceView {
  file_id: string;
  relative_path: string;
  package: string | null;
  total_lines: number;
  start_line: number;
  end_line: number;
  text: string;
}

export interface Job {
  id: string;
  state: "running" | "succeeded" | "failed";
  stage: string;
  done: number;
  total: number;
  error: string | null;
  root: string;
  result: { index: { total_files: number; analyzed: number; parse_errors: number }; knowledge: { skipped: boolean; stats: Record<string, number> } | null } | null;
}

export interface LLMStatus {
  model: string;
  reachable: boolean;
  model_installed: boolean;
  installed_models: string[];
  error: string | null;
}
