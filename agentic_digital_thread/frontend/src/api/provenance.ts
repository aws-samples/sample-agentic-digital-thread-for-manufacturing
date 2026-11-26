// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tier-3 provenance query redemption — the frontend side of frozen
 * provenance contract D5.
 *
 * `GET /api/admin/agents/traces/queries/{query_ref}?session_id=<sid>` redeems
 * the opaque `query_ref` handle a ProvenanceRecord carries for the server-held
 * executed-query text and KG artifact ids. The route is admin-gated
 * (`_check_admin` — same auth family as the trace routes) and returns
 * structured errors: 403 non-admin, 400 invalid ids, 404 unknown/expired.
 * Callers feature-detect those through `ApiError.status` and render an honest
 * "technical detail not available" line — never a payload echo.
 *
 * The response body is UNTRUSTED data: consumers must render `executed_query`
 * and `kg_artifacts` as React TEXT NODES only (zero-innerHTML sink policy;
 * no markdown, no highlighting). */

import { getJson } from './client';

/** The one note a writer may store on an entry with no query text
 * (`agents/provenance_store.py` `API_READ_NOTE`, the same text exactly,
 * pinned by `tests/unit/test_provenance_store.py`): the call ran no query and
 * read its source through the source's API. Its `statement_count` then
 * counts those reads, so the card says "N reads", never "statements". */
export const API_READ_NOTE = "read through the source's API; there is no query text";

/** The D5 200 body — seven frozen keys, plus the store's honest `note` on
 * over-bound entries (`executed_query: ''` + "query text exceeded bound")
 * or on an API-read entry (`API_READ_NOTE`).
 *
 * Wave-5 ADDITIVE fields (frozen contract F2) — ABSENT on every legacy
 * entry (orchestrator-side sidecar, pre-Wave-5 system agents): `entity`
 * (the source-side trusted-capture projection, honest-empty when not
 * derivable), `row_count` (null = captured-but-unknown; absent = legacy),
 * `statement_count` (inner SQLs joined into one executed_query, or the
 * successful reads of an API-read entry), and
 * `origin` ('system-agent' marks the source-side writer — the client-side
 * "corroborated" display state keys off it, F3). */
export interface ProvenanceQueryDetail {
  query_ref: string;
  session_id: string;
  system_id: string;
  tool_name: string;
  executed_query: string;
  kg_artifacts: string[];
  captured_at: string;
  note?: string;
  entity?: { table?: string; columns?: string[] };
  row_count?: number | null;
  statement_count?: number;
  origin?: string;
}

/** Redeem a `query_ref` for its stored technical detail (throws `ApiError`
 * on 403/404/400 — the caller renders honest absence, never retries). */
export function fetchProvenanceQuery(
  queryRef: string,
  sessionId: string,
): Promise<ProvenanceQueryDetail> {
  return getJson<ProvenanceQueryDetail>(
    `/api/admin/agents/traces/queries/${encodeURIComponent(queryRef)}` +
      `?session_id=${encodeURIComponent(sessionId)}`,
  );
}

/** The Wave-4 dataset redemption 200 body — the document persisted at cap
 * time (`agents/dataset_store.py`). `series` is UNTRUSTED wire data: render
 * every point field as React TEXT NODES only. */
export interface ProvenanceDatasetDetail {
  dataset_ref: string;
  stored_at: number;
  point_count: number;
  series: unknown[];
}

/** Redeem a claim-check `dataset_ref` (`tel-<uuid12>`) for the withheld full
 * series (Wave 4). Same admin-gated trace-route family and the same
 * structured `ApiError` mapping as `fetchProvenanceQuery`: 403 non-admin,
 * 400 invalid ids, 404 unknown/expired (incl. the store's 24 h TTL). The
 * caller fetches ON DEMAND only (an explicit "View full data" action) and
 * renders honest absence on any failure. */
export function fetchProvenanceDataset(
  datasetRef: string,
  sessionId: string,
): Promise<ProvenanceDatasetDetail> {
  return getJson<ProvenanceDatasetDetail>(
    `/api/admin/agents/traces/datasets/${encodeURIComponent(datasetRef)}` +
      `?session_id=${encodeURIComponent(sessionId)}`,
  );
}

/** One step of a Wave-6 investigation trace (frozen contract W6-F2). All
 * fields optional-defensive: the wire is UNTRUSTED and consumers must
 * render every string as a React TEXT NODE only (zero-innerHTML — the
 * reasoning/answer text is model output; tool input/output is payload).
 * `*_truncated` flags are present ONLY when true (honest); `*_bytes` carry
 * the honest full size. `duration_ms: null` = Before-stamp unmatched. */
export interface InvestigationTraceStep {
  seq?: number;
  reasoning?: string;
  reasoning_truncated?: boolean;
  tool_name?: string;
  tool_input?: string;
  input_truncated?: boolean;
  input_bytes?: number;
  tool_output?: string;
  output_truncated?: boolean;
  output_bytes?: number;
  status?: string;
  duration_ms?: number | null;
  started_at?: string;
}

/** The Wave-6 investigation-trace 200 body (frozen contract W6-F2): the
 * system agent's full path for ONE cited Mode-A call — delegated
 * sub-question, ordered tool steps with the agent's own reasoning text, and
 * the final answer returned to the orchestrator, as stored. `steps_dropped`
 * counts OLDEST-dropped steps (step cap + 64 KB ratchet) — always present,
 * render it honestly when > 0. `final_answer_names` (present only when the
 * graph names an id in the answer) maps each such id to its name as the
 * graph gave it; the card places the names after parsing the answer
 * (chat/trace-id-names.ts). */
export interface InvestigationTraceDetail {
  query_ref: string;
  session_id: string;
  system_id: string;
  origin: string;
  question: string;
  question_truncated?: boolean;
  kg_context_attached?: boolean;
  steps: InvestigationTraceStep[];
  steps_dropped: number;
  final_answer: string;
  final_answer_truncated?: boolean;
  final_answer_bytes?: number;
  final_answer_names?: Record<string, string>;
  captured_at: string;
}

/** Redeem a `query_ref` for its stored investigation trace (Wave 6 — the
 * D5-family sibling `GET /api/admin/agents/traces/investigations/…`).
 * Admin-gated; throws `ApiError` on 403/400/404 ("Unknown or expired
 * investigation trace" — covers legacy agents and the 24 h TTL). Callers
 * fetch on FIRST EXPAND only and render the honest absence line on any
 * failure — never a spinner residue, never a retry. */
export function fetchInvestigationTrace(
  queryRef: string,
  sessionId: string,
): Promise<InvestigationTraceDetail> {
  return getJson<InvestigationTraceDetail>(
    `/api/admin/agents/traces/investigations/${encodeURIComponent(queryRef)}` +
      `?session_id=${encodeURIComponent(sessionId)}`,
  );
}
