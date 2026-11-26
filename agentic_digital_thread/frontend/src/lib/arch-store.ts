// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Investigation-graph store — full implementation (Task 2.3).
 *
 * DOM-free store for the event-accumulation half of the investigation graph:
 * `handleArchEvent` (plus the shared chat globals it relies on:
 * getSystemLabel / the node-id + question counters). The chat stream bridge
 * (Task 2.1) forwards `/api/ask` SSE events here; the architecture view
 * (Task 2.3) subscribes and renders the cytoscape graph / trace / HUD from the
 * accumulated state.
 *
 * Semantics preserved from the old file:
 * - session-persistent: the graph accumulates across chat turns and survives
 *   view switches within a Conversation Session;
 * - reset by `startNewSession` (→ resetArchitectureState);
 * - one dedup'd node per system (I1) with an "×N" repeat badge (I2/I3);
 * - per-request blue "qinput" nodes: system → question → result (C);
 * - per-node request/result aggregation driving the click-to-open modal (I5);
 * - transient self-recovered query errors are NOT surfaced (isError gate);
 * - `intelligence_summary` renders into `#response-area` when present.
 *
 * The Wave-1 stub public interface (addNodes / addEdge / reset / subscribe /
 * getState + the `archStore` singleton and `createArchStore`) is preserved —
 * everything below is additive. */
import type { IntelligenceSummary } from '../api/types/chat';

import { RULES_APPLIED_TIP } from './metric-copy';

// Side-effect import: registers mapToolToSystemId / describeToolInput /
// describeToolResult on globalThis (the file is a verbatim CJS-compatible IIFE
// with no ES exports — same pattern as the old <script> tag load order).
import './architecture-util.js';
import { kgQuestionText } from './kg-label';
import { subscribeSystemNames, systemName } from './system-names';

const _g = globalThis as Record<string, unknown>;

function _mapToolToSystemId(toolName: unknown): string | null {
  const fn = _g.mapToolToSystemId;
  return typeof fn === 'function' ? (fn as (t: unknown) => string | null)(toolName) : null;
}

function _describeToolInput(tool: string, input: unknown): string {
  const fn = _g.describeToolInput;
  try {
    return typeof fn === 'function' ? String((fn as (t: string, i: unknown) => string)(tool, input) || '') : '';
  } catch {
    return '';
  }
}

function _describeToolResult(tool: string, summary: string, requestText: string, resultPayload?: unknown): string {
  const fn = _g.describeToolResult;
  try {
    return typeof fn === 'function'
      ? String((fn as (t: string, s: string, r: string, p?: unknown) => string)(tool, summary, requestText, resultPayload) || '')
      : '';
  } catch {
    return '';
  }
}

function _readsPastTable(tool: string, summary: string): boolean {
  const fn = _g.readsPastTable;
  try {
    return typeof fn === 'function' ? Boolean((fn as (t: string, s: string) => boolean)(tool, summary)) : false;
  } catch {
    return false;
  }
}

function _plainSummaryText(text: string): string {
  const fn = _g.plainSummaryText;
  try {
    return typeof fn === 'function' ? String((fn as (t: string) => string)(text) || '') : '';
  } catch {
    return '';
  }
}

function toolHalf(tool: string): string {
  return tool.includes('___') ? tool.split('___')[1] : tool;
}

function toolStartNarrative(tool: string, inputEnglish: string, systemLabel: string): string {
  if (!inputEnglish) return '';
  const half = toolHalf(tool);
  if (half.startsWith('save_')) return inputEnglish;
  if (half === 'explore_graph') return `Querying the Knowledge Graph: ${inputEnglish}`;
  if (half.startsWith('query_') || tool.includes('___')) {
    // The system's display name, the same label its graph node, HUD row and
    // trace step show (catalog name, else the id fallback); never the raw
    // gateway target id.
    const system = systemLabel.replace(/\n/g, ' ') || (tool.includes('___') ? tool.split('___')[0] : '');
    return `Asking ${system}: “${inputEnglish}”`;
  }
  return inputEnglish;
}

/** UNCAPPED plain-text form of a tool_start input (trace de-truncation, D1).
 * Deliberately duplicates the priority-key extraction of
 * architecture-util.js `_pickInputValue` — that file is a plain IIFE shared
 * with the python unit lane (tests/unit/test_architecture_util.py) and keeps
 * a frozen export surface, so the list lives here too; parity is pinned by a
 * unit test against the util's exported `describeToolInput`. Whitespace-collapsed, NO length cap — the
 * TraceEntries SHOW MORE clamp is the only length control for trace rows. */
export function fullToolInputText(input: unknown): string {
  const collapse = (v: unknown): string => String(v).replace(/\s+/g, ' ').trim();
  if (input == null) return '';
  if (typeof input !== 'object') return collapse(input);
  if (Array.isArray(input)) return input.length ? collapse(JSON.stringify(input)) : '';
  const obj = input as Record<string, unknown>;
  const PRIORITY = ['question', 'sub_question', 'subquestion', 'query', 'sql',
    'statement', 'filter', 'expression', 'cypher', 'gremlin', 'prompt', 'text', 'input'];
  const lowerMap: Record<string, string> = {};
  for (const k of Object.keys(obj)) lowerMap[k.toLowerCase()] = k;
  for (const key of PRIORITY) {
    const actual = lowerMap[key];
    if (actual != null && obj[actual] != null && String(obj[actual]).trim()) return collapse(obj[actual]);
  }
  for (const k of Object.keys(obj)) {
    const v = obj[k];
    if (typeof v === 'string' && v.trim()) return collapse(v);
  }
  const scalars: Record<string, unknown> = {};
  for (const k of Object.keys(obj)) {
    if (obj[k] == null || typeof obj[k] !== 'object') scalars[k] = obj[k];
  }
  return Object.keys(scalars).length ? collapse(JSON.stringify(scalars)) : '';
}

/** D1 truncated-prefix guard: the trace narrative gets the UNCAPPED input
 * text ONLY when `describeToolInput`'s output is provably a truncated prefix
 * of it (the util's `_truncate` emits `slice(0, n).trimEnd() + '…'`, so the
 * ellipsis-stripped form prefixes the collapsed full text). Englishized
 * Cypher and save_* phrasings are REWRITES, never prefixes — they pass
 * through untouched. Graph labels / HUD keep the capped form regardless. */
export function narrativeInputText(reqEng: string, input: unknown): string {
  if (!reqEng || !reqEng.endsWith('…')) return reqEng;
  const full = fullToolInputText(input);
  return full && full.startsWith(reqEng.slice(0, -1)) ? full : reqEng;
}

function resultObject(payload: unknown): Record<string, unknown> | null {
  if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
    return payload as Record<string, unknown>;
  }
  if (typeof payload !== 'string' || !payload.trim()) return null;
  try {
    const parsed: unknown = JSON.parse(payload);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/* ── System labels ───────────────────────────────────────────────────────── */

const SYSTEM_LABELS: Record<string, string> = {
  explore_graph: 'Knowledge Graph',
  get_system_schema: 'Get Schema',
};

/** Dynamic label for any query_* MCP tool — concise system name (no "Query"
 * prefix), through the ONE resolver (lib/system-names.ts): the catalog
 * display name (catalog `name`), else the id-derived fallback ("ERP",
 * "MES Ind"). Single source of graph-node + HUD labels; the graph wraps
 * long names itself (inv-graph `text-wrap`). */
export function getSystemLabel(toolName: string): string {
  if (SYSTEM_LABELS[toolName]) return SYSTEM_LABELS[toolName];
  const ownerId = toolName.includes('___')
    ? toolName.split('___')[0]
    : toolName.startsWith('query_')
      ? toolName.slice(6)
      : '';
  if (ownerId) return systemName(ownerId) || toolName;
  return toolName;
}

/** How a finding node shortens the system line of its label: each call
 * site keeps its own rule ('query' strips a "Query " prefix, 'vendor' a
 * vendor prefix, 'flat' joins a two-line label). Shared so a relabel
 * rebuilds exactly what the call site built. */
type ShortSysForm = 'query' | 'vendor' | 'flat';

function shortSystemLabel(label: string, form: ShortSysForm): string {
  if (form === 'query') return label.replace(/^Query\s*/i, '');
  if (form === 'vendor') return label.replace(/^(SAP |IBM |Ignition |Teamcenter |Neptune )/, '');
  return label.replace(/\n/g, ' ');
}

/* ── Types (Wave-1 stub types preserved, extended additively) ───────────── */

export interface ArchNode {
  id: string;
  label?: string;
  /** e.g. 'question' | 'system' | 'finding' — drives node palette. */
  type?: string;
  [key: string]: unknown;
}

export interface ArchEdge {
  id?: string;
  source: string;
  target: string;
  label?: string;
  [key: string]: unknown;
}

/** One reasoning-trace entry (port of addTraceEntry rows). */
export interface TraceEntry {
  /** Monotonic id (render key). */
  id: number;
  type: 'discovery' | 'decision';
  label: string;
  text: string;
  nodeId?: string;
  question: number;
  /** "Step N" counter at creation time. */
  step: number;
  /** Honest outcome of the completed call this entry reports (tool_result
   * entries only): 'failed' → red dot + error first-line body; 'empty' →
   * amber hollow dot + explicit "No data returned" body; 'timeout' → red dot
   * + "Request timed out after Ns" body; 'partial' → amber half dot + the
   * summary incl. its partial-stop note. Absent = normal. */
  outcome?: Exclude<CallOutcome, 'success'>;
  /** WHY the outcome was chosen (classifyToolResultDetailed reason) — the
   * trace renders it under non-success rows so a 'failed' is never opaque. */
  outcomeReason?: string;
  /** True when this step re-calls a system that FAILED earlier in the SAME
   * question (client-side heuristic) — renders a subtle 'retry' badge. */
  retry?: boolean;
  /** This failed step was followed by a successful retry to the same system. */
  recovered?: boolean;
  /** Correlation key of the completed call this entry reports (the call's
   * HUD row id) — links trace row ↔ HUD chip ↔ graph nodes bidirectionally. */
  callKey?: string;
}

/** One tool-activity HUD row (port of the #tool-hud children). */
export interface HudEntry {
  /** DOM-compatible id (`hud-<tool>-q<N>-<ts>`), unique per row. */
  id: string;
  kind: 'tool' | 'batch' | 'summary';
  question: number;
  /** kind === 'tool' */
  tool?: string;
  label?: string;
  t0?: number;
  num?: number;
  parallel?: boolean;
  status?: 'running' | 'ok' | 'stopped';
  elapsedMs?: number;
  /** Honest outcome of a COMPLETED call ('ok' rows): success keeps the green
   * ✓; 'failed' renders a red ✗ (+ errorLine tooltip); 'empty' renders a
   * hollow amber ○ "no data". Absent on running/stopped rows. */
  outcome?: CallOutcome;
  /** First line of the error summary ('failed' rows) — tooltip text. */
  errorLine?: string;
  /** WHY the non-success outcome was chosen — appended to the tooltip. */
  outcomeReason?: string;
  /** The call failed, but a later retry to this system succeeded. */
  recovered?: boolean;
  /** kind === 'summary' — collapsed prior-question chip ("Q1 · N tools ✓"). */
  count?: number;
}

/** Server-computed intelligence summary (`intelligence_summary` event) —
 * alias of the single superset definition in api/types/chat.ts. */
export type { IntelligenceSummary } from '../api/types/chat';

export interface ArchGraphState {
  nodes: ArchNode[];
  edges: ArchEdge[];
  /* Additive (Task 2.3) — reads of only nodes/edges keep working: */
  trace: TraceEntry[];
  hud: HudEntry[];
  questionCounter: number;
  currentQuestionId: string;
  /** Text of the live query (#active-query); '' when none. */
  activeQuestion: string;
  /** Per-question focus filter (Q# timeline); null = All. */
  activeQuestionFilter: number | null;
  /** Investigation edges currently animating (edge ids with .flowing). */
  flowingEdges: string[];
  lastIntelligence: IntelligenceSummary | null;
  /** Task 1H: true while a streamed turn is live (user_msg → done). Drives
   * the idle-but-live question-node pulse together with hudInFlight. */
  investigationActive: boolean;
  /** Task 1H: tool_start events without a matching tool_result yet. */
  hudInFlight: number;
}

export type ArchStoreListener = (state: ArchGraphState) => void;
/** Raw pass-through of each bridge event (drives system-map animations). */
export type ArchEventListener = (eventType: string, data: Record<string, unknown>) => void;

export interface ArchStore {
  /** Add nodes (deduped by id) and notify subscribers. */
  addNodes(nodes: ArchNode[]): void;
  /** Add one edge and notify subscribers. */
  addEdge(edge: ArchEdge): void;
  /** Clear the investigation graph (resetArchitectureState / '+ New'). */
  reset(): void;
  /** Subscribe to state changes; returns an unsubscribe function. */
  subscribe(listener: ArchStoreListener): () => void;
  /** Snapshot of the current graph. */
  getState(): ArchGraphState;
  /* Additive (Task 2.3): */
  /** Chat-bridge entry point (handleArchEvent). */
  handleArchEvent(eventType: string, data?: Record<string, unknown>): void;
  /** Subscribe to raw bridge events (map animations); returns unsubscribe. */
  subscribeEvents(listener: ArchEventListener): () => void;
  /** Q# timeline filter (filterToQuestion). */
  setQuestionFilter(q: number | null): void;
  /** Stop following catalog name changes (lib/system-names.ts). Idempotent;
   * the store keeps working, its labels just stop relabelling. For stores
   * made outside the page singleton (tests). */
  dispose(): void;
}

/* ── Pure graph helpers ──────────────────────────────────────────────────── */

const FIELD_NAME_DENYLIST = new Set([
  'batch_number', 'material', 'material_lot', 'batch', 'order_id', 'run_id',
  'asset_id', 'product_id', 'wo_id', 'check_id', 'alert_id', 'spec_id', 'bom_id',
]);

/** Derive the DEDUP'd investigation-graph node for a tool call (I1). Every
 * knowledge-graph operation — explore_graph AND save_* writes — collapses onto
 * ONE "system-neptune" hub; each leaf system is ONE node keyed by its system id. */
export function deriveInvNode(tool: string): { sysId: string; nodeType: string; label: string } {
  if (tool === 'get_system_schema') {
    return { sysId: 'system-get_system_schema', nodeType: 'schema', label: getSystemLabel(tool) };
  }
  const half = typeof tool === 'string' && tool.indexOf('___') !== -1 ? tool.split('___')[1] : tool;
  if (half === 'explore_graph' || (half && half.indexOf('save_') === 0)) {
    return { sysId: 'system-neptune', nodeType: 'neptune', label: 'Knowledge Graph' };
  }
  const sid = _mapToolToSystemId(tool);
  if (sid) return { sysId: `system-${sid}`, nodeType: 'system', label: getSystemLabel(tool) };
  // Unknown/leaf tool with no derivable system id — keep the legacy per-tool node.
  return { sysId: `system-${tool}`, nodeType: 'system', label: getSystemLabel(tool) };
}

/** Compose a system-node label: base name, an "×N" repeat badge, and a plain-
 * English "last request" sub-line (I2/I3). */
export function composeInvLabel(base: unknown, count: number, lastReq?: string): string {
  let lbl = String(base || '') + (count > 1 ? '  ×' + count : '');
  if (lastReq) {
    let r = String(lastReq).replace(/\s+/g, ' ').trim();
    if (r.length > 52) r = r.slice(0, 52).trimEnd() + '…';
    lbl += '\n' + r;
  }
  return lbl;
}

/* ── Per-call drill-down payload (rich drill-down) ───────────────────────
 * EVERYTHING the frontend receives for one tool call, captured from the SSE
 * pair (tool_start carries the call INPUT; tool_result carries the summary
 * markdown plus, per agents/events.py, the raw result payload in its `input`
 * field). Attached as `_call` to the finding/entity/qinput nodes a result
 * mints, and aggregated as `_calls` on the dedup'd system node — both drive
 * the click-to-open modal. NOTE: `resultSummary` is the FULL summary as
 * emitted; the SERVER truncates it before emitting (tool_result_summary.py
 * raw[:2000] / [:200] fallbacks) — a backend follow-up, not fixable here. */
export interface InvCallDetail {
  tool: string;
  /** Human system label (catalog name, e.g. "ERP"; or "Knowledge Graph"). */
  system: string;
  /** Question number (Q#) this call belonged to. */
  question: number;
  /** The user question text this call served. */
  questionText: string;
  /** Plain-English request line (architecture-util describeToolInput). */
  inputEnglish: string;
  /** Raw tool_start input (pretty JSON / string, capped). */
  inputRaw: string;
  /** FULL result summary markdown from the tool_result event. */
  resultSummary: string;
  /** The tool_result's `telemetry_coverage` lines (display only). Never part
   * of `resultSummary` or the modal markdown: the data modal shows them as
   * text nodes from the extras (data-modal.tsx `telemetrySections`), so no
   * classifier, markdown parser or chart splitter reads them. */
  telemetryCoverage?: string[];
  /** The tool_result's `sample_line` (display only), shown the same way. */
  sampleLine?: string;
  /** Plain-English result line (describeToolResult, or a line from typed
   * data: telemetryReadsLine / engineNoRowsLine). */
  resultEnglish?: string;
  /** Raw tool_result payload (capped) when it adds data beyond the summary. */
  resultRaw?: string;
  /** Epoch ms of the tool_start — the event's server `ts` when present
   * (stream-realtime wave), else the browser receipt time. */
  startedAt?: number;
  /** Call duration: the tool_result's server-paired `elapsed_ms` when
   * present, else the browser receipt-clock pairing (deploy-skew fallback). */
  elapsedMs?: number;
  /** Classified outcome of this call + WHY — the modal's Outcome line. */
  outcome?: CallOutcome;
  outcomeReason?: string;
}

/** Contract exposed to the data-modal stage: inv-graph passes this as an
 * OPTIONAL third argument to `window.openDataModal(title, contentMd, extras)`.
 * The current 2-arg modal ignores it; the modal stage can consume it to
 * render structured drill-down UI without re-parsing the markdown. */
export interface InvModalExtras {
  kind: 'inv-call' | 'inv-node';
  nodeId: string;
  nodeType?: string;
  calls: InvCallDetail[];
}

/** Bounds on the typed telemetry lines a drill-down keeps, the server's own
 * (agents/telemetry_coverage.py MAX_COVERAGE_LINES, MAX_COVERAGE_LINE_CHARS,
 * MAX_SAMPLE_LINE_CHARS). */
const MAX_COVERAGE_LINES = 16;
const MAX_COVERAGE_LINE_CHARS = 300;
const MAX_SAMPLE_LINE_CHARS = 2000;

/** The tool_result frame's `telemetry_coverage` and `sample_line`, bounded,
 * for the drill-down. Each is kept beside the summary, never joined into it,
 * so the call's outcome stays classified from the summary alone. */
function telemetryLinesOf(data: Record<string, unknown>): Pick<InvCallDetail, 'telemetryCoverage' | 'sampleLine'> {
  const coverage = Array.isArray(data.telemetry_coverage)
    ? data.telemetry_coverage
        .filter((l): l is string => typeof l === 'string' && !!l.trim())
        .slice(0, MAX_COVERAGE_LINES)
        .map((l) => l.slice(0, MAX_COVERAGE_LINE_CHARS))
    : [];
  const sample =
    typeof data.sample_line === 'string' && data.sample_line.trim()
      ? data.sample_line.slice(0, MAX_SAMPLE_LINE_CHARS)
      : '';
  return {
    ...(coverage.length ? { telemetryCoverage: coverage } : {}),
    ...(sample ? { sampleLine: sample } : {}),
  };
}

/** The result line of a call that carries typed telemetry lines: how many
 * reads they report, e.g. "3 telemetry reads". Each line is one read of one
 * series over one window (agents/telemetry_coverage.py), and the lines are
 * shown under this line, so the two count the same thing. The count is the
 * number of lines, never anything parsed from their text. At the server's
 * bound the call may have read more than it kept, so the line says so.
 * Empty when the call has no typed lines. Used only in place of the prose
 * line of a reply that holds a table (readsPastTable), whose rows are never
 * read as a count; a "No matching data" or other prose line is kept. */
export function telemetryReadsLine(coverage: string[] | undefined): string {
  const reads = coverage?.length ?? 0;
  if (!reads) return '';
  if (reads >= MAX_COVERAGE_LINES) return `${MAX_COVERAGE_LINES} or more telemetry reads`;
  return `${reads} telemetry read${reads === 1 ? '' : 's'}`;
}

/** Characters of a node's detail its hover tooltip shows. */
const TOOLTIP_CHARS = 300;

/** The hover tooltip of an Investigation graph node: its detail as plain text
 * (plainSummaryText: one line per content line, inline syntax removed, a
 * table row as its cells' text and fenced code as written, so a result that
 * is only a table or only code still has a preview), cut at TOOLTIP_CHARS
 * with "…". The tooltip is text, never rendered markup; the node keeps the
 * full detail for the drill-down, which renders it. '' when the detail holds
 * no content. */
export function invTooltipText(detail: unknown): string {
  const text = _plainSummaryText(String(detail ?? ''));
  return text.length > TOOLTIP_CHARS ? text.slice(0, TOOLTIP_CHARS).trimEnd() + '…' : text;
}

/** Cap + stringify a raw SSE payload for the drill-down (objects → pretty
 * JSON). Exported for unit tests. */
export function rawPayloadText(v: unknown, cap = 4000): string {
  if (v == null) return '';
  let s: string;
  if (typeof v === 'string') s = v;
  else {
    try {
      s = JSON.stringify(v, null, 2);
    } catch {
      s = String(v);
    }
  }
  s = s.trim();
  return s.length > cap ? s.slice(0, cap) + '\n… (truncated)' : s;
}

/** Markdown for ONE call's drill-down modal (finding/entity/qinput click):
 * requesting system + tool, the question it served, timing, the request in
 * BOTH English and raw form, and the FULL result summary (not the headline). */
export function buildInvCallModal(node: ArchNode): string {
  const call = node._call as InvCallDetail | undefined;
  if (!call) return node.detail ? String(node.detail) : '';
  const parts: string[] = [];
  let head = `**System:** ${call.system} · **Tool:** \`${call.tool}\``;
  if (call.elapsedMs != null) head += ` · **Duration:** ${(call.elapsedMs / 1000).toFixed(1)}s`;
  parts.push(head);
  if (call.outcome && call.outcome !== 'success') {
    parts.push(
      `**Outcome:** ${call.outcome}${call.outcomeReason ? ` — classified because ${call.outcomeReason}` : ''}`,
    );
  }
  if (call.questionText) parts.push(`**Question (Q${call.question}):** ${call.questionText}`);
  const req: string[] = [];
  if (call.inputEnglish) req.push(call.inputEnglish);
  if (call.inputRaw) req.push('```json\n' + call.inputRaw + '\n```');
  if (req.length) parts.push(`### Request\n\n${req.join('\n\n')}`);
  parts.push(`### Result\n\n${call.resultSummary || '_(empty result)_'}`);
  if (call.resultRaw && call.resultRaw !== call.resultSummary.trim()) {
    parts.push(`### Raw result\n\n\`\`\`json\n${call.resultRaw}\n\`\`\``);
  }
  return parts.join('\n\n');
}

/** Build the full, scrollable modal body (markdown) for a system/neptune node
 * from its aggregated per-call `_calls` (rich path) or the legacy
 * _requests / _results / _fullBodies arrays (I5 fallback). */
export function buildInvNodeModal(node: ArchNode): string {
  const calls = (node._calls as InvCallDetail[]) || [];
  if (calls.length) {
    return calls
      .map((c, i) => {
        let block = `### Request ${i + 1} — \`${c.tool}\``;
        if (c.elapsedMs != null) block += ` · ${(c.elapsedMs / 1000).toFixed(1)}s`;
        block += `\n\n${c.inputEnglish ? c.inputEnglish : '_(no request captured)_'}\n`;
        if (c.inputRaw) block += `\n\`\`\`json\n${c.inputRaw}\n\`\`\`\n`;
        if (c.resultEnglish) block += `\n**Result:** ${c.resultEnglish}\n`;
        if (c.resultSummary) block += `\n${c.resultSummary}\n`;
        return block;
      })
      .join('\n\n---\n\n');
  }
  const reqs = (node._requests as string[]) || [];
  const results = (node._results as string[]) || [];
  const bodies = (node._fullBodies as string[]) || [];
  const n = Math.max(reqs.length, results.length, bodies.length);
  if (!n) {
    const detail = node.detail;
    return detail ? String(detail) : '';
  }
  const parts: string[] = [];
  for (let i = 0; i < n; i++) {
    let block = `### Request ${i + 1}\n\n${reqs[i] ? reqs[i] : '_(no request captured)_'}\n`;
    if (results[i]) block += `\n**Result:** ${results[i]}\n`;
    if (bodies[i]) block += `\n${bodies[i]}\n`;
    parts.push(block);
  }
  return parts.join('\n\n---\n\n');
}

/** Envelope head — the ONLY text a failure/empty marker may appear in: the
 * first ENVELOPE_HEAD_CHARS characters of the trimmed summary (lines
 * included, so a wrapped traceback or a one-line preamble before "Error:"
 * still classifies). MIRRORS
 * agents/query_intelligence.py envelope_head (pinned by the shared fixture
 * test/fixtures/tool-result-outcome-cases.json). Evidence: a live ERP prose
 * answer (eight successful queries) ending "There are no results to display
 * ... does not exist in the current dataset" rendered as a red 'failed'
 * node because these needles matched anywhere in the text. */
export const ENVELOPE_HEAD_CHARS = 240;

export function envelopeHead(summary: string): string {
  return summary.trim().slice(0, ENVELOPE_HEAD_CHARS);
}

// "does not exist" / "cannot be resolved" are anchored to the SQL error
// shapes they come from (`relation "x" does not exist`, `Column 'x' cannot
// be resolved`) — as bare phrases they match honest absence prose
// ("the linkage does not exist in this dataset").
const TRANSIENT_RE =
  /COLUMN_NOT_FOUND|TABLE_NOT_FOUND|\b(?:column|relation|table|schema|database|view|function)\s+["'`]?[\w.$-]+["'`]?\s+(?:does not exist|cannot be resolved)|bytes[\s_-]*scanned[^\n]*(?:exceeded|limit)|(?:scan|query)[^\n]*limit exceeded|UndefinedColumn|UndefinedTable|\bexception\b|error executing|query failed|error:|McpException|MCP invocation failed|Authorization error when sending message/i;

/** The transient-error needle a summary's envelope head carries, or null. */
function transientNeedle(summary: string): string | null {
  const head = envelopeHead(summary);
  if (head.startsWith('Query error')) return 'Query error';
  if (head.startsWith('{"toolUseId')) return 'malformed raw result';
  const m = TRANSIENT_RE.exec(head);
  return m ? m[0] : null;
}

/** Transient, self-recovered query error (COLUMN_NOT_FOUND, bytes-scanned
 * cutoff, malformed raw result, generic "Query error") — NOT surfaced.
 *
 * Task 1F: also matches MCP transport/auth failures ("McpException - MCP
 * invocation failed: Authorization error when sending message") — the
 * existing `\bexception\b` cannot match inside "McpException" and the text
 * carries no "error:" colon. Belt-and-braces with the orchestrator-side
 * retry (Task 1E): even an exhausted-retry auth error must not render as a
 * result node or pollute the trace/modal aggregation.
 *
 * Envelope-scoped: only the envelopeHead is examined, so the same phrases
 * inside a system agent's prose answer never classify it. */
export function isTransientError(summary: string): boolean {
  return transientNeedle(summary) !== null;
}

/** Genuine "no matching data" result — a real finding, stays in the graph. */
export function isNoResults(summary: string, tool?: string): boolean {
  return (
    summary === 'No matching nodes found.' ||
    summary === 'No matching entities found.' ||
    (summary === 'No records found.' && !(tool || '').startsWith('sitewise'))
  );
}

/* ── Completed-call outcome (HUD/trace honesty) ──────────────────────────
 * The tool_result SSE event carries NO per-call server flag (chat_client.py
 * emits only tool/tool_id/summary/input; the server's is_failed_result
 * verdict reaches the frontend only as the AGGREGATE wasted_calls count in
 * intelligence_summary). So the frontend re-derives per-call outcome with
 * the SAME envelope-scoped semantics — EMPTY_RE / ERROR_RE below MIRROR
 * agents/query_intelligence.py _EMPTY_RE / _ERROR_RE (word-boundary
 * needles on the envelope head only; deliberately NOT the bare word
 * "error"). `transientNeedle` is folded into 'failed' so MCP transport/auth
 * failures (McpException etc., which carry no "error:" colon) classify
 * honestly too — a UI-only widening the fixture marks `divergent`. */

/** Outcome of a COMPLETED tool call, as shown in the HUD/trace/graph.
 * 'timeout' — a failed/empty result whose server-paired elapsed_ms exceeded
 * the gateway budget (call it what it is: "request timed out", never
 * "no data returned"); 'partial' — the SiteWise budget stop marker
 * ("partial: stopped at …"): real data, honestly labeled incomplete. */
export type CallOutcome = 'success' | 'failed' | 'empty' | 'timeout' | 'partial' | 'filtered';

/* Word-boundary needles (ADR-0054: never a bare substring) — MIRROR
 * agents/query_intelligence.py _EMPTY_RE / _ERROR_RE. */
const EMPTY_RE = /\b(?:no results|no records|no matching|no rows|returned no rows|empty result)\b/i;
// `\w*` keeps CamelCase envelopes (ValueError:, OperationalError:, McpException:).
const ERROR_RE =
  /\b\w*(?:error|exception):|\berror executing\b|\bquery failed\b|\bfailed to execute\b|\btraceback \(most recent\b/i;

function summaryCarriesRows(summary: string): boolean {
  if (/^\d+\s+records?\s+found\b/i.test(summary.trim())) return true;
  const lines = summary.split('\n').map((line) => line.trim()).filter(Boolean);
  // The line ABOVE the separator must be a piped header row — a bare
  // markdown rule ("---") under prose is not a table.
  const separator = lines.findIndex(
    (line, i) => i >= 1 && lines[i - 1].includes('|') && /^\|?[\s:\-|]+\|?$/.test(line) && line.includes('-'),
  );
  return separator >= 1 && lines.slice(separator + 1).some((line) => line.includes('|'));
}

/** A failed/empty completion whose call ran longer than this is a TIMEOUT —
 * the ~60s gateway/tool budget minus margin. Timeout beats empty/failed in
 * precedence (an empty envelope after 60s is a timeout, not "no data"). */
export const TIMEOUT_ELAPSED_MS = 55_000;

/** SiteWise query-budget partial marker (deployed backend emits this line
 * when a scan stops at its budget) — a 'partial' outcome, rendered amber. */
export const PARTIAL_MARKER = 'partial: stopped at';

/** A classified outcome plus the human-readable REASON it was chosen — so a
 * red 'failed' is never opaque ("why is this failed?" is answerable from the
 * trace, the HUD tooltip and the call modal). `reason` is '' for success. */
export interface ClassifiedOutcome {
  outcome: CallOutcome;
  reason: string;
}

function quoteNeedle(needle: string): string {
  return `"${needle.trim().slice(0, 60)}"`;
}

/** The fixed engine line a system agent's answer carries when no data query
 * its call ran returned data — MIRRORS agents/query_intelligence.py
 * _NO_DATA_LINE_RE (pinned by the shared fixture). Code-authored, so it is
 * matched ANYWHERE in the summary and ahead of the table check: a prose
 * answer about failed attempts carries no envelope marker, and test event
 * 32 (2026-10-15) showed such calls as answered. `no rows` alone is empty,
 * any other category list failed. */
const NO_DATA_LINE_RE =
  /\[engine\] No query this call ran returned data \(((?:[a-z_]{1,40}|no rows)(?:, (?:[a-z_]{1,40}|no rows)){0,15})\)\./;

/** Base (pre-timeout) classification of a non-save call's summary. */
function classifyBase(s: string): ClassifiedOutcome {
  if (!s) return { outcome: 'empty', reason: 'the tool returned an empty result' };
  const engine = NO_DATA_LINE_RE.exec(s);
  if (engine) {
    return engine[1] === 'no rows'
      ? { outcome: 'empty', reason: 'the engine says no query this call ran returned rows' }
      : { outcome: 'failed', reason: 'the engine says no query this call ran returned data' };
  }
  if (summaryCarriesRows(s)) return { outcome: 'success', reason: '' };
  const head = envelopeHead(s);
  const err = ERROR_RE.exec(head)?.[0] ?? transientNeedle(s);
  if (err) {
    return { outcome: 'failed', reason: `the result opens with the error marker ${quoteNeedle(err)}` };
  }
  const empty = EMPTY_RE.exec(head)?.[0];
  if (empty) {
    return { outcome: 'empty', reason: `the result opens with the no-data marker ${quoteNeedle(empty)}` };
  }
  return { outcome: 'success', reason: '' };
}

/** The result line of a call the engine says came back with no rows: "No
 * matching data" when the call's outcome is 'empty' because its summary
 * carries the engine no-data line with `no rows` alone (NO_DATA_LINE_RE,
 * the typed verdict classifyBase reads first), else ''. That line is
 * code-authored from the call's own captured attempts and statement row
 * counts (system_agent._no_data_fact), and every copy of it in model text is
 * made inert before the engine appends its own, so the line and the outcome
 * rest on the same verdict. It replaces any line described from the summary,
 * a count of the reply's own table rows included. A timed-out or partial
 * call (the outcome moved off 'empty') and a no-data line naming a failure
 * ('failed') keep their own lines. */
export function engineNoRowsLine(summary: string, outcome: CallOutcome): string {
  if (outcome !== 'empty') return '';
  return NO_DATA_LINE_RE.exec(String(summary ?? ''))?.[1] === 'no rows' ? 'No matching data' : '';
}

/** Classify a completed call's summary WITH the reason: 'partial'
 * (budget-stop marker, data present but incomplete), 'timeout'
 * (failed/empty envelope whose elapsed exceeded the budget — timeout beats
 * empty AND failed), 'failed' (error envelope), 'empty' (blank or no-data
 * envelope), else 'success'. Envelope-scoped — markers count only in the
 * envelopeHead of a result that carries no data table — and mirrors the
 * server's wasted-call semantics (failed ∪ empty == is_failed_result) plus
 * the client-side timeout/partial honesty layer. */
export function classifyToolResultDetailed(
  summary: unknown,
  elapsedMs?: number,
  resultPayload?: unknown,
  tool = '',
): ClassifiedOutcome {
  const s = String(summary ?? '').trim();
  if (toolHalf(tool).startsWith('save_')) {
    const status = String(resultObject(resultPayload)?.status ?? '').toLowerCase();
    if (status === 'dropped' || status === 'filtered' || status === 'skipped') {
      return { outcome: 'filtered', reason: `the learning write reported status "${status}"` };
    }
    if (status === 'rejected' || status === 'failed' || status === 'error') {
      return { outcome: 'failed', reason: `the learning write reported status "${status}"` };
    }
  }
  // The partial marker is an explicit server verdict — data came back before
  // a deliberate budget stop. It is NOT a timeout even on a long call.
  if (s && s.toLowerCase().includes(PARTIAL_MARKER)) {
    return { outcome: 'partial', reason: 'the query stopped at its scan budget (partial data)' };
  }
  const base = classifyBase(s);
  if (
    (base.outcome === 'failed' || base.outcome === 'empty') &&
    typeof elapsedMs === 'number' &&
    Number.isFinite(elapsedMs) &&
    elapsedMs > TIMEOUT_ELAPSED_MS
  ) {
    return {
      outcome: 'timeout',
      reason: `${base.reason} after ${Math.round(elapsedMs / 1000)}s (over the ${TIMEOUT_ELAPSED_MS / 1000}s budget)`,
    };
  }
  return base;
}

/** Outcome-only view of classifyToolResultDetailed (HUD/trace/graph). */
export function classifyToolResult(
  summary: unknown,
  elapsedMs?: number,
  resultPayload?: unknown,
  tool = '',
): CallOutcome {
  return classifyToolResultDetailed(summary, elapsedMs, resultPayload, tool).outcome;
}

/** Honest timeout body/tooltip text — "Request timed out after Ns". */
export function timeoutText(elapsedMs?: number): string {
  const secs = Math.round((elapsedMs ?? 0) / 1000);
  return `Request timed out after ${secs}s`;
}

/** The partial-stop note line from a 'partial' summary — the first line
 * containing the marker, capped like HUD errors.
 * KEEP the 200 cap (trace de-truncation audit, D2): the SOLE consumer is the
 * HUD tooltip `row.errorLine` (rendered only in `title=` attrs). Partial
 * TRACE rows never see this — they carry the FULL `summary.trim()` through
 * the parseSqlFindings else-branch. Do not "split" this for the trace. */
export function partialNote(summary: unknown): string {
  const line = String(summary ?? '')
    .split('\n')
    .find((l) => l.toLowerCase().includes(PARTIAL_MARKER));
  const t = (line ?? '').trim();
  return t.length > 200 ? t.slice(0, 200).trimEnd() + '…' : t;
}

/** First line of an error summary, trimmed and capped — HUD tooltip text.
 * Rendering escapes it (React text nodes / react-markdown).
 * `cap` SPLIT (trace de-truncation): the default 200 stays for the HUD
 * `row.errorLine` tooltip; TRACE rows and the error-node `detail` pass
 * `Infinity` — the first-LINE-only split remains, the length cap goes (the
 * TraceEntries SHOW MORE clamp is the only length control for trace rows). */
export function errorFirstLine(summary: unknown, cap = 200): string {
  const line = String(summary ?? '').split('\n')[0].trim();
  return line.length > cap ? line.slice(0, cap).trimEnd() + '…' : line;
}

/** Task 1H: presence-driven placeholder rule — the "Knowledge Discovery /
 * Ask a question…" empty-state overlay hides as soon as the investigation has
 * ANY content (a question was asked OR nodes exist), so an event stream that
 * raced a reset can never show the placeholder over a populating graph. */
export function archPlaceholderHidden(
  state: Pick<ArchGraphState, 'questionCounter'> & { nodes: ReadonlyArray<unknown> },
): boolean {
  return state.questionCounter > 0 || state.nodes.length > 0;
}

/** Human-readable edge label from the input query (tool_result heuristics). */
export function edgeLabelFromInput(rawInput: string): string {
  if (!rawInput) return 'result';
  const q = rawInput.toLowerCase();
  if (q.includes('identityrule')) return 'identity rules';
  if (q.includes('correlation')) return 'correlations';
  if (q.includes('joinpath')) return 'join paths';
  if (q.includes('valuemapping')) return 'status mappings';
  if (q.includes('system')) return 'systems';
  if (q.includes('quality') || q.includes('failure')) return 'quality data';
  if (q.includes('equipment') || q.includes('maintenance') || q.includes('sensor')) return 'equipment data';
  if (q.includes('order') || q.includes('supplier') || q.includes('material')) return 'order/material data';
  if (q.includes('batch') || q.includes('production')) return 'production data';
  return rawInput.slice(0, 35);
}

/* ── Store implementation ────────────────────────────────────────────────── */

export function createArchStore(): ArchStore {
  let nodes: ArchNode[] = [];
  let edges: ArchEdge[] = [];
  let trace: TraceEntry[] = [];
  let hud: HudEntry[] = [];
  let nodeIdCounter = 0;
  let traceStepCounter = 0;
  let traceIdCounter = 0;
  let questionCounter = 0;
  let currentQuestionId = 'question-root';
  let activeQuestion = '';
  let activeQuestionFilter: number | null = null;
  let flowingEdges = new Set<string>();
  let lastIntelligence: IntelligenceSummary | null = null;
  let hudInFlight = 0; // tool_start events without a matching tool_result
  // Task 1H: live-turn flag — true from user_msg until done (incl. the error-
  // path done emitted by useAskStream, so it can never stick on a failed turn).
  let investigationActive = false;
  // Per-system FIFO queue of pending calls (the _pendingInputs
  // bookkeeping): tool_start pushes {qinput id, in-flight
  // placeholder id, the call's raw+English input, t0, its flowing edge ids};
  // tool_result shifts the oldest so each result binds to the exact request
  // that made it — and now BOTH branches shift even on error, so a failed
  // call consumes its own entry (placeholder cleanup) and can never misbind
  // the NEXT result to the wrong question node.
  interface PendingCall {
    inputId: string;
    placeholderId: string | null;
    tool: string;
    inputEnglish: string;
    inputRaw: string;
    t0: number;
    /** Server-stamped tool_start epoch ms (`ts` on the event, stream-realtime
     * wave). Kept SEPARATE from the browser receipt clock `t0` — absolute
     * times prefer this; durations must never mix the two clocks. */
    serverTs?: number;
    flowEdgeIds: string[];
    /** Correlation key for this call — the HUD row id minted at tool_start.
     * Threaded onto trace entries and minted graph nodes (`_callKey`). */
    callKey: string;
  }
  let pendingInputs: Record<string, PendingCall[]> = {};
  // Deferral-seam heartbeat rows ('preparing <tool> query…', emitted while
  // the model is still COMPOSING the tool input) awaiting their tool_start
  // narrative, keyed by tool name in a per-tool FIFO. tool_start upgrades ITS
  // OWN tool's row in place — attach-to-latest misbound narratives in
  // parallel batches (heartbeat A, heartbeat B, tool_start A landed on B's
  // row, then B's attach no-op'd). Cleared per question and on reset.
  let pendingHeartbeats = new Map<string, TraceEntry[]>();
  // Systems with a FAILED call in the CURRENT question — drives the trace
  // 'retry' badge on subsequent steps for the same system. Reset per question.
  let failedSystems = new Set<string>();
  // Per-question record of completed 'empty' calls awaiting a possible retry
  // verdict: sysId → the HUD row / trace entry ids of each empty call. A
  // LATER call to the same system in the SAME question retroactively upgrades
  // them to 'failed' (no payload) — the retry proves the model judged the
  // empty result unusable. Reset per question (with failedSystems).
  let emptyAwaitingRetry: Record<string, { hudId?: string; traceId?: number }[]> = {};
  // In-flight placeholder data nodes (green pulsing '…') awaiting a result —
  // cleaned on the matching tool_result, and swept on done/user_msg/reset so
  // a crashed stream can never leave a stuck pulsing node.
  let inflightPlaceholders = new Set<string>();
  // Label provenance: every system-named string this store builds is recorded
  // with the tool it was built from, so catalog names that arrive AFTER a
  // call (GET /api/systems answering after a fast or replayed tool_start)
  // relabel what is already on screen (relabelSystems) instead of leaving
  // the id fallback beside the catalog name. Cleared on reset.
  const systemNodeTools = new Map<string, string>(); // system node id → tool
  const findingLabelSources = new Map<string, { tool: string; form: ShortSysForm; tail: string }>();
  const traceLabelSources = new Map<number, { tool: string; suffix: string }>();
  // Trace rows whose text carries a tool_start narrative: the text before it,
  // and what the narrative was built from.
  const traceNarrativeSources = new Map<number, { prefix: string; tool: string; input: string }>();

  const listeners = new Set<ArchStoreListener>();
  const eventListeners = new Set<ArchEventListener>();

  // Cached snapshot — stable identity between notifies so the React
  // useSyncExternalStore API does not loop on fresh objects.
  let snapshot: ArchGraphState = buildSnapshot();

  function buildSnapshot(): ArchGraphState {
    return {
      nodes: [...nodes],
      edges: [...edges],
      trace: [...trace],
      hud: [...hud],
      questionCounter,
      currentQuestionId,
      activeQuestion,
      activeQuestionFilter,
      flowingEdges: [...flowingEdges],
      lastIntelligence,
      investigationActive,
      hudInFlight,
    };
  }

  const notify = () => {
    snapshot = buildSnapshot();
    listeners.forEach((l) => l(snapshot));
  };

  const emit = (eventType: string, data: Record<string, unknown>) => {
    eventListeners.forEach((l) => l(eventType, data));
  };

  /** Port of addGraphNode (minus cytoscape): dedupe by id; an existing node
   * gains this question's number in its `questions` list. */
  function addNode(id: string, type: string, label: string, detail?: string): string {
    const existing = nodes.find((n) => n.id === id);
    if (existing) {
      const qs = (existing.questions as number[]) || [];
      if (!qs.includes(questionCounter)) {
        qs.push(questionCounter);
        existing.questions = qs;
      }
      return id;
    }
    nodes.push({ id, type, label, detail: detail || '', questions: [questionCounter] });
    return id;
  }

  /** Port of addGraphEdge: id `${source}->${target}-q${N}`, skip when the edge
   * exists or either endpoint is missing. */
  function addEdgeInternal(sourceId: string, targetId: string, label?: string): void {
    const edgeId = `${sourceId}->${targetId}-q${questionCounter}`;
    if (edges.some((e) => e.id === edgeId)) return;
    if (!nodes.some((n) => n.id === sourceId) || !nodes.some((n) => n.id === targetId)) return;
    edges.push({ id: edgeId, source: sourceId, target: targetId, label: label || '', questions: [questionCounter] });
  }

  /** Remove a node and every edge touching it (in-flight placeholder
   * consumption/sweep); touched edges also leave the flowing set. */
  function removeNodeAndEdges(id: string): void {
    nodes = nodes.filter((n) => n.id !== id);
    edges = edges.filter((e) => {
      const drop = e.source === id || e.target === id;
      if (drop && e.id) flowingEdges.delete(String(e.id));
      return !drop;
    });
  }

  /** Sweep ALL in-flight placeholders (done — incl. the error-path done from
   * useAskStream — and a new user_msg racing an unfinished turn). */
  function cleanupInflightPlaceholders(): void {
    inflightPlaceholders.forEach((id) => removeNodeAndEdges(id));
    inflightPlaceholders.clear();
  }

  /** Attach the per-call drill-down payload (and the correlation key) to a
   * minted node. */
  function attachCall(nodeId: string | null | undefined, call: InvCallDetail, callKey?: string): void {
    if (!nodeId) return;
    const n = nodes.find((x) => x.id === nodeId);
    if (!n) return;
    n._call = call;
    if (callKey) n._callKey = callKey;
  }

  /** Port of addTraceEntry (state only — the view renders "Step N · …").
   * Returns the new entry's id so callers can reference it later (the
   * empty→failed-when-retried retroactive upgrade). */
  function attachToolContextToLatestReasoning(
    narrative: string,
    nodeId: string,
    callKey: string,
    source: { tool: string; input: string },
  ): void {
    if (!narrative) return;
    const latest = trace[trace.length - 1];
    if (
      !latest ||
      latest.question !== questionCounter ||
      latest.type !== 'decision' ||
      latest.label !== 'Reasoning' ||
      latest.callKey
    ) return;
    if (!latest.text.includes(narrative)) {
      const prefix = `${latest.text}\n\n`;
      latest.text = prefix + narrative;
      traceNarrativeSources.set(latest.id, { prefix, ...source });
    }
    latest.nodeId = nodeId;
    latest.callKey = callKey;
  }

  function markPreviousFailuresRecovered(sysId: string, currentRow?: HudEntry): void {
    hud.forEach((entry) => {
      if (
        entry.kind === 'tool' &&
        entry !== currentRow &&
        entry.question === questionCounter &&
        entry.outcome === 'failed' &&
        deriveInvNode(String(entry.tool ?? '')).sysId === sysId
      ) entry.recovered = true;
    });
    trace.forEach((entry) => {
      if (entry.question === questionCounter && entry.nodeId === sysId && entry.outcome === 'failed') {
        entry.recovered = true;
      }
    });
  }

  function addTrace(
    type: 'discovery' | 'decision',
    label: string,
    text: string,
    nodeId?: string,
    opts?: {
      outcome?: Exclude<CallOutcome, 'success'>;
      outcomeReason?: string;
      retry?: boolean;
      callKey?: string;
      /** The tool whose system label `label` was built from (plus any text
       * after it), so a later catalog name relabels the step. */
      labelTool?: string;
      labelSuffix?: string;
    },
  ): number {
    const id = ++traceIdCounter;
    trace.push({
      id,
      type,
      label,
      text,
      nodeId,
      question: questionCounter,
      step: ++traceStepCounter,
      ...(opts?.outcome ? { outcome: opts.outcome } : {}),
      ...(opts?.outcome && opts.outcomeReason ? { outcomeReason: opts.outcomeReason } : {}),
      ...(opts?.retry ? { retry: true } : {}),
      ...(opts?.callKey ? { callKey: opts.callKey } : {}),
    });
    if (opts?.labelTool) {
      traceLabelSources.set(id, { tool: opts.labelTool, suffix: opts.labelSuffix ?? '' });
    }
    return id;
  }

  /** Backend deferral-seam heartbeat shape (agents/chat_client.py emits
   * `preparing <tool NAME> query…` while the model composes the tool input —
   * tool NAME only, never the partial input). */
  const HEARTBEAT_RE = /^preparing (.+) query…$/;

  /** 'reasoning' event: add the trace row; when it is a composition
   * heartbeat, ALSO queue the row (per-tool FIFO) so the matching tool_start
   * can upgrade it in place with the actual ask. */
  function onReasoning(data: Record<string, unknown>): void {
    const text = String(data.text ?? '');
    const id = addTrace('decision', 'Reasoning', text);
    const m = HEARTBEAT_RE.exec(text);
    if (!m) return;
    const entry = trace.find((t) => t.id === id);
    if (!entry) return;
    const queue = pendingHeartbeats.get(m[1]) ?? [];
    queue.push(entry);
    pendingHeartbeats.set(m[1], queue);
  }

  /** Empty→failed-when-retried: a NEW call to `sysId` proves the model judged
   * this question's earlier empty result(s) from the same system unusable —
   * upgrade those HUD rows + trace entries to 'failed' (no payload) IN PLACE
   * (the caller's notify() republishes the mutated rows). The system also
   * joins failedSystems so the in-flight re-call earns the 'retry' badge.
   * Triggered from tool_start only: a parallel sibling's RESULT arriving
   * after the empty must not read as a retry (its start preceded the empty). */
  function upgradeEmptiesRetried(sysId: string): void {
    const empties = emptyAwaitingRetry[sysId];
    if (!empties || !empties.length) return;
    delete emptyAwaitingRetry[sysId];
    const reason = 'it returned no data and the model re-called this system';
    empties.forEach(({ hudId, traceId }) => {
      const h = hudId ? hud.find((x) => x.id === hudId) : undefined;
      if (h && h.outcome === 'empty') {
        h.outcome = 'failed';
        h.errorLine = 'failed (no payload) — the model retried this system';
        h.outcomeReason = reason;
      }
      const t = traceId != null ? trace.find((x) => x.id === traceId) : undefined;
      if (t && t.outcome === 'empty') {
        t.outcome = 'failed';
        t.outcomeReason = reason;
      }
    });
    failedSystems.add(sysId);
  }

  /** Port of parseNeptuneEntities — Neptune results as entity nodes. Returns
   * the created entity node ids (empty when the summary is scalar rows). */
  function parseNeptuneEntities(summary: string, parentId: string): string[] {
    if (!summary) return [];
    const created: string[] = [];
    const lines = summary.split('\n');
    lines.forEach((line) => {
      if (!line.trim()) return;
      const match = line.match(/^(\w+):\s+(.+?)(?:\s+\((.+?)\))?$/);
      if (match) {
        const [, nodeLabel, name, domain] = match;
        if (FIELD_NAME_DENYLIST.has(name.toLowerCase())) return;
        const id = `entity-${name.replace(/\s+/g, '_')}-q${questionCounter}`;
        const detail = domain ? `${nodeLabel} — ${domain}` : nodeLabel;
        addNode(id, 'entity', name, detail);
        addEdgeInternal(parentId, id, nodeLabel.toLowerCase());
        created.push(id);
      } else {
        const parts = line.split('→').map((s) => s.trim()).filter(Boolean);
        if (parts.length < 2) return;
        let prevId = parentId;
        const labels = ['source', 'pattern', 'target', 'maps to'];
        parts.forEach((part, i) => {
          if (FIELD_NAME_DENYLIST.has(part.toLowerCase())) return;
          const id = `entity-${part.replace(/\s+/g, '_')}-q${questionCounter}`;
          addNode(id, 'entity', part, '');
          addEdgeInternal(prevId, id, labels[i] || 'relates');
          prevId = id;
          created.push(id);
        });
      }
    });
    return created;
  }

  /** Port of parseSqlFindings: SQL results as finding nodes. `resultLine`,
   * when given, is the call's result line from typed data (its telemetry
   * lines, telemetryReadsLine, or the engine no-rows verdict,
   * engineNoRowsLine) and replaces the one described from the summary. */
  function parseSqlFindings(
    summary: string,
    parentId: string,
    systemLabel: string,
    rawSql: string,
    edgeLabel: string,
    tool: string,
    requestText: string,
    resultLine = '',
  ): string | null {
    if (!summary) return null;
    const firstLine = summary.split('\n')[0];
    const countMatch = firstLine.match(/^(\d+) records? found/);
    const edgeLabelFinal =
      edgeLabel || (countMatch ? `${countMatch[1]} record${countMatch[1] === '1' ? '' : 's'}` : 'result');

    // A system query's request is English by contract (describeToolInput),
    // so a FROM in it is a word ("from the last week"), not a table.
    const systemQuery = toolHalf(tool).startsWith('query_');
    const tableMatch = systemQuery ? null : (rawSql || '').match(/\bFROM\s+(\w+)/i);
    const tableName = tableMatch ? tableMatch[1].replace(/_/g, ' ') : null;

    const shortSys = shortSystemLabel(systemLabel || '', 'query');
    const desc = resultLine || _describeToolResult(tool, summary, requestText);
    const secondLine = desc || tableName || 'Result';
    const label = `${shortSys}\n${secondLine}`;

    const id = `finding-${++nodeIdCounter}`;
    addNode(id, 'finding', label, summary);
    findingLabelSources.set(id, { tool, form: 'query', tail: secondLine });
    addEdgeInternal(parentId, id, edgeLabelFinal);
    return id;
  }

  /* ── Event handlers (port of handleArchEvent, DOM-free) ─────────────── */

  function onUserMsg(data: Record<string, unknown>): void {
    // Collapse the previous question's HUD entries into one summary chip
    // ("Q1 · N tools ✓" → filterToQuestion on click).
    if (questionCounter > 0) {
      const prevTools = hud.filter((h) => h.question === questionCounter && h.kind === 'tool');
      if (prevTools.length) {
        hud = hud.filter((h) => h.question !== questionCounter);
        hud.push({
          id: `hud-summary-q${questionCounter}`,
          kind: 'summary',
          question: questionCounter,
          count: prevTools.length,
        });
      }
    }
    // A new question while calls are still in flight: sweep their
    // placeholders so a pulsing '…' node can never leak across questions.
    cleanupInflightPlaceholders();
    // Reset per-question tool call counters + request/result aggregation on
    // system nodes (drives the ×N badge + click-to-open modal).
    nodes.forEach((n) => {
      const t = n.type;
      if (t === 'system' || t === 'neptune' || t === 'schema') {
        n._totalQueries = 0;
        const base = (n._baseLabel as string) || (n.label as string);
        n._baseLabel = base;
        n.label = base;
        n._requests = [];
        n._results = [];
        n._fullBodies = [];
        n._calls = [];
        n._lastRequest = '';
        // KG-question aggregation (neptune node label sub-line / tooltip).
        n._kgQuestions = [];
        n._lastKgQuestion = '';
        if (t === 'neptune') n.detail = '';
      }
    });
    activeQuestion = String(data.question ?? '');
    hudInFlight = 0;
    investigationActive = true; // Task 1H: live until done (incl. error-path done)
    // Fresh per-request input-node queue for the new question.
    pendingInputs = {};
    failedSystems = new Set(); // retry badges are per-question
    emptyAwaitingRetry = {}; // empty→failed retry verdicts are per-question
    pendingHeartbeats = new Map(); // heartbeat upgrades never cross a question
    currentQuestionId = `question-${++questionCounter}`;
    addNode(currentQuestionId, 'question', String(data.question ?? ''), String(data.question ?? ''));
    // Anchor the trace narrative on the user's question (once per turn), so
    // it reads top-down: question → reasoning/approach → per-system asks →
    // results. UNCAPPED (trace de-truncation): the TraceEntries SHOW MORE
    // clamp is the only length control for trace rows.
    const qTrace = String(data.question ?? '').replace(/\s+/g, ' ').trim();
    if (qTrace) {
      addTrace('discovery', 'Question', qTrace, currentQuestionId);
    }
    if (questionCounter > 1) {
      addEdgeInternal(`question-${questionCounter - 1}`, currentQuestionId, 'then');
    }
    // Auto-switch to the newest question's tab (cumulative graph; prior
    // questions stay, dimmed, reachable via their Q# tab or "All").
    activeQuestionFilter = questionCounter;
  }

  /** Task 1H defense-in-depth mint-on-miss: guarantee the current question
   * node exists before any subtree attaches to it. A reset that raced the
   * stream (e.g. the legacy reset-on-tab-switch, now removed, or '+ New'
   * mid-stream) left `currentQuestionId = 'question-root'` with user_msg
   * already consumed — tool events then repopulated a ROOTLESS graph
   * (`addEdgeInternal` silently drops edges whose endpoints are missing).
   * Label comes from `activeQuestion`, else `window._lastDisplayText`
   * (documented global set by runAskTurn before the stream starts), else
   * 'Question'. Minting also re-arms the live flag — tool events only arrive
   * while a turn is streaming. */
  function ensureQuestionNode(): void {
    if (nodes.some((n) => n.id === currentQuestionId)) return;
    if (currentQuestionId === 'question-root') {
      currentQuestionId = `question-${++questionCounter}`;
    }
    const label =
      activeQuestion ||
      String((_g._lastDisplayText as string | undefined) || '') ||
      'Question';
    if (!activeQuestion) activeQuestion = label;
    addNode(currentQuestionId, 'question', label, label);
    activeQuestionFilter = questionCounter;
    investigationActive = true;
  }

  function onToolStart(data: Record<string, unknown>): void {
    ensureQuestionNode();
    const tool = String(data.tool ?? '');
    const inv = deriveInvNode(tool);
    const { sysId, nodeType, label: sysLabel } = inv;
    const reqEng = _describeToolInput(tool, data.input);
    // A NEW call to this system: any of this question's earlier 'empty'
    // completions from the same system upgrade to failed (no payload) — the
    // re-call is the proof the model judged them unusable.
    upgradeEmptiesRetried(sysId);
    // Server-truth timing (stream-realtime wave): tool_start may carry `ts`
    // (epoch ms, stamped at the emitter). Absolute times prefer it; the
    // browser receipt clock t0 stays for the HUD's live running ticker and
    // as the duration fallback — the two clocks are never mixed in a delta.
    const serverTs =
      typeof data.ts === 'number' && Number.isFinite(data.ts) ? data.ts : undefined;
    addNode(sysId, nodeType, sysLabel, '');
    if (!systemNodeTools.has(sysId)) systemNodeTools.set(sysId, tool);
    addEdgeInternal(currentQuestionId, sysId, 'queries');
    const node = nodes.find((n) => n.id === sysId);
    if (node) {
      // Track active query count to handle parallel calls correctly.
      node._activeQueries = ((node._activeQueries as number) || 0) + 1;
    }
    // Investigation edge animation.
    const edgeId = `${currentQuestionId}->${sysId}-q${questionCounter}`;
    if (edges.some((e) => e.id === edgeId)) flowingEdges.add(edgeId);

    // HUD entry (parallel-batch bracket when a second tool starts in-flight).
    const t0 = Date.now();
    const hudId = `hud-${tool}-q${questionCounter}-${t0}`;
    const isParallel = hudInFlight > 0;
    if (hudInFlight === 1) {
      // A batch just formed: the FIRST member is the still-running tool row.
      // Insert the bracket BEFORE it and indent it as part of the batch, so
      // the group reads "∥ parallel batch → member, member, member" instead of
      // the first member floating above the marker. Later sequential calls
      // (no indent) visually close the group.
      let firstIdx = -1;
      for (let i = hud.length - 1; i >= 0; i--) {
        const h = hud[i];
        if (h.kind === 'tool' && h.status === 'running') firstIdx = i;
        else if (h.kind === 'tool' || h.kind === 'summary') break;
      }
      const marker: HudEntry = {
        id: `hud-batch-q${questionCounter}-${t0}`, kind: 'batch', question: questionCounter,
      };
      if (firstIdx >= 0) {
        hud[firstIdx] = { ...hud[firstIdx], parallel: true };
        hud.splice(firstIdx, 0, marker);
      } else {
        hud.push(marker);
      }
    }
    const num = hud.filter((h) => h.kind === 'tool' || h.kind === 'summary').length + 1;
    hud.push({
      id: hudId,
      kind: 'tool',
      question: questionCounter,
      tool,
      label: sysLabel,
      t0,
      num,
      parallel: isParallel,
      status: 'running',
    });
    hudInFlight++;

    // Query count badge + plain-English "last request" (I3) + aggregation (I5).
    if (node) {
      const totalQueries = ((node._totalQueries as number) || 0) + 1;
      node._totalQueries = totalQueries;
      const baseLabel = (node._baseLabel as string) || sysLabel;
      node._baseLabel = baseLabel;
      if (reqEng) {
        node._lastRequest = reqEng;
        const reqs = (node._requests as string[]) || [];
        reqs.push(reqEng);
        node._requests = reqs;
      }
      // The request is its OWN blue node — the system node keeps base + ×N.
      // Task 1F: the Knowledge Graph hub is treated the SAME way — its label
      // is no longer mutated with the last KG question; each KG call
      // (explore_graph AND save_*) mints its own blue qinput node below. The
      // _kgQuestions/_lastKgQuestion aggregation and the `detail` list are
      // KEPT — they drive the hub tooltip and the click-to-open modal.
      const kgQ = nodeType === 'neptune' ? kgQuestionText(tool, data.input) || reqEng : '';
      if (nodeType === 'neptune') {
        node._lastKgQuestion = kgQ || '';
        const kgQs = (node._kgQuestions as string[]) || [];
        if (kgQ) kgQs.push(kgQ);
        node._kgQuestions = kgQs;
        node.detail = kgQs.map((q) => `KG: ${q}`).join('\n');
      }
      node.label = composeInvLabel(baseLabel, totalQueries, '');

      // Mint a distinct blue "input/question" node for THIS request (system →
      // question → result). Real system queries AND knowledge-graph calls.
      if (nodeType === 'system' || nodeType === 'neptune') {
        const rawQ = nodeType === 'neptune' ? kgQ : reqEng;
        let qText = rawQ ? String(rawQ).replace(/\s+/g, ' ').trim() : 'query';
        if (qText.length > 90) qText = qText.slice(0, 90).trimEnd() + '…';
        const inputId = `qinput-${++nodeIdCounter}`;
        addNode(inputId, 'qinput', qText, rawQ || '');
        addEdgeInternal(sysId, inputId, 'asks');
        // Correlation: the HUD row id is THIS call's key — stamped on the
        // qinput and placeholder now, on trace/finding nodes at tool_result.
        const qn = nodes.find((n) => n.id === inputId);
        if (qn) qn._callKey = hudId;
        // In-flight PLACEHOLDER data node — green (finding palette), pulsing
        // '…' until the matching tool_result fills its spot with the real
        // finding (existing node-content path). Marked `_inflight` for the
        // view's pulse/dashed styling and the done/user_msg/reset sweeps.
        const placeholderId = `pending-${++nodeIdCounter}`;
        addNode(placeholderId, 'finding', '…', '');
        const ph = nodes.find((n) => n.id === placeholderId);
        if (ph) {
          ph._inflight = true;
          ph._callKey = hudId;
        }
        addEdgeInternal(inputId, placeholderId, '');
        inflightPlaceholders.add(placeholderId);
        // Marching-ants along the WHOLE active path: system → qinput →
        // placeholder (question → system was flagged above).
        const inputFlowId = `${sysId}->${inputId}-q${questionCounter}`;
        const phFlowId = `${inputId}->${placeholderId}-q${questionCounter}`;
        flowingEdges.add(inputFlowId);
        flowingEdges.add(phFlowId);
        (pendingInputs[sysId] = pendingInputs[sysId] || []).push({
          inputId,
          placeholderId,
          tool,
          inputEnglish: reqEng || '',
          inputRaw: rawPayloadText(data.input),
          t0,
          serverTs,
          flowEdgeIds: [inputFlowId, phFlowId],
          callKey: hudId,
        });
      }
    }
    // Trace narrative for this call. KEYED upgrade first: shift THIS tool's
    // pending composition heartbeat (FIFO — consumed even when the narrative
    // is empty, so a later call to the same tool can never upgrade an older
    // row) and REPLACE its bare 'preparing <tool> query…' text with the
    // actual ask. Only when no heartbeat is pending does the narrative fall
    // back to attach-to-latest — a GENUINE model-reasoning row immediately
    // preceding the call keeps getting the tool context appended.
    // D1: the narrative input is the UNCAPPED text when describeToolInput's
    // 120-char cap truncated it — graph labels (`node._lastRequest`), the
    // qinput label, and `pendingInputs.inputEnglish` keep the capped reqEng.
    const narrativeInput = narrativeInputText(reqEng, data.input);
    const narrative = toolStartNarrative(tool, narrativeInput, sysLabel);
    const heartbeat = pendingHeartbeats.get(tool)?.shift();
    if (heartbeat) {
      if (narrative) {
        // Same claim semantics as attachToolContextToLatestReasoning: the
        // row gains this call's node + correlation key (trace ↔ HUD ↔ graph).
        heartbeat.text = narrative;
        heartbeat.nodeId = sysId;
        heartbeat.callKey = hudId;
        traceNarrativeSources.set(heartbeat.id, { prefix: '', tool, input: narrativeInput });
      }
      // Empty narrative (describeToolInput found no text): the heartbeat row
      // honestly keeps its 'preparing … query…' text.
    } else {
      attachToolContextToLatestReasoning(narrative, sysId, hudId, { tool, input: narrativeInput });
    }
  }

  function onToolResult(data: Record<string, unknown>): void {
    ensureQuestionNode(); // Task 1H: tool_result may be the first event after a reset race
    const tool = String(data.tool ?? '');
    const inv = deriveInvNode(tool);
    const { sysId, label: sysLabel } = inv;
    const isExplore = tool === 'explore_graph' || tool.split('___')[1] === 'explore_graph';
    const node = nodes.find((n) => n.id === sysId);
    let activeCount = 0;
    if (node) {
      // Only clear "querying" when all parallel calls are done.
      activeCount = Math.max(0, ((node._activeQueries as number) || 1) - 1);
      node._activeQueries = activeCount;
    }
    // Stop the question→system edge animation only when the LAST parallel
    // call to this system settles — siblings keep the path flowing.
    const edgeId = `${currentQuestionId}->${sysId}-q${questionCounter}`;
    if (activeCount === 0) flowingEdges.delete(edgeId);

    // Mark the oldest still-running HUD entry for this tool complete.
    const running = hud.filter((h) => h.kind === 'tool' && h.tool === tool && h.status === 'running');
    const row = running[0] || hud.filter((h) => h.kind === 'tool' && h.tool === tool).slice(-1)[0];
    const summary = String(data.summary ?? '');
    // Peek (not shift — that happens below, both branches) at THIS call's
    // pending entry for the receipt-clock duration fallback used by the
    // timeout classification.
    const pendingPeek = (pendingInputs[sysId] || [])[0];
    // Server-truth duration (stream-realtime wave): tool_result may carry
    // `elapsed_ms`, paired by toolUseId AT THE EMITTER — immune to SSE burst
    // delivery. When present it is the duration shown everywhere; the browser
    // receipt-clock pairing (Date.now() - t0) is ONLY the deploy-skew
    // fallback, so the 0.1s-✓ artifact (start+result delivered in one burst)
    // is impossible with a stamping backend.
    const serverElapsed =
      typeof data.elapsed_ms === 'number' && Number.isFinite(data.elapsed_ms) && data.elapsed_ms >= 0
        ? data.elapsed_ms
        : undefined;
    // Effective duration for classification/display: server truth first, the
    // browser receipt-clock pairing as the deploy-skew fallback.
    const effElapsed =
      serverElapsed ??
      (pendingPeek?.t0 != null
        ? Date.now() - pendingPeek.t0
        : row?.t0 != null
          ? Date.now() - row.t0
          : undefined);
    // Honest per-call outcome — client-side mirror of the server's wasted-call
    // markers plus the timeout/partial layer: a failed/empty envelope whose
    // call exceeded the budget is a TIMEOUT ("request timed out", never
    // "no data returned"); the SiteWise budget marker is 'partial'.
    const classified = classifyToolResultDetailed(summary, effElapsed, data.input, tool);
    const outcome = classified.outcome;
    const outcomeReason = classified.reason;
    if (row) {
      row.status = 'ok';
      row.elapsedMs = serverElapsed ?? Date.now() - (row.t0 || Date.now());
      row.outcome = outcome;
      if (outcome === 'failed') row.errorLine = errorFirstLine(summary);
      else if (outcome === 'timeout') row.errorLine = timeoutText(row.elapsedMs);
      else if (outcome === 'partial') row.errorLine = partialNote(summary);
      if (outcome !== 'success' && outcomeReason) row.outcomeReason = outcomeReason;
    }
    hudInFlight = Math.max(0, hudInFlight - 1);

    // Retry heuristic: this call targets a system that already FAILED in the
    // current question — badge its trace step(s) as a retry.
    const isRetry = failedSystems.has(sysId);
    if (outcome === 'failed' || outcome === 'timeout') failedSystems.add(sysId);
    else if (isRetry && outcome === 'success') markPreviousFailuresRecovered(sysId, row);

    // rawInput: object → first value (the SQL); string is the raw result JSON.
    let rawInput = '';
    if (data.input && typeof data.input === 'object' && !Array.isArray(data.input)) {
      rawInput = String(Object.values(data.input as Record<string, unknown>)[0] || '');
    }
    const edgeLabel = edgeLabelFromInput(rawInput);
    const noResults = isNoResults(summary, tool);

    // Ensure the dedup'd system node exists — tool_start may never have been
    // received (parallel gateway batching).
    addNode(sysId, inv.nodeType, sysLabel, '');
    if (!systemNodeTools.has(sysId)) systemNodeTools.set(sysId, tool);
    addEdgeInternal(currentQuestionId, sysId, 'queries');

    // Shift THIS call's pending entry (FIFO) — BOTH branches, EVEN ON ERROR,
    // so a failed call consumes its own qinput/placeholder and can never
    // misbind the NEXT result to the wrong question node.
    const isQueryable = inv.nodeType === 'system' || inv.nodeType === 'neptune';
    const queue = pendingInputs[sysId] || (pendingInputs[sysId] = []);
    const pending = isQueryable ? queue.shift() : undefined;
    // Correlation key: pairs this result with its HUD row + trace entry +
    // minted graph nodes (tool_start's hudId; mint-on-miss falls back to the
    // matched running row's id).
    const callKey = pending?.callKey ?? row?.id;
    // The finished call's path stops flowing (question→system handled above).
    if (pending) pending.flowEdgeIds.forEach((id) => flowingEdges.delete(id));
    // Failed/timed-out calls keep an HONEST mark in the graph: the in-flight
    // placeholder CONVERTS to a small persistent error node (red border,
    // label 'failed'/'timed out', clickable → rich modal with the error)
    // instead of vanishing silently. Successful/empty/partial results consume
    // the placeholder as before — their real result nodes take its place.
    const failedLike = outcome === 'failed' || outcome === 'timeout';
    if (pending?.placeholderId) {
      inflightPlaceholders.delete(pending.placeholderId);
      if (!failedLike) removeNodeAndEdges(pending.placeholderId);
    }

    // The result hangs off THIS request's blue input node; mint-on-miss when
    // tool_start was never received (parallel gateway batching).
    let resultParent = sysId;
    if (isQueryable && !failedLike) {
      let inputId = pending?.inputId;
      if (!inputId) {
        const nodeNow = nodes.find((n) => n.id === sysId);
        const reqTxt =
          inv.nodeType === 'neptune'
            ? (nodeNow?._lastKgQuestion as string) || ''
            : (nodeNow?._lastRequest as string) || '';
        let qText = reqTxt ? String(reqTxt).replace(/\s+/g, ' ').trim() : 'query';
        if (qText.length > 90) qText = qText.slice(0, 90).trimEnd() + '…';
        inputId = `qinput-${++nodeIdCounter}`;
        addNode(inputId, 'qinput', qText, reqTxt);
        addEdgeInternal(sysId, inputId, 'asks');
        const qn = nodes.find((n) => n.id === inputId);
        if (qn && callKey) qn._callKey = callKey;
      }
      resultParent = inputId;
    }

    // Aggregate this call's result for the click-to-open modal (I5); skip
    // self-recovered transient errors so they don't pollute the modal.
    const aggNode = nodes.find((n) => n.id === sysId);
    // A successful call with typed telemetry lines whose reply holds a table
    // (its line reads past the table, readsPastTable) takes its line from the
    // lines instead (the drill-down shows them under it). A call the engine's
    // typed no-data line says came back with no rows reads "No matching data"
    // (engineNoRowsLine), whatever its summary would say. Every other line
    // stays: a partial, failed or timed-out outcome, a "No matching data" or
    // prose line, and every call without a typed signal.
    const telemetryLines = telemetryLinesOf(data);
    const readsLine =
      outcome === 'success' && _readsPastTable(tool, summary)
        ? telemetryReadsLine(telemetryLines.telemetryCoverage)
        : '';
    const typedLine = readsLine || engineNoRowsLine(summary, outcome);
    const resultDesc =
      typedLine ||
      _describeToolResult(tool, summary, pending?.inputEnglish || (aggNode?._lastRequest as string) || '', data.input);
    // Full per-call drill-down payload — EVERYTHING the frontend received
    // for this call (tool_start input from the pending entry; tool_result
    // summary + raw payload; timing; the question it served).
    const call: InvCallDetail = {
      tool,
      system: sysLabel.replace(/\n/g, ' '),
      question: questionCounter,
      questionText: activeQuestion,
      inputEnglish: pending?.inputEnglish || (aggNode?._lastRequest as string) || '',
      inputRaw: pending?.inputRaw || '',
      resultSummary: summary,
      ...telemetryLines,
      resultEnglish: resultDesc,
      resultRaw: rawPayloadText(data.input),
      startedAt: pending ? (pending.serverTs ?? pending.t0) : undefined,
      elapsedMs: serverElapsed ?? (pending ? Date.now() - pending.t0 : undefined),
      outcome,
      ...(outcome !== 'success' && outcomeReason ? { outcomeReason } : {}),
    };
    if (!failedLike && aggNode) {
      const results = (aggNode._results as string[]) || [];
      results.push(resultDesc);
      aggNode._results = results;
      const bodies = (aggNode._fullBodies as string[]) || [];
      bodies.push(summary);
      aggNode._fullBodies = bodies;
      const calls = (aggNode._calls as InvCallDetail[]) || [];
      calls.push(call);
      aggNode._calls = calls;
    }
    // The blue qinput node carries the call payload too (click → drill-down).
    // Failed/timed-out calls attach to their qinput as well — the request
    // that failed is part of the honest record.
    if (resultParent !== sysId) attachCall(resultParent, call, callKey);
    else if (failedLike && pending?.inputId) attachCall(pending.inputId, call, callKey);

    // Trace-entry id of THIS call (empty-capable branches assign it) — the
    // empty→failed-when-retried upgrade references it retroactively.
    let traceId: number | undefined;

    if (failedLike) {
      // Honest failure rendering: the placeholder (if any) CONVERTS in place
      // to a small persistent error node — red border, label 'failed' /
      // 'timed out', clickable → rich modal with the error. Mint-on-miss
      // (tool_start never received) hangs a fresh error node off the system.
      const errLabel = outcome === 'timeout' ? 'timed out' : 'failed';
      const saveError = toolHalf(tool).startsWith('save_')
        ? _describeToolResult(tool, summary, pending?.inputEnglish || '', data.input)
        : '';
      // SPLIT (trace de-truncation): the trace row and the error-node detail
      // carry the FULL first line (first-line-only splitting stays; the
      // length cap goes) — the HUD `row.errorLine` above keeps the 200 cap.
      const errText =
        outcome === 'timeout'
          ? timeoutText(effElapsed)
          : saveError || errorFirstLine(summary, Number.POSITIVE_INFINITY) || 'Call failed';
      const phId = pending?.placeholderId ?? null;
      let errNode = phId ? nodes.find((n) => n.id === phId) : undefined;
      if (!errNode) {
        const errId = `failed-${++nodeIdCounter}`;
        addNode(errId, 'finding', errLabel, errText);
        addEdgeInternal(sysId, errId, errLabel);
        errNode = nodes.find((n) => n.id === errId);
      }
      if (errNode) {
        errNode.label = errLabel;
        errNode.detail = errText;
        errNode.outcome = outcome;
        delete errNode._inflight;
        errNode._call = call;
        if (callKey) errNode._callKey = callKey;
      }
      // The trace reports it as what it is: 'Request timed out after Ns' for
      // timeouts (never 'No data returned'), the error first line otherwise.
      addTrace('discovery', sysLabel, errText, sysId, {
        outcome,
        outcomeReason,
        retry: isRetry,
        callKey,
        labelTool: tool,
      });
    } else if (noResults) {
      const shortSys = shortSystemLabel(sysLabel, 'vendor');
      const id = `finding-empty-${++nodeIdCounter}`;
      addNode(
        id,
        'finding',
        `${shortSys}\nNo matching data`,
        rawInput ? `Query: ${rawInput}\n\nResult: No matching data found.` : '',
      );
      findingLabelSources.set(id, { tool, form: 'vendor', tail: 'No matching data' });
      addEdgeInternal(resultParent, id, edgeLabel || 'no results');
      attachCall(id, call, callKey);
      traceId = addTrace('discovery', sysLabel, rawInput ? `No results for: ${rawInput}` : 'No data returned', sysId, {
        outcome: 'empty',
        outcomeReason: outcomeReason || 'the tool reported no matching data',
        retry: isRetry,
        callKey,
        labelTool: tool,
      });
    } else if (isExplore) {
      // Entity parse may add nothing when the summary is scalar rows (counts,
      // property values) rather than 'Label: name' lines — previously that
      // rendered NOTHING under the Knowledge Graph hub even though the call
      // succeeded (observed live: KG consulted, tab showed no result). Fall back
      // to a compact finding node so every KG read is visible + clickable.
      // Task 1F: entities/findings attach to THIS call's qinput (resultParent).
      const entityIds = parseNeptuneEntities(summary, resultParent);
      entityIds.forEach((id) => attachCall(id, call, callKey));
      if (!entityIds.length) {
        const id = `finding-kg-${++nodeIdCounter}`;
        // KEEP the 80 cap (trace de-truncation audit): this brief is the
        // graph-node LABEL only — the trace row below carries the full
        // `summary.trim()` and the modal keeps the full summary as `detail`.
        // The summary's lines are joined as plain text (plainSummaryText), so
        // no line's list marker is left in the middle of the label, and a
        // summary that is only a table or code still gives its text.
        let brief = _plainSummaryText(String(summary)).replace(/\s+/g, ' ').trim();
        if (brief.length > 80) brief = brief.slice(0, 80).trimEnd() + '…';
        addNode(id, 'finding', brief ? `Knowledge Graph\n${brief}` : 'Knowledge Graph', summary);
        addEdgeInternal(resultParent, id, 'returned');
        attachCall(id, call, callKey);
      }
      // Trace heading carries the actual KG question (stashed at tool_start).
      // Blank summaries render an explicit 'No data returned' body (never a
      // bare header), tagged with the honest 'empty' outcome.
      const kgQ = (aggNode?._lastKgQuestion as string) || '';
      traceId = addTrace('discovery', kgQ ? `${sysLabel} — ${kgQ}` : sysLabel, summary.trim() || 'No data returned', sysId, {
        outcome: outcome !== 'success' ? outcome : undefined,
        outcomeReason,
        retry: isRetry,
        callKey,
        labelTool: tool,
        labelSuffix: kgQ ? ` — ${kgQ}` : '',
      });
    } else if (inv.nodeType === 'neptune') {
      // A knowledge-graph WRITE (save_correlation / save_* learning write-back).
      // Previously fell through to parseSqlFindings which matched nothing, so the
      // graph's learning loop was invisible in the investigation view.
      const verb = tool.indexOf('___') !== -1 ? tool.split('___')[1] : tool || 'save';
      const id = `finding-kgwrite-${++nodeIdCounter}`;
      // SPLIT (trace de-truncation): the graph-node label sub-line keeps the
      // 80-char cap (`brief`); the TRACE row carries the uncapped text —
      // the phrased resultDesc when available, else the full collapsed
      // summary (the SHOW MORE clamp handles length in the trace).
      const kgWriteTraceText = resultDesc || String(summary).replace(/\s+/g, ' ').trim();
      let brief = kgWriteTraceText;
      if (brief.length > 80) brief = brief.slice(0, 80).trimEnd() + '…';
      const filtered = outcome === 'filtered';
      addNode(id, 'finding', `${filtered ? 'Learning filtered' : `Learned (${verb.replace(/^save_/, '')})`}\n${brief}`, summary);
      const writeNode = nodes.find((n) => n.id === id);
      if (writeNode && filtered) writeNode.outcome = 'filtered';
      addEdgeInternal(resultParent, id, filtered ? 'filtered' : 'saved');
      attachCall(id, call, callKey);
      addTrace('decision', sysLabel, kgWriteTraceText, sysId, {
        outcome: filtered ? 'filtered' : undefined,
        retry: isRetry,
        callKey,
        labelTool: tool,
      });
    } else {
      const findingId = parseSqlFindings(
        summary,
        resultParent,
        sysLabel,
        rawInput,
        edgeLabel,
        tool,
        (aggNode?._lastRequest as string) || '',
        typedLine,
      );
      attachCall(findingId, call, callKey);
      if (findingId) {
        _g._lastFindingId = findingId;
        // A budget-stop partial keeps its real data node, tagged amber.
        if (outcome === 'partial') {
          const fn = nodes.find((n) => n.id === findingId);
          if (fn) fn.outcome = 'partial';
        }
      } else if (outcome === 'partial') {
        // Partial marker with no parsable records line — an honest amber
        // partial node instead of nothing in the graph.
        const shortSys = shortSystemLabel(sysLabel, 'flat');
        const pid = `finding-partial-${++nodeIdCounter}`;
        addNode(pid, 'finding', `${shortSys}\npartial`, summary);
        findingLabelSources.set(pid, { tool, form: 'flat', tail: 'partial' });
        const pn = nodes.find((n) => n.id === pid);
        if (pn) pn.outcome = 'partial';
        addEdgeInternal(resultParent, pid, 'partial');
        attachCall(pid, call, callKey);
      }
      // Blank summaries previously rendered a BARE trace header (evidence:
      // steps 12/13) — now an explicit 'No data returned' body + 'empty' tag.
      traceId = addTrace('discovery', sysLabel, summary.trim() || 'No data returned', sysId, {
        outcome: outcome !== 'success' ? outcome : undefined,
        outcomeReason,
        retry: isRetry,
        callKey,
        labelTool: tool,
      });
    }

    // Record this question's 'empty' completion for the retry verdict — a
    // LATER call to the same system (tool_start) upgrades the HUD row and the
    // trace entry to failed (no payload) via upgradeEmptiesRetried.
    if (outcome === 'empty') {
      (emptyAwaitingRetry[sysId] = emptyAwaitingRetry[sysId] || []).push({
        hudId: row?.id,
        traceId,
      });
    }
  }

  function onDone(): void {
    // Task 1H: the turn settled (success OR the error-path done from
    // useAskStream) — the idle-but-live pulse must never stick.
    investigationActive = false;
    // Stop all still-running HUD timers and mark them complete (gray).
    hud.forEach((h) => {
      if (h.kind === 'tool' && h.status === 'running') {
        h.status = 'stopped';
        h.elapsedMs = Date.now() - (h.t0 || Date.now());
      }
    });
    flowingEdges.clear();
    // Crashed/aborted calls never got a tool_result — sweep their pulsing
    // placeholders (useAskStream routes stream errors/aborts through done).
    cleanupInflightPlaceholders();
  }

  function onIntelligenceSummary(data: Record<string, unknown>): void {
    const s = data as IntelligenceSummary;
    lastIntelligence = s;
    const rules = s.rules_applied || [];
    addTrace(
      'discovery',
      'Intelligence',
      `${s.total_tool_calls || 0} tool calls, ${rules.length} learned rules applied${
        s.correlation_writebacks ? `, ${s.correlation_writebacks} correlation findings saved` : ''
      }${s.new_proposed_mappings ? `, ${s.new_proposed_mappings} new optimizations` : ''}`,
    );
    // Render the intelligence score into the chat response area (element
    // construction — zero-innerHTML append to
    // #response-area). Guarded for DOM-free (unit-test) environments and a
    // not-yet-mounted chat panel.
    if (typeof document === 'undefined') return;
    const responseEl = document.getElementById('response-area');
    if (!responseEl) return;
    // "Used in this answer" (fired) — the ONE rules-applied wording, shared
    // with the chat analysis panel (lib/metric-copy.ts, T1.6).
    const RULES_TIP = RULES_APPLIED_TIP;
    const COVERAGE_TIP =
      "Share of this query's tool calls that were knowledge-graph traversals. Informational only — broad cross-system questions legitimately use many direct system queries; this is not a quality score.";

    const box = document.createElement('div');
    box.style.cssText =
      'margin-top:1rem;padding:0.75rem;background:var(--surface2);border:1px solid var(--border);border-radius:8px;font-size:0.8rem;color:var(--text);';

    const heading = document.createElement('div');
    heading.title = 'Deterministic, server-computed counts for this query.';
    heading.style.cssText = 'cursor:help;font-weight:600;margin-bottom:0.5rem;color:var(--accent);';
    heading.textContent = '📊 Query Intelligence';
    box.appendChild(heading);

    const toolsLine = document.createElement('div');
    toolsLine.append('Tool calls: ');
    const toolsStrong = document.createElement('strong');
    toolsStrong.textContent = String(s.total_tool_calls || 0);
    toolsLine.append(toolsStrong, ` (${s.neptune_queries || 0} Neptune, ${s.system_queries || 0} system queries)`);
    box.appendChild(toolsLine);

    const rulesLine = document.createElement('div');
    rulesLine.title = RULES_TIP;
    rulesLine.style.cssText = 'cursor:help;';
    rulesLine.append('Learned rules applied: ');
    const rulesStrong = document.createElement('strong');
    rulesStrong.textContent = String(rules.length);
    rulesLine.append(rulesStrong);
    box.appendChild(rulesLine);

    if ((s.correlation_writebacks || 0) > 0) {
      const correlationsLine = document.createElement('div');
      correlationsLine.title =
        'Cross-system correlation findings successfully saved for human review during this query; these are writes, not applied rules.';
      correlationsLine.style.cssText = 'cursor:help;';
      correlationsLine.append('Correlation findings saved: ');
      const correlationsStrong = document.createElement('strong');
      correlationsStrong.textContent = String(s.correlation_writebacks);
      correlationsLine.append(correlationsStrong);
      box.appendChild(correlationsLine);
    }

    if (rules.length > 0) {
      // Native <details> expansion. Labels render via textContent — text
      // nodes, inherently escaped (zero-innerHTML policy).
      const details = document.createElement('details');
      details.style.cssText = 'margin-top:0.25rem;';
      const rulesSummary = document.createElement('summary');
      rulesSummary.style.cssText = 'cursor:pointer;color:var(--muted);';
      rulesSummary.textContent = 'show rules';
      details.appendChild(rulesSummary);
      const list = document.createElement('ul');
      list.style.cssText = 'margin:0.25rem 0 0;padding-left:1.25rem;';
      for (const r of rules) {
        const li = document.createElement('li');
        li.textContent = String((r && (r.label || r.id)) || '');
        list.appendChild(li);
      }
      details.appendChild(list);
      box.appendChild(details);
    }

    if (s.graph_coverage_pct !== undefined) {
      // Deploy-skew guard: only new runtime payloads carry graph_coverage_pct
      // — an old payload renders without this line rather than with stale
      // semantics.
      const coverageLine = document.createElement('div');
      coverageLine.title = COVERAGE_TIP;
      coverageLine.style.cssText = 'cursor:help;';
      coverageLine.textContent = `Graph coverage: ${s.neptune_queries || 0}/${s.total_tool_calls || 0} knowledge-graph queries`;
      box.appendChild(coverageLine);
    }

    if ((s.wasted_calls || 0) > 0) {
      const wastedLine = document.createElement('div');
      wastedLine.textContent = `Exploration steps with no matching data: ${s.wasted_calls || 0}`;
      box.appendChild(wastedLine);
    }

    if ((s.new_proposed_mappings || 0) > 0) {
      const opt = document.createElement('div');
      opt.style.cssText =
        'margin-top:0.5rem;padding:0.4rem;background:var(--surface3);border:1px solid var(--amber);border-radius:4px;';
      opt.append('⚡ ');
      const optStrong = document.createElement('strong');
      optStrong.textContent = String(s.new_proposed_mappings);
      opt.append(
        optStrong,
        ` new optimization${(s.new_proposed_mappings || 0) > 1 ? 's' : ''} discovered. `,
      );
      // The inline onclick="switchView(...)" became data-nav (jsx-migration
      // W2.3, CSP-safe) — this module owns the element, so it registers the
      // navigation listener itself. Same anchor convention as thread-clicks.ts.
      const link = document.createElement('a');
      link.href = '#';
      link.dataset.nav = 'self-learning';
      link.style.cssText = 'color:var(--amber);text-decoration:underline;';
      link.textContent = 'Review in Self-Learning Discoveries →';
      link.addEventListener('click', (ev) => {
        ev.preventDefault();
        window.switchView?.('architecture');
        document.querySelector<HTMLElement>('[data-arch-tab="proposed"]')?.click();
      });
      opt.append(link);
      box.appendChild(opt);
    }

    responseEl.appendChild(box);
  }

  /** Rebuild every system-named string from the tool it came from (label
   * provenance above), so names the catalog supplies or renames AFTER a call
   * replace the old label everywhere at once: system nodes (and their ×N base
   * label), finding nodes, HUD rows, trace steps, tool_start narratives and
   * the drill-down modal's call metadata. Returns whether anything changed. */
  function relabelSystems(): boolean {
    let changed = false;
    const labelOf = (tool: string) => deriveInvNode(tool).label;
    for (const n of nodes) {
      const sysTool = systemNodeTools.get(n.id);
      if (sysTool) {
        const base = labelOf(sysTool);
        if (n._baseLabel !== undefined && n._baseLabel !== base) {
          n._baseLabel = base;
          changed = true;
        }
        const label = composeInvLabel(base, (n._totalQueries as number) || 0, '');
        if (n.label !== label) {
          n.label = label;
          changed = true;
        }
      }
      const finding = findingLabelSources.get(n.id);
      if (finding) {
        const label = `${shortSystemLabel(labelOf(finding.tool), finding.form)}\n${finding.tail}`;
        if (n.label !== label) {
          n.label = label;
          changed = true;
        }
      }
      const calls = [n._call as InvCallDetail | undefined, ...((n._calls as InvCallDetail[]) || [])];
      for (const call of calls) {
        if (!call || !call.tool) continue;
        const system = labelOf(call.tool).replace(/\n/g, ' ');
        if (call.system !== system) {
          call.system = system;
          changed = true;
        }
      }
    }
    for (const h of hud) {
      if (h.kind !== 'tool' || !h.tool) continue;
      const label = labelOf(h.tool);
      if (h.label !== label) {
        h.label = label;
        changed = true;
      }
    }
    for (const t of trace) {
      const src = traceLabelSources.get(t.id);
      if (src) {
        const label = labelOf(src.tool) + src.suffix;
        if (t.label !== label) {
          t.label = label;
          changed = true;
        }
      }
      const narr = traceNarrativeSources.get(t.id);
      if (narr) {
        const text = narr.prefix + toolStartNarrative(narr.tool, narr.input, labelOf(narr.tool));
        if (t.text !== text) {
          t.text = text;
          changed = true;
        }
      }
    }
    return changed;
  }

  // The Architecture view subscribes to THIS store only; names arriving in
  // the shared resolver reach it through the relabel + notify here. Kept so
  // dispose() can stop it (test-made stores; the app's singleton lives for
  // the page).
  const unsubscribeNames = subscribeSystemNames(() => {
    if (relabelSystems()) notify();
  });

  return {
    addNodes(added) {
      const known = new Set(nodes.map((n) => n.id));
      const fresh = added.filter((n) => !known.has(n.id));
      if (!fresh.length) return;
      nodes = [...nodes, ...fresh];
      notify();
    },
    addEdge(edge) {
      edges = [...edges, edge];
      notify();
    },
    reset() {
      // Port of resetArchitectureState — clears everything the view derives.
      nodes = [];
      edges = [];
      trace = [];
      hud = [];
      nodeIdCounter = 0;
      traceStepCounter = 0;
      questionCounter = 0;
      currentQuestionId = 'question-root';
      activeQuestion = '';
      activeQuestionFilter = null;
      flowingEdges = new Set();
      lastIntelligence = null;
      hudInFlight = 0;
      investigationActive = false;
      pendingInputs = {};
      pendingHeartbeats = new Map();
      failedSystems = new Set();
      emptyAwaitingRetry = {};
      inflightPlaceholders = new Set();
      systemNodeTools.clear();
      findingLabelSources.clear();
      traceLabelSources.clear();
      traceNarrativeSources.clear();
      notify();
      emit('reset', {});
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getState() {
      return snapshot;
    },
    handleArchEvent(eventType, data = {}) {
      if (eventType === 'user_msg') onUserMsg(data);
      else if (eventType === 'tool_start') onToolStart(data);
      else if (eventType === 'tool_result') onToolResult(data);
      else if (eventType === 'reasoning') onReasoning(data);
      else if (eventType === 'done') onDone();
      else if (eventType === 'intelligence_summary') onIntelligenceSummary(data);
      else {
        // Unknown event (e.g. tool_end / metadata) — pass through to event
        // subscribers (map animations) without touching state.
        emit(eventType, data);
        return;
      }
      notify();
      emit(eventType, data);
    },
    subscribeEvents(listener) {
      eventListeners.add(listener);
      return () => eventListeners.delete(listener);
    },
    setQuestionFilter(q) {
      activeQuestionFilter = q;
      notify();
    },
    dispose() {
      unsubscribeNames();
    },
  };
}

/** Shared singleton — one investigation graph per page (session-scoped). */
export const archStore: ArchStore = createArchStore();

/** Function-shaped bridge, mirroring the old window-global call sites
 * (`handleArchEvent(type, data)`). */
export function handleArchEvent(eventType: string, data?: Record<string, unknown>): void {
  archStore.handleArchEvent(eventType, data);
}

/** Port of resetArchitectureState — called by startNewSession ('+ New'). */
export function resetArchitectureState(): void {
  archStore.reset();
}
