// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — in-flight flow indicator + rich drill-down payload (arch-store).
 *
 * tool_start mints a green pulsing PLACEHOLDER data node under the request's
 * qinput and marks the whole active path (question → system → qinput →
 * placeholder) as flowing (marching ants); the matching tool_result consumes
 * the placeholder and the real finding takes its place carrying the FULL
 * per-call drill-down payload (`_call`: tool, system, question, English+raw
 * input, full summary, timing). Errors and stream done/user_msg sweep ALL
 * in-flight placeholders — never a stuck pulsing node. */
import { beforeEach, describe, expect, it } from 'vitest';

import {
  buildInvCallModal,
  buildInvNodeModal,
  rawPayloadText,
  type ArchNode,
  type ArchStore,
  type InvCallDetail,
} from '../lib/arch-store';
import { installArchStoreDisposal, trackedArchStore as createArchStore } from './arch-store-tracking';

installArchStoreDisposal();

function ask(store: ArchStore, question: string): void {
  store.handleArchEvent('user_msg', { question });
}

function placeholders(store: ArchStore): ArchNode[] {
  return store.getState().nodes.filter((n) => n._inflight === true);
}

describe('in-flight flow indicator: placeholder + marching ants', () => {
  let store: ArchStore;
  beforeEach(() => {
    store = createArchStore();
  });

  it('tool_start mints a pulsing placeholder under the qinput and flows the whole path', () => {
    ask(store, 'Why did batch 42 fail?');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: { sql: 'SELECT 1' } });
    const s = store.getState();
    const ph = s.nodes.find((n) => String(n.id).startsWith('pending-'));
    expect(ph).toBeTruthy();
    expect(ph!._inflight).toBe(true);
    expect(ph!.type).toBe('finding'); // green finding palette
    expect(ph!.label).toBe('…');
    // Placeholder hangs off THIS request's blue qinput node.
    const inEdge = s.edges.find((e) => e.target === ph!.id);
    expect(String(inEdge!.source).startsWith('qinput-')).toBe(true);
    // Marching ants along the WHOLE active path.
    const qinputId = String(inEdge!.source);
    expect(s.flowingEdges).toContain('question-1->system-erp-q1');
    expect(s.flowingEdges).toContain(`system-erp->${qinputId}-q1`);
    expect(s.flowingEdges).toContain(`${qinputId}->${ph!.id}-q1`);
  });

  it('tool_result consumes the placeholder; the real finding takes its spot; flowing clears', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: { sql: 'SELECT * FROM orders' } });
    const qinputId = String(store.getState().edges.find((e) => String(e.target).startsWith('pending-'))!.source);
    store.handleArchEvent('tool_result', {
      tool: 'erp___query_erp',
      summary: '3 records found\n| a | b |',
      input: { sql: 'SELECT * FROM orders' },
    });
    const s = store.getState();
    expect(placeholders(store)).toHaveLength(0);
    expect(s.nodes.some((n) => String(n.id).startsWith('pending-'))).toBe(false);
    // The real finding hangs off the SAME qinput the placeholder occupied.
    const finding = s.nodes.find((n) => n.type === 'finding');
    expect(finding).toBeTruthy();
    expect(s.edges.some((e) => e.source === qinputId && e.target === finding!.id)).toBe(true);
    // No dangling edges to the removed placeholder; all ants stopped.
    expect(s.edges.some((e) => String(e.target).startsWith('pending-'))).toBe(false);
    expect(s.flowingEdges).toHaveLength(0);
  });

  it('parallel calls to ONE system: the question→system edge keeps flowing until the LAST result', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    expect(placeholders(store)).toHaveLength(2);
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: '1 record found', input: {} });
    let s = store.getState();
    // Sibling still in flight — the shared question→system edge keeps its ants.
    expect(s.flowingEdges).toContain('question-1->system-erp-q1');
    expect(placeholders(store)).toHaveLength(1);
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: '2 records found', input: {} });
    s = store.getState();
    expect(s.flowingEdges).toHaveLength(0);
    expect(placeholders(store)).toHaveLength(0);
  });

  it('an errored call CONVERTS its placeholder to a persistent honest error node (never a stuck pulsing node, never silently vanished)', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_result', {
      tool: 'erp___query_erp',
      summary: 'Query error: COLUMN_NOT_FOUND: line 1:8',
      input: {},
    });
    const s = store.getState();
    // No longer in flight (no pulsing '…')…
    expect(placeholders(store)).toHaveLength(0);
    // …but the node PERSISTS as an honest failure marker (graph honesty):
    const err = s.nodes.find((n) => String(n.id).startsWith('pending-'));
    expect(err).toBeTruthy();
    expect(err!.label).toBe('failed');
    expect(err!.outcome).toBe('failed');
    expect(err!._inflight).toBeUndefined();
    expect((err!._call as InvCallDetail).resultSummary).toContain('COLUMN_NOT_FOUND'); // clickable → rich modal
    expect(s.flowingEdges).toHaveLength(0);
    // done does NOT sweep the resolved-failed node (only unresolved ones).
    store.handleArchEvent('done', {});
    expect(store.getState().nodes.some((n) => n.id === err!.id)).toBe(true);
  });

  it('done sweeps ALL in-flight placeholders (crashed/aborted stream cleanup)', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_start', { tool: 'explore_graph', input: {} });
    expect(placeholders(store)).toHaveLength(2);
    store.handleArchEvent('done', {}); // incl. the error-path done from useAskStream
    const s = store.getState();
    expect(placeholders(store)).toHaveLength(0);
    expect(s.nodes.some((n) => String(n.id).startsWith('pending-'))).toBe(false);
    expect(s.flowingEdges).toHaveLength(0);
  });

  it('a new user_msg sweeps placeholders leaked from the previous question', () => {
    ask(store, 'q1');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    expect(placeholders(store)).toHaveLength(1);
    ask(store, 'q2');
    expect(placeholders(store)).toHaveLength(0);
  });

  it('reset clears placeholders with everything else', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.reset();
    expect(placeholders(store)).toHaveLength(0);
    expect(store.getState().nodes).toHaveLength(0);
  });
});

describe('rich drill-down payload (_call / _calls)', () => {
  let store: ArchStore;
  beforeEach(() => {
    store = createArchStore();
  });

  function runOneCall(): ArchStore {
    ask(store, 'Why did batch 42 fail?');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: { sql: 'SELECT * FROM orders' } });
    store.handleArchEvent('tool_result', {
      tool: 'erp___query_erp',
      summary: '3 records found\n| order | qty |\n| A-1 | 5 |',
      input: { sql: 'SELECT * FROM orders' },
    });
    return store;
  }

  it('the finding carries the FULL per-call payload (tool, system, question, raw input, full summary, timing)', () => {
    runOneCall();
    const s = store.getState();
    const finding = s.nodes.find((n) => n.type === 'finding');
    const call = finding!._call as InvCallDetail;
    expect(call).toBeTruthy();
    expect(call.tool).toBe('erp___query_erp');
    expect(call.system).toBe('ERP');
    expect(call.question).toBe(1);
    expect(call.questionText).toBe('Why did batch 42 fail?');
    expect(call.inputRaw).toContain('SELECT * FROM orders');
    expect(call.resultSummary).toBe('3 records found\n| order | qty |\n| A-1 | 5 |'); // FULL, not the headline
    expect(typeof call.elapsedMs).toBe('number');
    expect(call.elapsedMs!).toBeGreaterThanOrEqual(0);
    expect(typeof call.startedAt).toBe('number');
  });

  it('the qinput node carries the same payload; the system node aggregates _calls', () => {
    runOneCall();
    const s = store.getState();
    const qinput = s.nodes.find((n) => String(n.id).startsWith('qinput-'));
    expect((qinput!._call as InvCallDetail).resultSummary).toContain('| A-1 | 5 |');
    const sys = s.nodes.find((n) => n.id === 'system-erp');
    const calls = sys!._calls as InvCallDetail[];
    expect(calls).toHaveLength(1);
    expect(calls[0].inputRaw).toContain('SELECT * FROM orders');
  });

  it('KG entities and the KG-write finding carry the payload too', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'explore_graph', input: { question: 'which systems?' } });
    store.handleArchEvent('tool_result', {
      tool: 'explore_graph',
      summary: 'Concept: customer (identity)',
      input: {},
    });
    store.handleArchEvent('tool_start', { tool: 'kg___save_correlation', input: { a: 'erp.batch' } });
    store.handleArchEvent('tool_result', {
      tool: 'kg___save_correlation',
      summary: 'Saved correlation between erp.orders.batch and mes.runs.lot',
      input: {},
    });
    const s = store.getState();
    const entity = s.nodes.find((n) => n.type === 'entity');
    expect((entity!._call as InvCallDetail).resultSummary).toContain('Concept: customer');
    const kgWrite = s.nodes.find((n) => String(n.id).startsWith('finding-kgwrite-'));
    const wcall = kgWrite!._call as InvCallDetail;
    expect(wcall.tool).toBe('kg___save_correlation');
    expect(wcall.resultSummary).toContain('Saved correlation between'); // full text, not the 80-char headline
    expect(wcall.inputRaw).toContain('erp.batch');
  });

  it('user_msg resets the per-question _calls aggregation on system nodes', () => {
    runOneCall();
    ask(store, 'next question');
    const sys = store.getState().nodes.find((n) => n.id === 'system-erp');
    expect(sys!._calls).toEqual([]);
  });

  it('errors do not pollute _calls', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: 'Query error: boom', input: {} });
    const sys = store.getState().nodes.find((n) => n.id === 'system-erp');
    expect((sys!._calls as InvCallDetail[]) ?? []).toHaveLength(0);
  });
});

describe('drill-down markdown builders', () => {
  const call: InvCallDetail = {
    tool: 'kg___save_correlation',
    system: 'Knowledge Graph',
    question: 2,
    questionText: 'How do orders map to runs?',
    inputEnglish: 'Saving a correlation between ERP orders and MES runs',
    inputRaw: '{\n  "a": "erp.orders.batch",\n  "b": "mes.runs.lot"\n}',
    resultSummary: 'Saved correlation between erp.orders.batch and mes.runs.lot\nwith confidence 0.92',
    resultEnglish: 'Saved 1 correlation',
    resultRaw: '{"status": "ok", "reason": "saved"}',
    startedAt: 1000,
    elapsedMs: 1234,
  };

  it('buildInvCallModal shows system+tool, the question, timing, BOTH input forms, and the FULL summary', () => {
    const md = buildInvCallModal({ id: 'x', _call: call });
    expect(md).toContain('**System:** Knowledge Graph');
    expect(md).toContain('`kg___save_correlation`');
    expect(md).toContain('**Question (Q2):** How do orders map to runs?');
    expect(md).toContain('**Duration:** 1.2s');
    expect(md).toContain('Saving a correlation between ERP orders and MES runs');
    expect(md).toContain('erp.orders.batch'); // raw input block
    expect(md).toContain('### Result');
    expect(md).toContain('with confidence 0.92'); // FULL summary, beyond the headline
    expect(md).toContain('### Raw result');
    expect(md).toContain('"status": "ok"');
  });

  it('buildInvCallModal falls back to detail when no _call exists (legacy nodes)', () => {
    expect(buildInvCallModal({ id: 'x', detail: 'legacy detail' })).toBe('legacy detail');
    expect(buildInvCallModal({ id: 'x' })).toBe('');
  });

  it('buildInvNodeModal prefers the rich _calls aggregation over the legacy arrays', () => {
    const md = buildInvNodeModal({ id: 'system-x', _calls: [call] });
    expect(md).toContain('### Request 1 — `kg___save_correlation` · 1.2s');
    expect(md).toContain('Saving a correlation between ERP orders and MES runs');
    expect(md).toContain('erp.orders.batch');
    expect(md).toContain('**Result:** Saved 1 correlation');
    expect(md).toContain('with confidence 0.92');
  });

  it('buildInvNodeModal keeps the legacy _requests/_results/_fullBodies fallback', () => {
    const md = buildInvNodeModal({
      id: 'n',
      _requests: ['Fetch orders'],
      _results: ['Returned ~3 rows'],
      _fullBodies: ['| a |'],
    });
    expect(md).toContain('### Request 1');
    expect(md).toContain('**Result:** Returned ~3 rows');
  });

  it('rawPayloadText pretty-prints objects and caps long payloads', () => {
    expect(rawPayloadText({ q: 'SELECT 1' })).toContain('"q": "SELECT 1"');
    expect(rawPayloadText('plain')).toBe('plain');
    expect(rawPayloadText(null)).toBe('');
    const long = rawPayloadText('x'.repeat(5000));
    expect(long.length).toBeLessThan(4100);
    expect(long).toContain('… (truncated)');
  });
});
