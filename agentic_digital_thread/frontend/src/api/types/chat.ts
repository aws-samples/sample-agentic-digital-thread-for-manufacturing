// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Types for the /api/ask POST-SSE stream and /api/feedback — derived from the
 * chat client usage (the spec for Task 2.1). */
import type { ProvenanceMetadata } from '../../chat/provenance';

/** POST /api/ask request body (sendMessage). */
export interface AskRequest {
  question: string;
  session_id: string;
  plant: string;
  /** User's IANA timezone (Intl.DateTimeFormat().resolvedOptions().timeZone).
   * Optional — synthetic flows omit it; the server falls back to UTC-only
   * temporal context. */
  timezone?: string;
  /** User's locale tag (navigator.language). Optional, same fallback. */
  locale?: string;
  /** "Ask again" on the chat's last turn only (chat/useAskStream.ts
   * askRequestBody). Absent on every ordinary send. */
  retry?: boolean;
}

/** `event: token` — incremental answer/reasoning text. */
export interface TokenEventData {
  text: string;
}

/** `event: tool_start` — the input args are ONLY available here; the client
 * stashes them (`trace._toolInput`) because tool_result's `input` field
 * carries the RESULT text, not the call arguments. */
export interface ToolStartEventData {
  tool: string;
  tool_id?: string;
  input?: unknown;
  /** Server-side emit timestamp (epoch ms), stamped by chat_client at
   * composition-emit. Optional: older backends omit it. The client timing
   * switch to this server truth is a separate stage — types only here. */
  ts?: number;
}

/** `event: tool_result` — `input` is the RESULT payload (see above);
 * `summary` is the investigation-trace markdown. */
export interface ToolResultEventData {
  tool: string;
  tool_id?: string;
  input?: unknown;
  summary?: string;
  /** Server-side receipt timestamp (epoch ms). Optional (older backends). */
  ts?: number;
  /** Server-known call duration (ms), paired by toolUseId at the emitter —
   * the server truth for pill timing (client wall-clock was skewed by wire
   * buffering). Optional (older backends / unpaired results). */
  elapsed_ms?: number;
  /** The ROUTE context the gateway sent this system call when it differs
   * from the model's own arguments (the route seed's facts merged in).
   * Optional: absent when the model's `kg_context` went out as written. */
  route_context?: string;
  /** The telemetry sample line the system runtime stored for this call
   * (display only): shown as its own line below `summary`, never read by an
   * outcome classifier. Optional: absent when the call stored none. */
  sample_line?: string;
  /** The telemetry-coverage lines the system runtime stored for this call
   * (display only): shown above `summary`, never read by an outcome
   * classifier. Optional: absent when the call stored none. */
  telemetry_coverage?: string[];
}

export interface ToolEndEventData {
  tool?: string;
  tool_id?: string;
}

/** `event: done` — cloud mode sends an empty text; the client reconstruction
 * (answer segments + selectFinalAnswer) is the source of truth. */
export interface DoneEventData {
  text?: string;
  [k: string]: unknown;
}

export interface ErrorEventData {
  text: string;
  /** Closed-taxonomy failure class (server-side events.ERROR_CATEGORIES). */
  category?: string;
  /** App-minted per-turn/session reference the user can quote. */
  correlation_id?: string;
}

/** metadata.intelligence_summary — server-side per-query counts. SINGLE
 * superset definition (merged with the former duplicate in lib/arch-store.ts,
 * which re-exports it). */
export interface IntelligenceSummary {
  total_tool_calls?: number;
  neptune_queries?: number;
  system_queries?: number;
  /** Neptune-only coverage share (new payload). Presence gates the
   * "Graph coverage" line — deploy-skew guard for old runtime payloads. */
  graph_coverage_pct?: number;
  /** Deterministic count of failed/empty tool results (new payload). */
  wasted_calls?: number;
  /** Mechanically-grounded tool steps this turn (additive —
   * `determinism.deterministic_calls` via build_intelligence_summary).
   * Presence gates the grounded/deterministic score line. */
  grounded_steps?: number;
  /** COMPUTED determinism share, 0–100 (additive; never LLM-judged —
   * distinct from the deleted LLM-era key of the same name). */
  deterministic_pct?: number;
  /** Accepted self-learning rules used for this query (server-capped:
   * ≤20 entries, labels ≤160 chars). Falls back to label === id. */
  rules_applied?: { id?: string; label?: string }[];
  /** Mode A route-seed planning summary (KG-contribution additive). The key
   * is present only when a route seed opened this turn — local mode has no
   * route-seed prelude and Mode B omits it, so presence gates the "Planned
   * with" part (deploy-skew guard for old runtime payloads). Server-capped:
   * shared_concepts ≤10 entries, each ≤80 chars; counts coerced ≥0. */
  planned_with?: {
    systems?: number;
    shared_concepts?: string[];
    join_paths?: number;
    value_mappings?: number;
    identity_rules?: number;
    /** T1.6 additive, HONEST-NULL: count of active authority rules loaded
     * into the route seed — "loaded at planning", NOT "used in this answer".
     * ABSENT (not 0) on payloads from runtimes predating the key. */
    authority_rules?: number;
  };
  new_proposed_mappings?: number;
  /** Successful save_correlation finding writes this turn. Distinct from
   * rules_applied, which counts accepted rules read and applied. */
  correlation_writebacks?: number;
  /** Measured orchestrator model usage for this turn (E7, ADR-0053).
   * ABSENT when usage could not be read — never zeros. Orchestrator scope
   * only: system-agent model calls are not included; no dollar conversion. */
  cost?: { input_tokens: number; output_tokens: number; total_tokens: number;
           latency_ms?: number; scope: string };
  /** Results an EARLIER question in this chat returned that the runtime
   * labelled `[E<k>]` for this turn (agents/citation_ordinals.py
   * earlier_sources): label + tool name only, at most 8. ABSENT when the
   * chat had none; entries are validated by the thread store. */
  earlier_sources?: unknown[];
  [k: string]: unknown;
}

export interface WastedCall {
  reason?: string;
  fix?: string;
  tool?: string;
}

/** metadata.agent_analysis — LLM post-query review (untrusted fields). */
export interface AgentAnalysis {
  summary?: string;
  wasted_calls?: WastedCall[];
  recommendations?: unknown[];
  [k: string]: unknown;
}

/** `event: metadata` */
export interface MetadataEventData {
  streamMB?: number;
  intelligence_summary?: IntelligenceSummary;
  agent_analysis?: AgentAnalysis;
  /** metadata.provenance — additive per-claim provenance records (frozen
   * contract D1; type lives in chat/provenance.ts so parallel tasks share
   * ONE definition). ABSENT on legacy/old-runtime payloads — absence means
   * zero behavior change by construction (the store only reads it when the
   * key carries records). */
  provenance?: ProvenanceMetadata;
  [k: string]: unknown;
}

/** POST /api/feedback — thumbs payload (fire-and-forget). */
export interface FeedbackPayload {
  message_id: string;
  session_id: string;
  user_message: string;
  assistant_response: string;
  tools_used: string[];
  sentiment: 'positive' | 'negative';
  /** Optional free-text comment (the details block's SECOND POST — same
   * message_id/sentiment as the thumb click). UNTRUSTED user text: the
   * server sanitizes/validates it (1000-char cap, 400 on secret-shaped or
   * fence-token content). */
  user_comment?: string;
  /** Cited context of the ORIGINAL answer (labels the user saw) — lets a
   * negative-feedback exploration diff its re-run against what was cited.
   * The server re-caps (≤12 systems / ≤20 rules) regardless. */
  cited_systems?: string[];
  cited_rules?: { id?: string; label?: string }[];
}
