// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Activity-line model for the chat reasoning trace — no DOM, no HTML. The
 * phrases come from the i18n dictionary (`activity.*`, lib/i18n.ts), so the
 * line follows the language picker; everything else is pure.
 *
 * Back-to-back tool calls (no narration text between them, i.e. equal
 * `segBoundary`) fold into ONE activity line: a present-tense summary of the
 * calls still in flight while any runs ("Querying ERP and CMMS", 1 of 3),
 * then a past-tense summary of the whole group once all are done ("Searched
 * the knowledge graph · Queried ERP, CMMS and PLM", 4 calls · 3.1s). The
 * line expands to the per-call rows (<ToolActivityGroup/>), which keep the
 * `.tool-pill.inline` / `.tool-trace` DOM contract.
 *
 * Generic across tools and systems: names come from the tool identity
 * (tool-labels.ts systemDisplayName), never from a plant list. Outcomes are
 * the store's classifyToolResultDetailed verdict (ONE outcome fixture); a
 * call that finished without its own tool_result carries NO verdict. */
import type { CallOutcome } from '../lib/arch-store';
import { isExploreGraphTool } from '../lib/kg-label';
import { t as tr, tf } from '../lib/i18n';
import { parseToolIdentity } from '../lib/tool-identity';
import { formatToolInput, systemDisplayName } from './tool-labels';
import type { ToolPill, ToolTrace } from './turn-model';

/** Fold consecutive pills that share a segBoundary into groups. segBoundary
 * is non-decreasing in insertion order, so equal neighbours are exactly the
 * calls with no completed narration run between them. */
export function groupBackToBack(pills: readonly ToolPill[]): ToolPill[][] {
  const groups: ToolPill[][] = [];
  for (const pill of pills) {
    const last = groups[groups.length - 1];
    if (last && last[0].segBoundary === pill.segBoundary) last.push(pill);
    else groups.push([pill]);
  }
  return groups;
}

/** The knowledge-graph gateway target (agents/digital_thread_server.py). */
const KNOWLEDGE_GRAPH_TARGET = 'digital-thread';

/** Named non-query operations with their own phrases (i18n keys
 * `activity.op.<name>.<tense>`) — the knowledge-graph target's ops (gateway
 * shape) and the orchestrator's local tools (plain shape:
 * agents/orchestrator.py toolset). Anything else reads "Running <name>". */
const NAMED_OPS = new Set([
  'verify_relationship',
  'save_value_mapping',
  'save_correlation',
  'get_query_workflow',
  'get_client_instructions',
  'propose_metric_definition',
]);

export type Tense = 'present' | 'past' | 'stopped';

type TargetKind = 'graph' | 'schema' | 'system' | 'other';

interface ToolTarget {
  kind: TargetKind;
  /** system: display name; other: the operation/tool name. */
  name: string;
  /** other only: an unnamed generic op inside the knowledge-graph target. */
  graphOp?: boolean;
}

const words = (s: string) => s.replace(/_/g, ' ').replace(/\s+/g, ' ').trim();

function toolTarget(tool: string): ToolTarget {
  if (isExploreGraphTool(tool)) return { kind: 'graph', name: '' };
  const id = parseToolIdentity(tool);
  const op = id.shape === 'gateway' ? (id.operation ?? '') : tool;
  if (op === 'get_system_schema') return { kind: 'schema', name: '' };
  if (id.shape === 'gateway' && id.systemId === KNOWLEDGE_GRAPH_TARGET) {
    return { kind: 'other', name: op, graphOp: true };
  }
  if (id.shape === 'gateway' || id.shape === 'query') {
    return { kind: 'system', name: systemDisplayName(id.systemId ?? '') || tool };
  }
  return { kind: 'other', name: tool };
}

/** Phrase for a non-query operation in one tense. */
function opPhrase(t: ToolTarget, tense: Tense): string {
  if (NAMED_OPS.has(t.name)) return tr(`activity.op.${t.name}.${tense}`);
  const name =
    words(t.name) || tr(t.graphOp ? 'activity.op.unnamedGraphTool' : 'activity.op.unnamedTool');
  return tf(`activity.op.generic.${tense}`, { name });
}

/** "A" · "A and B" · "A, B and C" · "A, B and 3 more systems". */
function listNames(names: string[]): string {
  if (names.length <= 1) return names[0] ?? '';
  if (names.length === 2) return tf('activity.list.two', { a: names[0], b: names[1] });
  if (names.length === 3) {
    return tf('activity.list.three', { a: names[0], b: names[1], c: names[2] });
  }
  return tf('activity.list.more', { a: names[0], b: names[1], n: names.length - 2 });
}

const times = (n: number) => (n > 1 ? ` (${n}×)` : '');

/** One-line summary of a set of calls: present tense (in flight), past
 * tense (finished), or stopped (the turn ended mid-call by a Stop or an
 * error frame — EVERY clause says so:
 * "Stopped querying ERP · Stopped reading a schema"). Phrases come from the
 * i18n dictionary (`activity.*`), so the line follows the language picker. */
export function describeCalls(pills: readonly ToolPill[], tense: Tense): string {
  let graph = 0;
  let schema = 0;
  const systems = new Map<string, number>();
  const others = new Map<string, ToolTarget>();
  const order: string[] = [];
  const mark = (key: string) => {
    if (!order.includes(key)) order.push(key);
  };
  for (const pill of pills) {
    const t = toolTarget(pill.tool);
    if (t.kind === 'graph') {
      graph++;
      mark('graph');
    } else if (t.kind === 'schema') {
      schema++;
      mark('schema');
    } else if (t.kind === 'system') {
      systems.set(t.name, (systems.get(t.name) ?? 0) + 1);
      mark('system');
    } else {
      others.set(t.name, t);
      mark(`other:${t.name}`);
    }
  }
  const parts = order.map((key) => {
    if (key === 'graph') {
      const head = tr(`activity.graph.${tense}`);
      // A lone KG call keeps its question (pill.kgq label "KG: <question>").
      if (pills.length === 1 && pills[0].kgq) {
        const q = pills[0].label.replace(/^KG:\s*/, '').trim();
        if (q) return tf('activity.withQuestion', { action: head, question: q });
      }
      return head + times(graph);
    }
    if (key === 'schema') {
      return schema > 1
        ? tf(`activity.schemas.${tense}`, { n: schema })
        : tr(`activity.schema.${tense}`);
    }
    if (key === 'system') {
      const names = [...systems.keys()];
      const repeat = names.length === 1 ? times(systems.get(names[0]) ?? 1) : '';
      return tf(`activity.system.${tense}`, { systems: listNames(names) }) + repeat;
    }
    const target = others.get(key.slice('other:'.length));
    return target ? opPhrase(target, tense) : key;
  });
  return parts.join(' · ');
}

/** Per-call display state: running, a classified outcome, 'stopped' (the
 * turn ended mid-call by a Stop or an error frame), or 'done' (the call
 * finished via tool_end / done without its own result). Neither of the last
 * two carries a verdict. */
export type CallState = 'running' | 'done' | 'stopped' | CallOutcome;

export function callState(pill: ToolPill, trace: ToolTrace | undefined): CallState {
  if (!pill.done) return 'running';
  if (pill.stopped) return 'stopped';
  return trace?.outcome ?? 'done';
}

/** 0.4s · 4.2s · 12s · 1m 05s. */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '';
  const s = ms / 1000;
  if (s < 10) return `${s.toFixed(1)}s`;
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${String(Math.round(s - m * 60)).padStart(2, '0')}s`;
}

/** Wall time of a group of finished calls, or null when unknown. Uses the
 * server's `ts` stamps when EVERY call carries both ends (skew-free); else
 * the browser receipt clock. Never mixes the two clocks. */
export function groupSpanMs(resulted: readonly ToolTrace[]): number | null {
  if (!resulted.length) return null;
  const server = resulted.every((t) => t.serverStartTs != null && t.serverEndTs != null);
  const starts = resulted.map((t) => (server ? t.serverStartTs : t.startedAt));
  const ends = resulted.map((t) => (server ? t.serverEndTs : t.resultAt));
  if (starts.some((v) => v == null) || ends.some((v) => v == null)) return null;
  const span = Math.max(...(ends as number[])) - Math.min(...(starts as number[]));
  return span > 0 ? span : null;
}

/** Visible text of one expanded call row: the target plus what was asked
 * ("ERP · orders for B-2847"). KG calls keep their question label; a call
 * with no readable input keeps its pill label. */
export function callRowLabel(pill: ToolPill, trace: ToolTrace | undefined): string {
  if (pill.kgq) return pill.label;
  const t = toolTarget(pill.tool);
  const head = t.kind === 'system' ? t.name : '';
  const asked = trace?.toolInput != null ? formatToolInput(pill.tool, trace.toolInput).summary : '';
  if (head && asked) return `${head} · ${asked}`;
  if (head) return head;
  return pill.label;
}

/** Group tone for the status dot: worst outcome wins. */
export type GroupTone = 'active' | 'ok' | 'warn' | 'error' | 'neutral';

export interface ToolGroupSummary {
  active: boolean;
  text: string;
  /** Right-hand meta: "1 of 3" while active; "4 calls · 1 failed · 3.1s" done. */
  meta: string;
  tone: GroupTone;
  doneCount: number;
  total: number;
}

const ISSUE_OUTCOMES: readonly CallOutcome[] = ['failed', 'timeout', 'partial', 'empty', 'filtered'];

/** "4 calls · 1 failed" — call count plus one entry per non-success
 * outcome. An activity line omits the count for a single call (its text
 * already names the one call); the reasoning fold always shows it. */
function countsMeta(
  total: number,
  states: readonly CallState[],
  alwaysCount = false,
): string[] {
  const meta: string[] = [];
  if (total > 1) meta.push(tf('activity.meta.calls', { n: total }));
  else if (alwaysCount && total === 1) meta.push(tr('activity.meta.oneCall'));
  for (const outcome of ISSUE_OUTCOMES) {
    const n = states.filter((s) => s === outcome).length;
    if (n) meta.push(tf(`activity.issue.${outcome}`, { n }));
  }
  return meta;
}

/** Worst outcome wins: any failure/timeout is red, any thin result amber,
 * verdict-less calls (done without a result, stopped) neutral, else green. */
function groupTone(states: readonly CallState[]): GroupTone {
  return states.some((s) => s === 'failed' || s === 'timeout')
    ? 'error'
    : states.some((s) => s === 'partial' || s === 'empty' || s === 'filtered')
      ? 'warn'
      : states.every((s) => s === 'done' || s === 'stopped')
        ? 'neutral'
        : 'ok';
}

/** Past-tense text for settled calls. Stopped calls never read as finished:
 * "Queried ERP · Stopped querying CMMS", or "Stopped querying ERP" when
 * nothing else ran. */
function settledText(pills: readonly ToolPill[], states: readonly CallState[]): string {
  const finished = pills.filter((_, i) => states[i] !== 'stopped');
  const stopped = pills.filter((_, i) => states[i] === 'stopped');
  return [
    finished.length ? describeCalls(finished, 'past') : '',
    stopped.length ? describeCalls(stopped, 'stopped') : '',
  ]
    .filter(Boolean)
    .join(' · ');
}

export function summarizeToolGroup(
  pills: readonly ToolPill[],
  traces: ReadonlyMap<string, ToolTrace>,
): ToolGroupSummary {
  const total = pills.length;
  const states = pills.map((p) => callState(p, traces.get(p.toolId)));
  const running = pills.filter((_, i) => states[i] === 'running');
  const doneCount = total - running.length;
  if (running.length > 0) {
    return {
      active: true,
      text: describeCalls(running, 'present'),
      meta: total > 1 ? tf('activity.meta.progress', { done: doneCount, total }) : '',
      tone: 'active',
      doneCount,
      total,
    };
  }
  const meta = countsMeta(total, states);
  // Group span: first start → last result, shown ONLY when every call in the
  // group returned (a result-less or stopped call has no end, so any span
  // would be a part passed off as the whole). Server stamps first, the
  // browser receipt clock as the fallback.
  const traced = pills.map((p) => traces.get(p.toolId)).filter((t): t is ToolTrace => !!t);
  const allReturned = traced.length === total && traced.every((t) => t.resultAt != null);
  if (allReturned) {
    const span = groupSpanMs(traced);
    if (total === 1 && traced[0].elapsedMs != null) meta.push(formatDuration(traced[0].elapsedMs));
    else if (span != null) meta.push(formatDuration(span));
  }
  const tone = groupTone(states);
  const text = settledText(pills, states);
  return { active: false, text, meta: meta.join(' · '), tone, doneCount, total };
}

/** The done-collapse fold label for a whole turn: the same past-tense
 * summary the activity lines use, over EVERY call in the turn ("Searched
 * the knowledge graph · Queried ERP, CMMS and 2 more systems"), with the
 * call count and issue counts as meta. No duration: calls are separated by
 * narration, so a first-start-to-last-result span would read as time spent
 * in tools when it is not. Empty text when the turn made no calls. */
export interface ReasoningSummary {
  text: string;
  meta: string;
  tone: GroupTone;
}

export function summarizeReasoning(
  pills: readonly ToolPill[],
  traces: ReadonlyMap<string, ToolTrace>,
): ReasoningSummary {
  if (!pills.length) return { text: '', meta: '', tone: 'neutral' };
  const states = pills.map((p) => callState(p, traces.get(p.toolId)));
  return {
    text: settledText(pills, states),
    meta: countsMeta(pills.length, states, true).join(' · '),
    tone: groupTone(states),
  };
}
