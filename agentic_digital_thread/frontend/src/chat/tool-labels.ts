// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tool-pill / graph-node label helpers and tool-input distillation
 * (formatToolInput / getSystemLabel / getToolLabel).
 * Pure string helpers (no DOM, no HTML). The tool-input BLOCK renders as JSX
 * in <ToolInputBlock/> (ToolInputBlock.tsx) — the renderToolInputBlock
 * HTML-string builder was removed in the zero-innerHTML migration. */
import { catalogSystemName, systemName } from '../lib/system-names';
import { parseToolIdentity } from '../lib/tool-identity';

export interface FormattedToolInput {
  summary: string;
  raw: string;
}

/** Distil a tool call's input arguments into a readable one-liner + the full
 * raw payload. Generic across tools/systems — no hardcoded plant/system names.
 * Picks the most meaningful field by a case-insensitive priority list (the NL
 * sub-question in Mode A, the SQL in Mode B, the explore_graph filter, etc.),
 * falling back to the first string value, then a JSON of the scalar keys. */
export function formatToolInput(_tool: string, input: unknown): FormattedToolInput {
  if (!input || typeof input !== 'object' || Array.isArray(input)) {
    return {
      summary: String(input == null ? '' : input).replace(/\s+/g, ' ').trim(),
      raw: '',
    };
  }
  const obj = input as Record<string, unknown>;
  const PRIORITY = ['question', 'sub_question', 'subquestion', 'query', 'sql',
    'statement', 'filter', 'expression', 'cypher', 'gremlin', 'prompt', 'text', 'input'];
  const lowerMap: Record<string, string> = {};
  for (const k of Object.keys(obj)) lowerMap[k.toLowerCase()] = k;
  let val: unknown;
  for (const key of PRIORITY) {
    const actual = lowerMap[key];
    if (actual != null && obj[actual] != null && String(obj[actual]).trim()) {
      val = obj[actual];
      break;
    }
  }
  if (val == null) {
    for (const k of Object.keys(obj)) {
      if (typeof obj[k] === 'string' && (obj[k] as string).trim()) {
        val = obj[k];
        break;
      }
    }
  }
  if (val == null) {
    const scalars: Record<string, unknown> = {};
    for (const k of Object.keys(obj)) {
      const v = obj[k];
      if (v == null || typeof v !== 'object') scalars[k] = v;
    }
    val = Object.keys(scalars).length ? JSON.stringify(scalars) : '';
  }
  let summary = String(val == null ? '' : val).replace(/\s+/g, ' ').trim();
  if (summary.length > 200) summary = summary.slice(0, 200).trimEnd() + '…';
  const raw = Object.keys(obj).length ? JSON.stringify(obj, null, 2) : '';
  return { summary, raw };
}

/** Display cap for the derived "Routing context" block, in characters. A
 * normal ROUTE block is a few hundred characters (the server-side system
 * agent caps kg_context at 2,000), but the trace holds the ORIGINAL
 * tool_start args, so a malformed or model-produced value can be megabytes.
 * Only the derived display is capped; raw args stays byte-identical. */
export const ROUTING_CONTEXT_MAX_CHARS = 4000;

export interface RoutingContext {
  /** Display copy: outer whitespace trimmed, the code fence the block is
   * wrapped in at its source dropped (see `unfencedBounds`), CRLF / lone CR
   * as LF, at most ROUTING_CONTEXT_MAX_CHARS characters. */
  text: string;
  /** True when the value was longer than the cap and `text` is its head. */
  truncated: boolean;
}

/** Longest opening fence line (backticks plus info string) recognised. */
const FENCE_LINE_MAX = 200;

/** [start, end) narrowed past outer whitespace. */
function trimBounds(v: string, start: number, end: number): [number, number] {
  let s = start;
  let e = end;
  while (s < e && /\s/.test(v[s])) s += 1;
  while (e > s && /\s/.test(v[e - 1])) e -= 1;
  return [s, e];
}

/** The opening code fence at `start`: a first line of three or more
 * backticks or tildes and an optional info string (without backticks, for a
 * backtick fence). Returns the index just past that line, the fence
 * character and its length, else null. */
function openingFence(
  v: string,
  start: number,
  end: number,
): { after: number; mark: string; ticks: number } | null {
  const mark = v[start];
  if (mark !== '`' && mark !== '~') return null;
  let i = start;
  while (i < end && v[i] === mark) i += 1;
  const ticks = i - start;
  if (ticks < 3) return null;
  const limit = Math.min(end, start + FENCE_LINE_MAX);
  while (i < limit && v[i] !== '\n' && v[i] !== '\r') {
    if (mark === '`' && v[i] === '`') return null;
    i += 1;
  }
  if (i === end) return { after: end, mark, ticks };
  if (v[i] !== '\n' && v[i] !== '\r') return null;
  return { after: v[i] === '\r' && v[i + 1] === '\n' ? i + 2 : i + 1, mark, ticks };
}

/** Start of the closing code-fence line ending at `end`: a last line of at
 * least `ticks` of the opening fence's character `mark`, after optional
 * spaces or tabs. Else null. */
function closingFence(v: string, start: number, end: number, mark: string, ticks: number): number | null {
  let i = end;
  while (i > start && v[i - 1] === mark) i -= 1;
  if (end - i < ticks) return null;
  while (i > start && (v[i - 1] === ' ' || v[i - 1] === '\t')) i -= 1;
  return i === start || v[i - 1] === '\n' || v[i - 1] === '\r' ? i : null;
}

/** [start, end) of the shown part of a routing-context value: outer
 * whitespace trimmed, then the fence the block is wrapped in at its source
 * dropped, i.e. a leading fence line (with an optional info string such as
 * `ROUTE:`) and, after it, a trailing fence line. Backticks anywhere else,
 * and unfenced text, stay as written. Index arithmetic only: a huge value is
 * never copied. */
function unfencedBounds(v: string): [number, number] {
  const [start, end] = trimBounds(v, 0, v.length);
  const open = openingFence(v, start, end);
  if (!open) return [start, end];
  return trimBounds(v, open.after, closingFence(v, open.after, end, open.mark, open.ticks) ?? end);
}

/** A routing-context value as a bounded display copy with real line breaks;
 * null when it has no visible content. A huge value is never copied whole:
 * only a bounded window is sliced, normalised and capped. */
function boundedRoutingText(v: string): RoutingContext | null {
  const [start, end] = unfencedBounds(v);
  if (start >= end) return null;
  // CRLF normalisation can at most halve the length, so twice the cap
  // always holds enough source for a full display.
  const stop = Math.min(end, start + 2 * ROUTING_CONTEXT_MAX_CHARS);
  const text = v.slice(start, stop).replace(/\r\n?/g, '\n');
  if (stop === end && text.length <= ROUTING_CONTEXT_MAX_CHARS) return { text, truncated: false };
  let cut = ROUTING_CONTEXT_MAX_CHARS;
  // Never split a surrogate pair at the cut.
  const last = text.charCodeAt(cut - 1);
  if (last >= 0xd800 && last <= 0xdbff) cut -= 1;
  return { text: text.slice(0, cut), truncated: true };
}

/** The call's own `kg_context` argument (key matched case-insensitively),
 * when it is a string; else undefined. */
function modelKgContext(input: unknown): string | undefined {
  if (!input || typeof input !== 'object' || Array.isArray(input)) return undefined;
  const obj = input as Record<string, unknown>;
  for (const k of Object.keys(obj)) {
    if (k.toLowerCase() === 'kg_context' && typeof obj[k] === 'string') return obj[k] as string;
  }
  return undefined;
}

/** The routing context sent with a system query call: `sent` (the
 * tool_result's `route_context`, what the gateway sent the system after
 * merging route-seed facts in) when it has visible content, else the call's
 * `kg_context` argument (key matched case-insensitively), i.e. the fenced
 * `ROUTE:` block with its `join_rule:` line, as a bounded display copy with
 * real line breaks. null when neither has visible content. */
export function routingContextOf(input: unknown, sent?: string): RoutingContext | null {
  if (typeof sent === 'string') {
    const shown = boundedRoutingText(sent);
    if (shown) return shown;
  }
  const own = modelKgContext(input);
  return own === undefined ? null : boundedRoutingText(own);
}

/** True when the sent routing context differs from the model's own
 * `kg_context` (the knowledge graph filled lines the model did not write).
 * False when the sent value shows nothing (only fence lines, say): the
 * display then falls back to the model's own context, so no shown line came
 * from the knowledge graph. */
export function routingContextAdded(input: unknown, sent?: string): boolean {
  if (typeof sent !== 'string' || !boundedRoutingText(sent)) return false;
  return sent.trim() !== (modelKgContext(input) ?? '').trim();
}

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
  const identity = parseToolIdentity(toolName);
  if (identity.shape === 'gateway' || identity.shape === 'query') {
    return systemName(identity.systemId) || toolName;
  }
  return toolName;
}

/** The reserved gateway target that hosts the knowledge-graph tools
 * (agents/digital_thread_server.py, SYSTEM_ID=digital-thread). */
const KNOWLEDGE_GRAPH_TARGET = 'digital-thread';

const KNOWLEDGE_GRAPH_OPS: Record<string, string> = {
  explore_graph: 'query',
  get_system_schema: 'system schema',
};

/** Sources-row label for a numbered citation. A knowledge-graph gateway call
 * reads "Knowledge graph · <what it did>" (the bare target id read "Digital
 * Thread", the same on every graph row); every other tool keeps
 * getSystemLabel. Label text only: numbering stays provenance D2. */
export function getCitationLabel(toolName: string): string {
  const identity = parseToolIdentity(toolName);
  const op = identity.operation ?? '';
  const isGraph =
    identity.shape === 'gateway' &&
    (identity.systemId === KNOWLEDGE_GRAPH_TARGET || op === 'explore_graph');
  if (!isGraph) return getSystemLabel(toolName);
  const what = KNOWLEDGE_GRAPH_OPS[op] ?? op.replace(/_/g, ' ').trim();
  return what ? `Knowledge graph · ${what}` : 'Knowledge graph';
}

/** A citation's fallback chip label. The label stored at tool_start is kept
 * (and rendered as given), except when the catalog now knows the cited
 * system: then the label is derived again at RENDER, so a chip names the
 * system by its catalog name even when the catalog arrived after the call
 * started (lib/system-names.ts). */
export function citationLabel(c: { tool: string; label: string }): string {
  const identity = parseToolIdentity(c.tool);
  const known =
    (identity.shape === 'gateway' || identity.shape === 'query') && catalogSystemName(identity.systemId);
  return (known && getCitationLabel(c.tool)) || c.label;
}

/** Same rule for a stored system label (the Agent Analysis share bar keeps
 * the label baked at tool_result): kept as given unless the catalog now
 * knows the system, then derived again at render. */
export function systemLabelNow(c: { tool: string; label: string }): string {
  const identity = parseToolIdentity(c.tool);
  const known =
    (identity.shape === 'gateway' || identity.shape === 'query') && catalogSystemName(identity.systemId);
  return (known && getSystemLabel(c.tool)) || c.label;
}

/** One-line system name for a gateway system id or local query target, via
 * the ONE shared resolver (lib/system-names.ts): the catalog display name
 * (whatever the catalog `name` says); for an id the catalog does not know, a
 * readable fallback ("qms-west" / "qms_west" → "QMS West"). Used by the
 * tool-pill HUD label and the chat activity line. */
export function systemDisplayName(systemId: string): string {
  return systemName(systemId);
}

/** Tool-pill HUD label (getToolLabel). */
export function getToolLabel(toolName: string): string {
  if (!toolName) return 'Tool';
  if (toolName === 'explore_graph') return 'Querying knowledge graph';
  if (toolName === 'get_system_schema') return 'Getting schema';
  const identity = parseToolIdentity(toolName);
  if (identity.shape === 'gateway') {
    const sys = identity.systemId ?? '';
    const fn = identity.operation ?? '';
    const fnLabel = fn.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
    return `${systemDisplayName(sys) || sys} — ${fnLabel}`;
  }
  return toolName.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

/** explore_graph pill relabel after tool_result — derives from the graph
 * query/filter (input args), falling back to the result text. A query that
 * names no known node label reads as a knowledge-graph query ("KG: Cypher
 * query"), like the routing lookup's "KG: <question>" pill. */
export function neptunePillLabel(labelSrc: string): string {
  // Match node LABELS in the Cypher pattern (`:AuthorityRule`), not bare words:
  // a `MATCH (n) RETURN labels(n)` count lists every type in its result, and
  // the bare-word match labelled it "Querying authority rules" (event 29).
  const s = labelSrc.toLowerCase();
  const has = (label: string) => new RegExp(`:\\s*\`?${label}\\b`).test(s);
  return has('correlation') ? 'Querying correlations'
    : has('decisionboundary') ? 'Querying decision boundaries'
    : has('authorityrule') ? 'Querying authority rules'
    : has('entity') ? 'Querying entities'
    : has('concept') ? 'Querying concepts'
    : 'KG: Cypher query';
}
