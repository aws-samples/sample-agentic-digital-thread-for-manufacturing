// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <EvidenceCard/> — the Tier-2 per-claim evidence card (provenance plan
 * Task 2.3) plus the Tier-3 "Technical detail" expander (Task 3.5). The
 * caller (Task 2.4 wires it into <AssistantTurn/>) supplies the already-bound
 * ProvenanceRecord (or null), the clicked Citation, and onClose. The card
 * itself performs NO fetches — the only network access is the Tier-3
 * expander's fetch-on-first-open of the D5 redemption route, and nothing
 * fires until the user expands it. A caller-supplied `technicalDetail` node
 * still overrides the built-in expander (Wave-2 slot contract).
 *
 * Honesty posture (frozen contracts D1/D4/D5/D6):
 * - Sparse records are the NORM — every enrichment line renders only when
 *   its data exists; nothing is fabricated.
 * - A null record (or any non-'verified'/'partial' verification, including
 *   hostile/unknown wire values) renders the card WITH an explicit
 *   "No captured evidence for this claim" banner — never a hidden card.
 * - NO SQL in Tier 2: the record structurally cannot carry query text (D6)
 *   and `query_ref` is deliberately NEVER rendered anywhere (it is a Tier-3
 *   redemption handle used only in the fetch URL, not user-facing content).
 * - Tier 3 is honest too: 403/404/fetch failure/absent `query_ref` render a
 *   plain "Technical detail not available for this claim" line — never an
 *   empty spinner; an over-bound entry surfaces the store's `note` verbatim,
 *   and so does an API-read entry (no query text to show, and why).
 * - Wave 4 raw data: when the record carries `dataset_refs` (telemetry the
 *   server capped on that call), the expander offers an explicit
 *   "View full data" affordance per handle. NOTHING auto-fetches — the
 *   redemption GET fires only on that click — and every failure renders an
 *   honest line (403 admin-gated / 404 unknown-or-expired / network).
 *   Absent refs ⇒ the expander is exactly its pre-Wave-4 self (and the
 *   trace's claim-check summary keeps its "full series withheld" wording).
 *
 * Sink policy: EVERY string below is a React TEXT NODE — zero innerHTML /
 * dangerouslySetInnerHTML, no markdown rendering (untrusted evidence text
 * and fetched query text must not route through <Md/>). Hostile strings in
 * any field — including a fetched `executed_query` — render as literal text
 * (pinned in evidence-card.test.tsx). */

import { useState } from 'react';

import { ApiError } from '../api/client';
import { useI18n } from '../lib/i18n';
import {
  API_READ_NOTE,
  fetchProvenanceDataset,
  fetchProvenanceQuery,
  type ProvenanceDatasetDetail,
  type ProvenanceQueryDetail,
} from '../api/provenance';
import { getSessionId } from '../lib/session';
import { systemName } from '../lib/system-names';
import { InvestigationTrace } from './InvestigationTrace';
import { QueryBlocks } from './QueryBlocks';
import {
  chipLabel,
  resultShapeSentence,
  ROW_EXCLUSIONS_OMITTED_LINE,
  rowExclusionViews,
  scopeSentences,
  verificationBadge,
  type CitationOutcome,
  type GraphQueryRef,
  type ProvenanceRecord,
  type VerificationBadge,
} from './provenance';
import { citationLabel } from './tool-labels';
import type { Citation } from './turn-model';
import './evidence-card.css';

/** D4 badge → `.evidence-badge-<badge>` (evidence-card.css, Sources-row
 * palette). Keyed by the VerificationBadge union so a new badge value fails
 * to compile until it has a class. */
const BADGE_CLASS: Record<VerificationBadge, string> = {
  verified: 'evidence-badge-verified',
  partial: 'evidence-badge-partial',
  unverified: 'evidence-badge-unverified',
};

/** The D5 honest-absence line (403 / 404 / fetch failure / no query_ref). */
const TIER3_UNAVAILABLE = 'Technical detail not available for this claim';

type Tier3State =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'loaded'; detail: ProvenanceQueryDetail }
  | { status: 'unavailable' };

/** Merge the record's KG rule ids with the fetched `kg_artifacts` into one
 * deduplicated text-chip list (ids only — labels already render in Tier 2). */
function tier3ArtifactIds(
  record: ProvenanceRecord | null,
  detail: ProvenanceQueryDetail,
): string[] {
  const ids: string[] = [];
  for (const rule of record?.kg_route?.rules_applied ?? []) {
    if (rule && typeof rule.id === 'string' && rule.id) ids.push(rule.id);
  }
  const fetched = Array.isArray(detail.kg_artifacts) ? detail.kg_artifacts : [];
  for (const id of fetched) {
    if (typeof id === 'string' && id) ids.push(id);
  }
  return [...new Set(ids)];
}

/** Plain-language lines for the Wave-5 F2 additive fields — [] on every
 * legacy response (absent keys ⇒ nothing renders; pre-Wave-5 byte-identical).
 * `row_count: null` renders the honest "row count not captured" (a
 * source-side capture that could not count ≠ no capture). An API-read entry
 * (`API_READ_NOTE`) counts reads, "1 read" or "N reads", never statements;
 * a SQL entry names a count only past one statement. */
function sourceCaptureLines(detail: ProvenanceQueryDetail): string[] {
  const lines: string[] = [];
  const entity = detail.entity;
  if (entity && typeof entity.table === 'string' && entity.table !== '') {
    const columns = (entity.columns ?? []).filter(
      (c): c is string => typeof c === 'string' && c !== '',
    );
    lines.push(columns.length > 0 ? `${entity.table} (${columns.join(', ')})` : entity.table);
  }
  if ('row_count' in detail) {
    lines.push(
      typeof detail.row_count === 'number'
        ? `rows: ${detail.row_count}`
        : 'row count not captured',
    );
  }
  if (typeof detail.statement_count === 'number') {
    const count = detail.statement_count;
    if (detail.note === API_READ_NOTE && count >= 1) {
      lines.push(count === 1 ? '1 read' : `${count} reads`);
    } else if (count > 1) {
      lines.push(`${count} statements executed`);
    }
  }
  return lines;
}

/** F3 corroborated display state: a successful redemption whose entry was
 * written by the SOURCE-SIDE system agent (`origin === 'system-agent'`) for
 * the SAME system the record cites. Computed client-side from the
 * redemption response — never a mutation of the streamed record, never an
 * LLM judgment; mismatch or 404 renders exactly today's card. An unverified
 * record is never corroborated: its banner says nothing was captured, and a
 * turn the integrity gate rejected stays unverified whatever was captured
 * (the sidecar merge leaves it as built). */
function isCorroborated(
  record: ProvenanceRecord | null,
  detail: ProvenanceQueryDetail,
): boolean {
  return (
    detail.origin === 'system-agent' &&
    typeof detail.system_id === 'string' &&
    detail.system_id !== '' &&
    record?.source_system === detail.system_id &&
    verificationBadge(record) !== 'unverified'
  );
}

/** Wave-5 source-side capture block (entity / rows / statement count +
 * the corroborated badge). Legacy responses render NOTHING here. All
 * strings are React TEXT NODES (zero-innerHTML). */
function SourceCapture({
  record,
  detail,
}: {
  record: ProvenanceRecord | null;
  detail: ProvenanceQueryDetail;
}) {
  const lines = sourceCaptureLines(detail);
  const corroborated = isCorroborated(record, detail);
  if (lines.length === 0 && !corroborated) return null;
  return (
    <div className="evidence-source-capture">
      {corroborated && (
        <div className="evidence-corroborated">corroborated by source-side capture</div>
      )}
      {lines.map((line, i) => (
        <div key={`sc-${i}`} className="evidence-source-capture-line">
          {line}
        </div>
      ))}
    </div>
  );
}

/** The loaded Tier-3 body: executed query as LITERAL text node(s) inside
 * <pre> block(s) via <QueryBlocks/> (untrusted data — no markdown, no
 * highlighting; copy buttons + per-statement split live in QueryBlocks.tsx),
 * or the honest not-captured/over-bound line; KG artifact ids as text
 * chips. */
function Tier3Loaded({
  record,
  detail,
}: {
  record: ProvenanceRecord | null;
  detail: ProvenanceQueryDetail;
}) {
  const query = typeof detail.executed_query === 'string' ? detail.executed_query : '';
  const artifacts = tier3ArtifactIds(record, detail);
  const { t } = useI18n();
  return (
    <>
      {query !== '' ? (
        <QueryBlocks
          text={query}
          preClassName="evidence-executed-query"
          copyLabel={t('chat.copyExecutedQuery')}
        />
      ) : (
        <div className="evidence-query-absent">
          {typeof detail.note === 'string' && detail.note
            ? detail.note
            : 'executed query not captured for this call'}
        </div>
      )}
      {/* Wave 5: source-side capture (F2 fields + F3 corroborated state) —
       * absent keys ⇒ renders nothing (legacy byte-identical). */}
      <SourceCapture record={record} detail={detail} />
      {artifacts.length > 0 && (
        <div className="evidence-kg-artifacts">
          {artifacts.map((id) => (
            <span key={id} className="evidence-kg-artifact">
              {id}
            </span>
          ))}
        </div>
      )}
    </>
  );
}

/** Honest per-status wording for a failed dataset redemption (Wave 4). */
function datasetErrorLine(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return 'Full data requires admin access';
    if (error.status === 404) return 'Full data expired or no longer available';
  }
  return 'Full data could not be loaded';
}

/** Bounded client-side display rows — the fetched series can be thousands of
 * points; rows past the cap are summarized by an honest count line. */
const MAX_DATASET_DISPLAY_ROWS = 500;

/** One point → three LITERAL text cells (untrusted wire data — string/number
 * pass through String(); anything structured renders as its JSON literal). */
function pointCells(point: unknown): [string, string, string] {
  const obj = (point && typeof point === 'object' ? point : {}) as Record<string, unknown>;
  const text = (v: unknown): string => {
    if (v == null) return '';
    if (typeof v === 'string') return v;
    if (typeof v === 'number' || typeof v === 'boolean') return String(v);
    try {
      return JSON.stringify(v);
    } catch {
      return '';
    }
  };
  return [text(obj.timestamp ?? obj.time), text(obj.value), text(obj.quality)];
}

type DatasetState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'loaded'; detail: ProvenanceDatasetDetail }
  | { status: 'error'; line: string };

/** The loaded series as a literal-text table (zero-innerHTML: every cell is
 * a React text node), display-capped with an honest count line. */
function DatasetTable({ detail }: { detail: ProvenanceDatasetDetail }) {
  const series = Array.isArray(detail.series) ? detail.series : [];
  const shown = series.slice(0, MAX_DATASET_DISPLAY_ROWS);
  return (
    <div className="evidence-dataset-table">
      <table>
        <thead>
          <tr>
            {['timestamp', 'value', 'quality'].map((h) => (
              <th key={h}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((point, i) => {
            const [ts, value, quality] = pointCells(point);
            return (
              <tr key={`pt-${i}`}>
                <td>{ts}</td>
                <td>{value}</td>
                <td>{quality}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {series.length > shown.length && (
        <div className="evidence-dataset-truncated">
          showing first {shown.length} of {series.length} points
        </div>
      )}
    </div>
  );
}

/** Wave-4 "View full data" affordance for ONE claim-check handle. NEVER
 * auto-fetches: the redemption GET fires only on the explicit click, once;
 * failures render the honest per-status line (never an empty spinner). */
function DatasetView({ datasetRef }: { datasetRef: string }) {
  const [state, setState] = useState<DatasetState>({ status: 'idle' });

  const onView = () => {
    if (state.status !== 'idle' && state.status !== 'error') return;
    setState({ status: 'loading' });
    fetchProvenanceDataset(datasetRef, getSessionId()).then(
      (detail) => setState({ status: 'loaded', detail }),
      (error: unknown) => setState({ status: 'error', line: datasetErrorLine(error) }),
    );
  };

  return (
    <div className="evidence-dataset">
      {(state.status === 'idle' || state.status === 'error') && (
        <button type="button" className="evidence-dataset-view evidence-link-btn" onClick={onView}>
          View full data
        </button>
      )}
      {state.status === 'loading' && (
        <div className="evidence-dataset-loading">Loading full data…</div>
      )}
      {state.status === 'error' && (
        <div className="evidence-dataset-error">
          {state.line}
        </div>
      )}
      {state.status === 'loaded' && <DatasetTable detail={state.detail} />}
    </div>
  );
}

/** Validated handles from the record — string, `tel-` prefixed (defensive
 * mirror of the server bound; hostile non-string entries drop out). */
function datasetRefs(record: ProvenanceRecord | null): string[] {
  return (record?.dataset_refs ?? []).filter(
    (ref): ref is string => typeof ref === 'string' && ref.startsWith('tel-'),
  );
}

/** Validated top-level `graph_queries` rows — non-empty string `query_ref`
 * plus a string `tool_name` (hostile/short-shaped entries drop out). */
function routingQueryRows(rows: GraphQueryRef[] | undefined): GraphQueryRef[] {
  return (rows ?? []).filter(
    (row): row is GraphQueryRef =>
      !!row &&
      typeof row.query_ref === 'string' &&
      row.query_ref !== '' &&
      typeof row.tool_name === 'string',
  );
}

type RoutingQueryState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'loaded'; detail: ProvenanceQueryDetail }
  | { status: 'unavailable' };

/** One uncited routing-query handle: an explicit fetch-on-click of the SAME
 * D5 redemption route the expander uses. NEVER auto-fetches; the executed
 * query renders as a LITERAL text node (zero-innerHTML); every failure
 * (403 non-admin / 404 unknown-or-expired / network) collapses to the same
 * honest Tier-3 line — never an empty spinner. */
function RoutingQueryView({ row }: { row: GraphQueryRef }) {
  const [state, setState] = useState<RoutingQueryState>({ status: 'idle' });

  const onView = () => {
    if (state.status !== 'idle') return;
    setState({ status: 'loading' });
    fetchProvenanceQuery(row.query_ref, getSessionId()).then(
      (detail) => setState({ status: 'loaded', detail }),
      () => setState({ status: 'unavailable' }),
    );
  };

  return (
    <div className="evidence-routing-query">
      {state.status === 'idle' && (
        <button
          type="button"
          className="evidence-routing-query-view evidence-link-btn"
          onClick={onView}
        >
          {row.tool_name || 'graph query'}
        </button>
      )}
      {state.status === 'loading' && (
        <div className="evidence-routing-query-loading">Loading technical detail…</div>
      )}
      {state.status === 'unavailable' && (
        <div className="evidence-routing-query-unavailable">
          {TIER3_UNAVAILABLE}
        </div>
      )}
      {state.status === 'loaded' && <RoutingQueryLoaded detail={state.detail} />}
    </div>
  );
}

/** The loaded routing-query body: literal-text <pre> block(s) via
 * <QueryBlocks/> (untrusted data — no markdown, no highlighting; copy
 * buttons + per-statement split) or the honest not-captured/over-bound
 * line. */
function RoutingQueryLoaded({ detail }: { detail: ProvenanceQueryDetail }) {
  const { t } = useI18n();
  const query = typeof detail.executed_query === 'string' ? detail.executed_query : '';
  if (query === '') {
    return (
      <div className="evidence-query-absent">
        {typeof detail.note === 'string' && detail.note
          ? detail.note
          : 'executed query not captured for this call'}
      </div>
    );
  }
  return (
    <QueryBlocks
      text={query}
      preClassName="evidence-routing-query-text"
      copyLabel={t('chat.copyRoutingQuery')}
    />
  );
}

/** The "Routing queries" list — the turn's additive top-level
 * `graph_queries` rows (Mode A's uncited-but-redeemable bare explore_graph).
 * One explicit fetch-on-click button per validated row; an absent/empty key
 * renders NOTHING (the pre-fix expander verbatim — legacy safe). */
function RoutingQueries({ rows }: { rows: GraphQueryRef[] | undefined }) {
  const valid = routingQueryRows(rows);
  if (valid.length === 0) return null;
  return (
    <div className="evidence-routing-queries">
      <div className="evidence-section-label">Routing queries</div>
      {valid.map((row) => (
        <RoutingQueryView key={row.query_ref} row={row} />
      ))}
    </div>
  );
}

/** "Rows left out": the source owner's row exclusions this call's queries
 * ran with, in plain words, from the record alone (Tier 2 — no fetch, shown
 * to every signed-in user). Nothing renders when the record carries none
 * (`rowExclusionViews` drops a rule it cannot state exactly, whole). */
function RowExclusions({ record }: { record: ProvenanceRecord | null }) {
  const views = rowExclusionViews(record);
  const omitted = record?.row_exclusions_omitted === true;
  if (views.length === 0 && !omitted) return null;
  return (
    <div className="evidence-row-exclusions">
      <div className="evidence-section-label">Rows left out</div>
      {views.map((view, i) => (
        <div key={`rx-${i}`} className="evidence-row-exclusion">
          <div className="evidence-row-exclusion-title">{view.title}</div>
          <div className="evidence-row-exclusion-intro">{view.intro}</div>
          <ul className="evidence-row-exclusion-conditions">
            {view.conditions.map((line, j) => (
              <li key={`rx-${i}-${j}`}>{line}</li>
            ))}
          </ul>
          {view.provenance !== '' && (
            <div className="evidence-row-exclusion-provenance">{view.provenance}</div>
          )}
        </div>
      ))}
      {omitted && (
        <div className="evidence-row-exclusion-omitted">{ROW_EXCLUSIONS_OMITTED_LINE}</div>
      )}
    </div>
  );
}

interface Tier3Props {
  record: ProvenanceRecord | null;
  graphQueries?: GraphQueryRef[];
}

/** The built-in Tier-3 expander (Task 3.5): a <details> that redeems the
 * record's `query_ref` via the D5 route on FIRST open only. No query_ref ⇒
 * the honest line without any fetch; every failure path (403 non-admin, 404
 * unknown/expired, network) collapses to the same honest line. */
function Tier3TechnicalDetail({ record, graphQueries }: Tier3Props) {
  const [state, setState] = useState<Tier3State>({ status: 'idle' });
  const queryRef = record?.query_ref ?? '';

  const onToggle = (e: React.SyntheticEvent<HTMLDetailsElement>) => {
    // Only this details' own toggle, and only the first open (fetch once).
    if (e.target !== e.currentTarget) return;
    if (!e.currentTarget.open || state.status !== 'idle') return;
    if (!queryRef) {
      setState({ status: 'unavailable' });
      return;
    }
    setState({ status: 'loading' });
    fetchProvenanceQuery(queryRef, getSessionId()).then(
      (detail) => setState({ status: 'loaded', detail }),
      () => setState({ status: 'unavailable' }),
    );
  };

  return (
    <details className="evidence-tier3" onToggle={onToggle}>
      <summary>Technical detail</summary>
      {state.status === 'loading' && (
        <div className="evidence-tier3-loading">Loading technical detail…</div>
      )}
      {state.status === 'unavailable' && (
        <div className="evidence-tier3-unavailable">
          {TIER3_UNAVAILABLE}
        </div>
      )}
      {state.status === 'loaded' && <Tier3Loaded record={record} detail={state.detail} />}
      {/* Wave 4: one explicit "View full data" affordance per claim-check
       * handle on the record — independent of the query_ref fetch state
       * above (a capped telemetry call may carry refs with no query text).
       * Absent refs ⇒ nothing renders (the pre-Wave-4 expander verbatim). */}
      {datasetRefs(record).map((ref) => (
        <DatasetView key={ref} datasetRef={ref} />
      ))}
      {/* Additive graph_queries — absent key ⇒ renders nothing (legacy). */}
      <RoutingQueries rows={graphQueries} />
    </details>
  );
}

export interface EvidenceCardProps {
  /** The bound provenance record — null when nothing binds (D2 fallback
   * exhausted); the card still opens with the fallback label + banner. */
  record: ProvenanceRecord | null;
  /** The clicked Sources-row citation (turn-model `_queriedSystems` entry). */
  citation: Citation;
  onClose: () => void;
  /** Optional OVERRIDE of the built-in Tier-3 expander (the Wave-2 slot
   * contract, kept for callers/tests that supply their own node). Absent ⇒
   * the built-in D5 fetch-on-expand <Tier3TechnicalDetail/> renders. */
  technicalDetail?: React.ReactNode;
  /** Additive top-level `graph_queries` rows for the TURN (turn-model
   * `graphQueries`) — the uncited-but-redeemable routing queries. Absent/
   * empty ⇒ the Tier-3 expander is exactly its pre-fix self. */
  graphQueries?: GraphQueryRef[];
  /** The cited call's own outcome (`citationOutcome`), the one its Sources
   * chip shows. 'empty' with rows on the record makes the result-shape line
   * say the source reported no data while its captured evidence holds those
   * rows. Absent ⇒ the plain result-shape line. */
  outcome?: CitationOutcome | null;
}

export function EvidenceCard({
  record,
  citation,
  onClose,
  technicalDetail,
  graphQueries,
  outcome = null,
}: EvidenceCardProps) {
  const badge = verificationBadge(record);
  const scope = scopeSentences(record);
  const shape = resultShapeSentence(record, outcome);
  const table = record?.entity?.table ?? '';
  const columns = (record?.entity?.columns ?? []).filter((c) => typeof c === 'string' && c);
  const rules = (record?.kg_route?.rules_applied ?? []).filter((r) => r && r.label);
  const mappings = (record?.kg_route?.value_mappings ?? []).filter(
    (m) => typeof m === 'string' && m,
  );

  return (
    <div
      className="evidence-card"
      role="dialog"
      aria-label={`Evidence for citation ${citation.num}`}
      data-cite-num={citation.num}
    >
      {/* ── header: [n] chip label · verification badge · close ─────────── */}
      <div className="evidence-card-header">
        <span className="evidence-card-title">
          [{citation.num}] {chipLabel(record, citationLabel(citation), systemName)}
        </span>
        <span className={`evidence-badge ${BADGE_CLASS[badge]}`}>{badge}</span>
        <button
          type="button"
          className="evidence-card-close"
          aria-label="Close evidence card"
          onClick={onClose}
        >
          ×
        </button>
      </div>

      {/* ── D4 honest absence: explicit banner, never a hidden card ─────── */}
      {badge === 'unverified' && (
        <div className="evidence-unverified-banner">
          No captured evidence for this claim
        </div>
      )}

      {/* ── entity (when captured) ───────────────────────────────────────── */}
      {table !== '' && (
        <div className="evidence-entity">
          <span className="evidence-entity-label">Entity: </span>
          {columns.length > 0 ? `${table} (${columns.join(', ')})` : table}
        </div>
      )}

      {/* ── plain-language scope (T2.1 helpers; includes "queried at …") ── */}
      {scope.length > 0 && (
        <ul className="evidence-scope">
          {scope.map((s, i) => (
            <li key={`scope-${i}`}>{s}</li>
          ))}
        </ul>
      )}

      {/* ── result-shape honesty line ('' ⇒ omitted, honest absence) ────── */}
      {shape !== '' && (
        <div className="evidence-shape">
          {shape}
        </div>
      )}

      {/* ── knowledge-graph route one-liners ─────────────────────────────── */}
      {(rules.length > 0 || mappings.length > 0) && (
        <div className="evidence-kg-route">
          <div className="evidence-section-label">Knowledge-graph route</div>
          <ul className="evidence-kg-route-list">
            {rules.map((r, i) => (
              <li key={`rule-${i}`}>{r.label}</li>
            ))}
            {mappings.map((m, i) => (
              <li key={`vm-${i}`}>translated {m} between systems</li>
            ))}
          </ul>
        </div>
      )}

      {/* ── the source owner's row exclusions this call ran with ───────── */}
      <RowExclusions record={record} />

      {/* ── Tier-3 "Technical detail" (Task 3.5): caller override wins,
       * otherwise the built-in D5 fetch-on-expand expander. Keyed by the
       * citation num COMPOSED with the query_ref: two successive
       * record-less citations share no query_ref ('' for both), so the num
       * keeps their expander state/fetch from bleeding across; the
       * query_ref part still resets state when the same citation re-binds
       * to a different record. ─────────────────────────────────────────── */}
      <div className="evidence-technical-detail">
        {technicalDetail != null ? (
          technicalDetail
        ) : (
          <Tier3TechnicalDetail
            key={`${citation.num}:${record?.query_ref ?? ''}`}
            record={record}
            graphQueries={graphQueries}
          />
        )}
      </div>

      {/* ── Wave 6: collapsible "Investigation trace" (W6-F6) — the LAST
       * child of the card. Fetch-on-first-expand only; no query_ref (legacy
       * record / null record) renders the honest absence line with zero
       * fetches. Same key composition as the Tier-3 expander so state never
       * bleeds across citations or re-bound records. ──────────────────── */}
      <InvestigationTrace
        key={`${citation.num}:${record?.query_ref ?? ''}`}
        queryRef={record?.query_ref ?? ''}
      />
    </div>
  );
}
