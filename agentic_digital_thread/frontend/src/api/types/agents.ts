// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Types for the agent fleet endpoints, derived from the
 * agents-fleet consumer usage (cross-checked against
 * agents/agents_routes.py). All fields the JS reads defensively are optional. */

/** One eval metric aggregate inside `eval_scores` (avg in [0,1]). The
 * code-based figure check (`FigureCheck`) carries a pass rate as `avg`,
 * `null` while only answers without figures were sampled, plus the counts of
 * answers it had no figure to check (`no_figures`) or could not check
 * (`unchecked`), never averaged into the rate. */
export interface EvalScore {
  avg: number | null;
  count?: number;
  no_figures?: number;
  unchecked?: number;
}

/** One runtime entry from GET /api/admin/agents?hours=. */
export interface AgentInfo {
  runtime_name?: string;
  name?: string;
  display_name?: string;
  status?: string;
  model_id?: string;
  /** True for tool-only runtimes (no LLM behind them). */
  no_llm?: boolean;
  invocations_24h?: number;
  avg_latency_s?: number;
  cost_usd_24h?: number;
  cost_per_request?: number;
  tool_calls_24h?: number;
  avg_events_per_session?: number;
  input_tokens_24h?: number;
  output_tokens_24h?: number;
  /** A model bucket is in both roles' pools; Bedrock cannot split its tokens
   * by runtime, so this row's figures include tokens the other role's rows
   * also show and may overstate its own share. */
  model_shared_across_roles?: boolean;
  /** The fleet's measured totals, each model bucket counted once (the same on
   * every row of a cloud payload; absent from the local fleet). */
  fleet_input_tokens_24h?: number;
  fleet_output_tokens_24h?: number;
  fleet_cost_usd_24h?: number;
  has_eval?: boolean;
  eval_scores?: Record<string, EvalScore>;
  /** Native CloudWatch deep-links (fail-soft: absent on older/local payloads). */
  observability_url?: string;
  logs_url?: string;
}

/** GET /api/admin/agents?hours= — batched CloudWatch fleet metrics. */
export interface AgentsResponse {
  agents?: AgentInfo[];
  /** Present when the payload was served from the snapshot cache. */
  cached?: boolean;
  [k: string]: unknown;
}

/** One model in the curated catalog (GET /api/admin/models) — alias of the
 * single superset definition in api/types/admin.ts (DRY type merge). */
export type { ModelCatalogEntry as CatalogModel, ModelCatalog } from './admin';

/** One line from GET /api/admin/agents/log-tail?lines=. */
export interface LogTailLine {
  /** Epoch millis. */
  ts: number;
  msg: string;
}

/** GET /api/admin/agents/log-tail?lines= */
export interface LogTailResponse {
  lines?: LogTailLine[];
}

/* ---- Observability drill-down (frozen O2 contract) --------------------- */
/* Both trace routes return `{"enabled": false}` (HTTP 200) when the
 * AGENTS_OBSERVABILITY_DRILLDOWN feature gate is off — the frontend
 * feature-detects on that field and renders nothing (graceful degrade). */

/** One recent session row from GET /api/admin/agents/traces/sessions?hours=.
 * Only `session_id` is contractual; the rest is read defensively. */
export interface TraceSessionSummary {
  session_id: string;
  span_count?: number | null;
  /** Epoch millis of the most recent span, when the backend provides it. */
  last_seen_ms?: number | null;
  [k: string]: unknown;
}

/** GET /api/admin/agents/traces/sessions?hours= */
export interface TraceSessionsResponse {
  enabled?: boolean;
  sessions?: TraceSessionSummary[];
  /** Honest-error contract: when the Logs Insights query fails the route
   * answers HTTP 200 with empty `sessions` and this message — an empty list
   * with `error` set is a FAILED query, not a quiet window. */
  error?: string;
  [k: string]: unknown;
}

/** One span row in the per-session detail (frozen schema). */
export interface TraceSpan {
  name?: string;
  /** Epoch millis (large values) — read defensively. */
  start_ms?: number | null;
  duration_ms?: number | null;
  status?: string;
  tokens_in?: number | null;
  tokens_out?: number | null;
  /** Span classification, e.g. "tool" for gateway tool calls. */
  kind?: string;
}

/** GET /api/admin/agents/traces/sessions/{id} (frozen schema). */
export interface TraceSessionDetail {
  enabled?: boolean;
  session_id?: string;
  spans?: TraceSpan[];
  totals?: {
    tokens_in?: number | null;
    tokens_out?: number | null;
    tool_calls?: number | null;
  };
  /** Honest-error contract: HTTP 200 with empty `spans` and this message
   * when the Logs Insights query fails — distinct from a quiet session. */
  error?: string;
  [k: string]: unknown;
}

/** GET /api/admin/agents/neptune?hours= — Neptune Analytics stats card. */
export interface NeptuneStats {
  status?: string;
  graph_id?: string;
  ncu_count?: number | null;
  provisioned_memory_gb?: number | null;
  replica_count?: number | null;
  mncu_price_per_hour?: number | null;
  /** The graph's state now; a stopped graph is priced at the stopped rate. */
  billing_state?: 'running' | 'stopped' | null;
  cost_usd?: number | null;
  cost_ncu?: number | null;
  query_rate_avg?: number | null;
  query_rate_peak?: number | null;
  cpu_avg?: number | null;
  cpu_peak?: number | null;
  graph_size_mb?: number | null;
  edges?: number | null;
}
