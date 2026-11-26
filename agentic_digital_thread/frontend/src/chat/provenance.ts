// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Per-claim provenance — the frontend mirror of the server wire contract
 * (agents/provenance.py, frozen contract D1) plus pure presentation helpers
 * for the Tier-1 chips and Tier-2 evidence card.
 *
 * PURE module: no DOM, no JSX, no store imports (parallel-safe by design —
 * sibling tasks import the ProvenanceRecord type from HERE, not from
 * api/types/chat.ts). Every function returns plain strings/objects that the
 * consumers render as React TEXT NODES only (zero-innerHTML sink policy);
 * nothing here parses, interprets, or re-emits markup.
 *
 * Sparse records are the NORM in the default report-only deployment (D1):
 * every field except citation_num, tool_id, source_system, captured_at,
 * verification may be empty/absent — helpers render absence honestly
 * (fallback chip label, 'unverified' badge), never fabricate. */

import { citedEarlierNums } from './citations';

/** One `{id, label}` KG rule row (server `_kg_route`, labels pre-bounded). */
export interface ProvenanceRuleApplied {
  id: string;
  label: string;
}

/** One D1 wire record (`metadata.provenance.records[]`).
 *
 * Required-by-contract keys (`citation_num`, `tool_id`, `source_system`,
 * `captured_at`, `verification`) are non-optional; ALL enrichment fields are
 * optional AND tolerate the server's honest-empty spellings ('' / [] / null).
 * `verification` is typed open (string) — the badge helper normalizes
 * unknown values to 'unverified' rather than trusting the wire. */
export interface ProvenanceRecord {
  citation_num: number;
  tool_id: string;
  source_system: string;
  captured_at: string;
  verification: string;
  tool_name?: string;
  entity?: { table?: string; columns?: string[] };
  scope?: {
    filters?: string[];
    window?: { start?: string; end?: string } | null;
  };
  query_ref?: string;
  result_shape?: {
    rows?: number | null;
    truncated?: boolean;
    partial_reason?: string;
  };
  kg_route?: {
    rules_applied?: ProvenanceRuleApplied[];
    value_mappings?: string[];
  };
  /** Wave 4 — claim-check handles (`tel-<uuid12>`) for telemetry the server
   * capped on this call; ABSENT on every pre-Wave-4 record and whenever no
   * series was withheld. Handles only, never data: the Tier-3 expander's
   * "View full data" affordance redeems them on demand. */
  dataset_refs?: string[];
  /** The source owner's row exclusions this call's queries ran WITH, each
   * exactly as it was applied (server `provenance.normalize_row_exclusions`,
   * bounded). ABSENT when no rule was applied, or when the frame had no room
   * for it; never present for a rule that was not applied. */
  row_exclusions?: ProvenanceRowExclusion[];
  /** True when a rule this call's queries ran with was left off the record
   * for size (server `provenance._add_row_exclusions`): the card says a rule
   * left rows out without its details. ABSENT otherwise. */
  row_exclusions_omitted?: boolean;
}

/** One condition of a row exclusion, as the server stores it: a column, an
 * op, and values only (and always) for `in` / `not_in`. */
export interface ProvenanceRowExclusionCondition {
  column: string;
  op: string;
  values?: string[];
}

/** One applied row exclusion: the table, the owner's label and conditions
 * (rows where ALL hold were left out), and when it was set and from what
 * source ('' when unknown; `source_shortened` when the server cut the source
 * to fit). Who set it is never on the wire: an administrator did. */
export interface ProvenanceRowExclusion {
  table?: string;
  label: string;
  conditions: ProvenanceRowExclusionCondition[];
  at?: string;
  source?: string;
  source_shortened?: boolean;
}

/** The card's line for a record whose rule was left off for size. */
export const ROW_EXCLUSIONS_OMITTED_LINE =
  "A source owner's rule left rows out of this result; its details didn't fit on this card.";

/** One applied rule in plain words, for the card's "Rows left out" section. */
export interface RowExclusionView {
  title: string;
  intro: string;
  conditions: string[];
  /** "Set by an administrator[ on <at>]. [Source: <source>]". */
  provenance: string;
}

function nonEmpty(value: unknown): value is string {
  return typeof value === 'string' && value !== '';
}

/** One condition in plain words, or null when it is not exactly one of the
 * stored shapes. The rule reads a column as COALESCE(column, ''), so an
 * empty or null value counts as empty, and a `not_in` also holds for one. */
function conditionWords(raw: unknown): string | null {
  const c = raw as ProvenanceRowExclusionCondition | null;
  if (!c || typeof c !== 'object' || !nonEmpty(c.column)) return null;
  const values = c.values === undefined ? [] : c.values;
  if (!Array.isArray(values) || !values.every((v) => nonEmpty(v) && !v.includes('"'))) {
    return null;
  }
  const listed = c.op === 'in' || c.op === 'not_in';
  if (listed !== values.length > 0) return null;
  // Each literal in double quotes (values never hold one), so a comma inside
  // a value, or a value that reads like the null wording, stays one value.
  const list = values.map((v) => `"${v}"`).join(', ');
  switch (c.op) {
    case 'empty':
      return `${c.column} is empty or null`;
    case 'not_empty':
      return `${c.column} is not empty or null`;
    case 'in':
      return values.length === 1 ? `${c.column} is ${list}` : `${c.column} is one of ${list}`;
    case 'not_in':
      return values.length === 1
        ? `${c.column} is not ${list}, or is empty or null`
        : `${c.column} is none of ${list}, or is empty or null`;
    default:
      return null;
  }
}

/** Who set the rule, when and from what source. Only an administrator can
 * set one, and the sign-in identity is not shown (any actor a record might
 * carry is ignored); a shortened source ends in an ellipsis and says so. */
function provenanceWords(rule: ProvenanceRowExclusion): string {
  const parts = [
    nonEmpty(rule.at) ? `Set by an administrator on ${rule.at}.` : 'Set by an administrator.',
  ];
  const shortened = rule.source_shortened === true;
  if (nonEmpty(rule.source) || shortened) {
    const source = nonEmpty(rule.source) ? rule.source : '';
    parts.push(`Source: ${source}${shortened ? '… (source shortened)' : ''}`);
  }
  return parts.join(' ');
}

/** One applied rule in plain words, WHOLE or null: a rule with any condition
 * the card cannot state exactly is never shown in part, since part of a rule
 * would misstate which rows were left out. */
function rowExclusionView(raw: unknown): RowExclusionView | null {
  const rule = raw as ProvenanceRowExclusion | null;
  if (!rule || typeof rule !== 'object' || !nonEmpty(rule.label)) return null;
  if (!Array.isArray(rule.conditions) || rule.conditions.length === 0) return null;
  const conditions = rule.conditions.map(conditionWords);
  if (conditions.some((c) => c === null)) return null;
  return {
    title: nonEmpty(rule.table) ? `${rule.label} (${rule.table})` : rule.label,
    intro:
      conditions.length === 1
        ? 'Rows where this holds were left out:'
        : 'Rows where all of these hold were left out:',
    conditions: conditions as string[],
    provenance: provenanceWords(rule),
  };
}

/** The record's applied row exclusions in plain words (Tier 2, every
 * signed-in user): [] when it carries none, and a rule the card cannot state
 * exactly is left out whole. Strings only — rendered as React text nodes. */
export function rowExclusionViews(record: ProvenanceRecord | null): RowExclusionView[] {
  const rules = record?.row_exclusions;
  if (!Array.isArray(rules)) return [];
  return rules.map(rowExclusionView).filter((v): v is RowExclusionView => v !== null);
}

/** One additive top-level `graph_queries` row (server
 * `uncited_query_refs`, live cloud-path fix 2026-08-04): an
 * UNCITED-but-redeemable Tier-3 handle — in default Mode A the bare
 * `explore_graph` routing query is never cited (D2), so its stored sidecar
 * entry rides this key. Refs + tool names ONLY; query text stays
 * server-side until the D5 redemption fetch (D6). */
export interface GraphQueryRef {
  query_ref: string;
  tool_name: string;
}

/** The full additive metadata payload (`{"provenance": {...}}`, D1). */
export interface ProvenanceMetadata {
  records: ProvenanceRecord[];
  truncated?: boolean;
  /** Additive `graph_queries` rows — ABSENT on every pre-fix payload and
   * whenever no uncited query-bearing call ran (legacy byte-identical). */
  graph_queries?: GraphQueryRef[];
}

/** Normalized verification state for chips/cards (D4 vocabulary). */
export type VerificationBadge = 'verified' | 'partial' | 'unverified';

/** The D2 counting predicate — the ONE frontend home. A tool call earns a
 * citation exactly when its tool name fails the EXACT string test
 * `!== 'explore_graph'` (deliberately NOT isExploreGraphTool: a
 * gateway-prefixed `x___explore_graph` IS numbered — frozen contract D2;
 * the server mirror is agents/provenance.py `is_client_cited`). Used by
 * BOTH the thread-store citation numbering and `citationToolId` below —
 * never re-spell the comparison inline. */
export function isCitedTool(tool: string): boolean {
  return tool !== 'explore_graph';
}

/** Minimal citation shape for binding (structurally satisfied by the
 * turn-model `Citation`; `toolId` is supplied by callers that hold the
 * pill/trace toolId — the Citation itself does not carry one today). */
export interface CitationRef {
  num: number;
  toolId?: string | null;
}

/** Minimal pill shape for ordinal counting — structurally satisfied by the
 * turn-model `ToolPill` (no store import; this module stays pure).
 * `ambiguousWireId`: the server sent this call's tool_id more than once in
 * the turn, so a tool_id match cannot say WHICH call a record describes —
 * such a pill never primary-binds (fail closed to the citation_num
 * fallback, which the server assigns per occurrence). */
export interface CitedPill {
  tool: string;
  toolId: string;
  ambiguousWireId?: boolean;
}

/** D2 dual binding support: citation num → the owning pill's toolId. The
 * citation is the Nth pill whose tool passes the ONE counting predicate
 * (isCitedTool above — the exact `!== 'explore_graph'` string test
 * thread-store numbering uses, per frozen contract D2; the server mirror
 * is agents/provenance.py `citation_ordinals`). Null when no pill matches
 * (rehydrated history, synthetic turns) — recordForCitation then falls
 * back to citation_num. */
export function citationToolId(pills: Iterable<CitedPill>, num: number): string | null {
  let n = 0;
  for (const p of pills) {
    if (isCitedTool(p.tool)) {
      n++;
      if (n === num) return p.ambiguousWireId ? null : p.toolId;
    }
  }
  return null;
}

/** A cited call's outcome worth saying on its Sources chip. */
export type CitationOutcome = 'failed' | 'timeout' | 'empty';

/** Minimal trace shape (structurally satisfied by the turn-model ToolTrace). */
export interface OutcomeTrace {
  outcome?: string;
}

/** The outcome of the call behind citation `num`, when it gave no answer:
 * its OWN trace outcome (classified once at tool_result by
 * lib/arch-store.ts classifyToolResultDetailed), bound through
 * `citationToolId` so an ambiguous wire id fails closed to null. Success,
 * partial, filtered, a running call and an unbound number are null: the
 * chip stays plain. Test event 32 (2026-10-15): Sources listed three failed
 * calls like answered ones; they are MARKED, never dropped, because
 * dropping one would renumber the inline citations. */
export function citationOutcome(
  pills: Iterable<CitedPill>,
  traces: ReadonlyMap<string, OutcomeTrace>,
  num: number,
): CitationOutcome | null {
  const toolId = citationToolId(pills, num);
  const outcome = toolId ? traces.get(toolId)?.outcome : undefined;
  return outcome === 'failed' || outcome === 'timeout' || outcome === 'empty' ? outcome : null;
}

/** The earlier citations (`earlier: true`) whose `[E<k>]` label the answer
 * text actually cites, alone or as a member of a group the inline links
 * expand (`[E1, E2]`, citations.ts CITATION_GROUP_RE), in label order: the
 * footer shows a chip only for a result the answer used. `[E<k>]` is the
 * fixed label the runtime gives a result an EARLIER question in this chat
 * returned (agents/citation_ordinals.py earlier_stamp), one or two digits;
 * validated labels are the only ones counted. */
export function citedEarlierCitations<C extends { num: number; earlier?: boolean }>(
  citations: readonly C[],
  answerMd: string,
): C[] {
  const cited = citedEarlierNums(citations, answerMd || '');
  return citations
    .filter((c) => c.earlier === true && cited.has(c.num))
    .sort((a, b) => a.num - b.num);
}

/** D1 `partial_reason` vocabulary → plain-language cap wording
 * (server source: response_integrity_query.py bounded-capture reasons). */
const PARTIAL_REASON_WORDING: Record<string, string> = {
  row_limit: 'result was capped at the row limit',
  byte_limit: 'result was capped at the size limit',
  cell_limit: 'result was capped at the cell limit',
  column_limit: 'result was capped at the column limit',
};

/** Compact a scope window to a short human string; '' when absent. */
function compactWindow(win: { start?: string; end?: string } | null | undefined): string {
  if (!win) return '';
  // Date part only when the bound looks like an ISO timestamp.
  const day = (s: string | undefined): string => {
    const v = typeof s === 'string' ? s.trim() : '';
    return v.includes('T') ? v.split('T')[0] : v;
  };
  const start = day(win.start);
  const end = day(win.end);
  if (start && end) return start === end ? start : `${start} to ${end}`;
  if (start) return `since ${start}`;
  if (end) return `until ${end}`;
  return '';
}

/** Bind a citation to its provenance record (D2): `tool_id` match is
 * PRIMARY (against the pill/trace toolId when the caller has one);
 * `citation_num` is the fallback. Null when nothing binds — the chip then
 * keeps today's label and the card renders the unverified banner. */
export function recordForCitation(
  records: ProvenanceRecord[] | null | undefined,
  citation: CitationRef,
): ProvenanceRecord | null {
  if (!records || records.length === 0) return null;
  const toolId = citation.toolId;
  if (typeof toolId === 'string' && toolId) {
    const byId = records.find((r) => r.tool_id === toolId);
    if (byId) return byId;
  }
  return records.find((r) => r.citation_num === citation.num) ?? null;
}

/** The ONE spelling of the full D2 dual-binding recipe (toolId resolved
 * from the turn's pills, then `recordForCitation`) — used by the chips
 * map, the EvidenceCard mount, and thread-clicks' tryOpenEvidence; never
 * re-compose it inline. */
export function boundRecordFor(
  records: ProvenanceRecord[] | null | undefined,
  pills: Iterable<CitedPill>,
  num: number,
): ProvenanceRecord | null {
  return recordForCitation(records, { num, toolId: citationToolId(pills, num) });
}

/** Tier-1 chip label: `source_system` plus `entity.table` and a compact
 * window when present. A record with NO enrichment beyond what today's
 * label already conveys (no table, no window) returns `fallback` VERBATIM
 * (D1: sparse records render honestly with the fallback chip label).
 * `nameOf` turns the record's `source_system` (a system id on the wire)
 * into the name a person reads; callers pass the shared resolver
 * (lib/system-names.ts `systemName`). The default keeps the id, so this
 * module stays pure (no store import). */
export function chipLabel(
  record: ProvenanceRecord | null,
  fallback: string,
  nameOf: (systemId: string) => string = (id) => id,
): string {
  if (!record) return fallback;
  const table = record.entity?.table ?? '';
  const window = compactWindow(record.scope?.window);
  if (!table && !window) return fallback;
  const parts = [(record.source_system && nameOf(record.source_system)) || fallback];
  if (table) parts.push(table);
  if (window) parts.push(window);
  return parts.join(' · ');
}

/** Plain-language Tier-2 scope sentences: one "rows where …" line per
 * server-bounded filter string, a "data observed …" line for the window,
 * and a "queried at …" line for the capture timestamp. NO SQL by
 * construction — the record structurally cannot carry it (D6), and these
 * strings render as React text nodes only. */
export function scopeSentences(record: ProvenanceRecord | null): string[] {
  if (!record) return [];
  const sentences: string[] = [];
  for (const filter of record.scope?.filters ?? []) {
    if (typeof filter === 'string' && filter) {
      sentences.push(`rows where ${filter}`);
    }
  }
  const window = compactWindow(record.scope?.window);
  if (window) sentences.push(`data observed ${window}`);
  if (record.captured_at) sentences.push(`queried at ${record.captured_at}`);
  return sentences;
}

/** "N rows" (or "1 row", or the capped "the first N rows of a larger
 * result") for the result-shape line. */
function countedRows(rows: number, capped: boolean): string {
  if (capped) return `the first ${rows} rows of a larger result`;
  return rows === 1 ? '1 row' : `${rows} rows`;
}

/** Result-shape honesty line (D1 `partial_reason` vocabulary). Capped
 * results say so explicitly ("first N rows of a larger result — …");
 * un-capped results state the row count; nothing captured ⇒ '' (the card
 * simply omits the line: honest absence, not fabrication).
 *
 * `outcome` is the cited call's own outcome (`citationOutcome`). The record
 * counts the rows of the call's evidence, which can come from a query other
 * than the lookup the call was asked for. When the call is 'empty' (it
 * reported no data) but the record counts rows, the line states both facts
 * the client holds, without guessing why they differ. */
export function resultShapeSentence(
  record: ProvenanceRecord | null,
  outcome: CitationOutcome | null = null,
): string {
  const shape = record?.result_shape;
  if (!shape) return '';
  const rows = typeof shape.rows === 'number' ? shape.rows : null;
  const reason = shape.partial_reason ?? '';
  const capped = shape.truncated === true || reason !== '';
  if (outcome === 'empty' && rows !== null && rows > 0) {
    return `This source reported no data, but its captured evidence holds ${countedRows(rows, capped)}.`;
  }
  if (capped) {
    const wording = PARTIAL_REASON_WORDING[reason] ?? 'result was capped';
    return rows !== null
      ? `first ${rows} rows of a larger result — ${wording}`
      : `partial result — ${wording}`;
  }
  if (rows !== null) return rows === 1 ? '1 row returned' : `${rows} rows returned`;
  return '';
}

/** Normalize a record's verification for display (D4): only the exact
 * 'verified'/'partial' wire values upgrade the badge; a null record or any
 * other value — including hostile/unknown strings — is 'unverified'
 * (honest absence; never hidden, never trusted). */
export function verificationBadge(
  record: ProvenanceRecord | null,
): VerificationBadge {
  const v = record?.verification;
  return v === 'verified' || v === 'partial' ? v : 'unverified';
}
