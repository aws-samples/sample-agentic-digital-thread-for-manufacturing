// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Unit tests — investigation-graph store (src/lib/arch-store.ts).
 *
 * DOM-free (vitest node env): exercises the handleArchEvent accumulation
 * semantics — accumulate across events,
 * survive between reads (session persistence), and reset on new session
 * (resetArchitectureState). */
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  archPlaceholderHidden,
  composeInvLabel,
  deriveInvNode,
  edgeLabelFromInput,
  buildInvNodeModal,
  fullToolInputText,
  getSystemLabel,
  isNoResults,
  isTransientError,
  narrativeInputText,
  type ArchStore,
} from '../lib/arch-store';
import { installArchStoreDisposal, trackedArchStore as createArchStore } from './arch-store-tracking';

installArchStoreDisposal();

function ask(store: ArchStore, question: string): void {
  store.handleArchEvent('user_msg', { question });
}

describe('arch-store: accumulate', () => {
  let store: ArchStore;
  beforeEach(() => {
    store = createArchStore();
  });

  it('user_msg mints a question node and sets the active question', () => {
    ask(store, 'Which batches failed QC?');
    const s = store.getState();
    expect(s.questionCounter).toBe(1);
    expect(s.currentQuestionId).toBe('question-1');
    expect(s.activeQuestion).toBe('Which batches failed QC?');
    expect(s.nodes.map((n) => n.id)).toContain('question-1');
    expect(s.activeQuestionFilter).toBe(1); // auto-focus newest question
  });

  it('tool_start adds a dedup’d system node, a queries edge, a blue qinput node and a HUD row', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: { q: 'SELECT 1' } });
    const s = store.getState();
    const ids = s.nodes.map((n) => n.id);
    expect(ids).toContain('system-erp');
    expect(ids.some((id) => id.startsWith('qinput-'))).toBe(true);
    expect(s.edges.some((e) => e.source === 'question-1' && e.target === 'system-erp')).toBe(true);
    expect(s.edges.some((e) => e.source === 'system-erp' && String(e.target).startsWith('qinput-'))).toBe(true);
    const hudTools = s.hud.filter((h) => h.kind === 'tool');
    expect(hudTools).toHaveLength(1);
    expect(hudTools[0].status).toBe('running');
    expect(s.flowingEdges).toContain('question-1->system-erp-q1');
    const sys = s.nodes.find((n) => n.id === 'system-erp');
    expect(sys?._activeQueries).toBe(1);
  });

  it('repeat calls to one system stay dedup’d with an ×N badge', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    const s = store.getState();
    const sysNodes = s.nodes.filter((n) => n.id === 'system-erp');
    expect(sysNodes).toHaveLength(1);
    expect(String(sysNodes[0].label)).toContain('×2');
    // Second concurrent start marks a parallel batch in the HUD.
    expect(s.hud.some((h) => h.kind === 'batch')).toBe(true);
    // The bracket sits BEFORE the batch's FIRST member, and BOTH overlapping
    // members are flagged parallel (indented) so the group reads as one batch.
    const batchIdx = s.hud.findIndex((h) => h.kind === 'batch');
    const tools = s.hud.filter((h) => h.kind === 'tool');
    expect(s.hud[batchIdx + 1]?.kind).toBe('tool');
    expect(tools.every((h) => h.parallel)).toBe(true);
  });

  it('a sequential call after a batch is not marked parallel', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_start', { tool: 'mes___query_mes', input: {} });
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: 'ok', input: {} });
    store.handleArchEvent('tool_result', { tool: 'mes___query_mes', summary: 'ok', input: {} });
    store.handleArchEvent('tool_start', { tool: 'plm___query_plm', input: {} });
    const s = store.getState();
    const tools = s.hud.filter((h) => h.kind === 'tool');
    expect(tools).toHaveLength(3);
    expect(tools[0].parallel).toBe(true);   // first batch member (retroactive)
    expect(tools[1].parallel).toBe(true);   // second batch member
    expect(tools[2].parallel).toBeFalsy();  // sequential follow-up, outside the batch
    // Exactly one bracket for the one batch.
    expect(s.hud.filter((h) => h.kind === 'batch')).toHaveLength(1);
  });

  it('tool_result adds a finding under the request’s qinput node and a trace entry', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: { sql: 'SELECT * FROM orders' } });
    store.handleArchEvent('tool_result', {
      tool: 'erp___query_erp',
      summary: '3 records found\n| a | b |',
      input: { sql: 'SELECT * FROM orders' },
    });
    const s = store.getState();
    const finding = s.nodes.find((n) => n.type === 'finding');
    expect(finding).toBeTruthy();
    // Finding hangs off the blue qinput node (system → question → result).
    const inEdge = s.edges.find((e) => e.target === finding!.id);
    expect(String(inEdge!.source).startsWith('qinput-')).toBe(true);
    // Trace: the question anchor row (Step 1) + the finding discovery row.
    expect(s.trace).toHaveLength(2);
    expect(s.trace[0].label).toBe('Question');
    expect(s.trace[1].type).toBe('discovery');
    expect(s.trace[1].step).toBe(2);
    // HUD row completed; flowing edge cleared; querying count back to 0.
    expect(s.hud.find((h) => h.kind === 'tool')?.status).toBe('ok');
    expect(s.flowingEdges).toHaveLength(0);
    expect(s.nodes.find((n) => n.id === 'system-erp')?._activeQueries).toBe(0);
  });

  it('explore_graph collapses onto the Knowledge Graph hub and parses entities', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'explore_graph', input: { cypher: 'MATCH (n) RETURN n' } });
    store.handleArchEvent('tool_result', {
      tool: 'explore_graph',
      summary: 'Concept: customer (identity)\nSystem: erp',
      input: { cypher: 'MATCH (n) RETURN n' },
    });
    const s = store.getState();
    expect(s.nodes.some((n) => n.id === 'system-neptune' && n.type === 'neptune')).toBe(true);
    // Entity nodes minted from the summary lines.
    expect(s.nodes.filter((n) => n.type === 'entity').length).toBe(2);
    // Task 1F: each KG call gets its own blue qinput node under the hub, and
    // the parsed entities attach to THAT qinput (not the hub directly).
    const qinput = s.nodes.find((n) => String(n.id).startsWith('qinput-'));
    expect(qinput).toBeTruthy();
    expect(s.edges.some((e) => e.source === 'system-neptune' && e.target === qinput!.id && e.label === 'asks')).toBe(
      true,
    );
    const entityIds = s.nodes.filter((n) => n.type === 'entity').map((n) => n.id);
    expect(s.edges.some((e) => e.source === qinput!.id && entityIds.includes(String(e.target)))).toBe(true);
    expect(s.edges.some((e) => e.source === 'system-neptune' && entityIds.includes(String(e.target)))).toBe(false);
  });

  it('a failed explore_graph query renders failed, never "No matching data"', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'explore_graph', input: { cypher: 'MATCH (a) RETURN a' } });
    store.handleArchEvent('tool_result', {
      tool: 'explore_graph',
      summary:
        'Error: Graph query failed (UNION column mismatch, not run): branch 1 returns [id, name]; branch 2 returns [concept_id, name].',
      input: {},
    });
    const s = store.getState();
    expect(s.nodes.some((n) => n.outcome === 'failed' && n.label === 'failed')).toBe(true);
    expect(s.nodes.some((n) => String(n.label).includes('No matching'))).toBe(false);
    expect(s.trace[s.trace.length - 1].outcome).toBe('failed');
  });

  it('transient errors surface as an honest error node + failed trace step; genuine no-results ARE findings', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_result', {
      tool: 'erp___query_erp',
      summary: 'Query error: COLUMN_NOT_FOUND: line 1:8',
      input: {},
    });
    let s = store.getState();
    // Graph honesty: the failed call's placeholder CONVERTS to a persistent
    // error node (red border, label 'failed') instead of vanishing silently.
    const errNode = s.nodes.find((n) => n.outcome === 'failed');
    expect(errNode).toBeTruthy();
    expect(errNode!.label).toBe('failed');
    expect(errNode!._inflight).toBeUndefined();
    // HUD/trace honesty: the error is no longer silently swallowed — it
    // renders ONE failed trace step with the error line (after the question
    // anchor row).
    expect(s.trace).toHaveLength(2);
    expect(s.trace[1].outcome).toBe('failed');
    expect(s.trace[1].text).toBe('Query error: COLUMN_NOT_FOUND: line 1:8');

    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_result', {
      tool: 'erp___query_erp',
      summary: 'No matching nodes found.',
      input: {},
    });
    s = store.getState();
    const empty = s.nodes.find((n) => String(n.id).startsWith('finding-empty-'));
    expect(empty).toBeTruthy();
    expect(String(empty!.label)).toContain('No matching data');
    // The no-results step is tagged 'empty' and, following the earlier failed
    // call to the same system, carries the retry badge.
    const emptyTrace = s.trace[2];
    expect(emptyTrace.outcome).toBe('empty');
    expect(emptyTrace.retry).toBe(true);
  });

  it('a second user_msg keeps the graph cumulative and links questions with "then"', () => {
    ask(store, 'first');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: '1 record found', input: {} });
    ask(store, 'second');
    const s = store.getState();
    expect(s.questionCounter).toBe(2);
    expect(s.nodes.map((n) => n.id)).toEqual(expect.arrayContaining(['question-1', 'question-2']));
    expect(s.edges.some((e) => e.source === 'question-1' && e.target === 'question-2' && e.label === 'then')).toBe(true);
    // Prior question's HUD rows collapsed into one summary chip.
    const summaries = s.hud.filter((h) => h.kind === 'summary');
    expect(summaries).toHaveLength(1);
    expect(summaries[0].question).toBe(1);
    expect(summaries[0].count).toBe(1);
    // Per-question aggregation reset on the shared system node.
    const sys = s.nodes.find((n) => n.id === 'system-erp');
    expect(sys?._totalQueries).toBe(0);
    expect(sys?._requests).toEqual([]);
    // The shared system node now belongs to no new question yet — its label is back to base.
    expect(String(sys?.label)).not.toContain('×');
  });

  it('a result without a matching tool_start mints the qinput on the fly', () => {
    ask(store, 'q');
    // No tool_start (parallel gateway batching) — result still gets a parent chain.
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: '2 records found', input: {} });
    const s = store.getState();
    expect(s.nodes.some((n) => n.id === 'system-erp')).toBe(true);
    expect(s.nodes.some((n) => String(n.id).startsWith('qinput-'))).toBe(true);
    expect(s.nodes.some((n) => n.type === 'finding')).toBe(true);
  });

  it('done marks running HUD rows stopped and clears flowing edges', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('done', {});
    const s = store.getState();
    expect(s.hud.find((h) => h.kind === 'tool')?.status).toBe('stopped');
    expect(s.flowingEdges).toHaveLength(0);
  });

  it('attaches captured tool arguments to the preceding genuine reasoning step', () => {
    ask(store, 'q');
    store.handleArchEvent('reasoning', { text: 'I need the production record before comparing suppliers.' });
    store.handleArchEvent('tool_start', {
      tool: 'mes-mil___query_mes_mil',
      input: { question: 'Find work order WO-2042 and return its lot number.' },
    });
    const s = store.getState();
    expect(s.trace).toHaveLength(2); // question anchor + the reasoning row
    expect(s.trace[1].type).toBe('decision');
    expect(s.trace[1].text).toContain('I need the production record');
    // The narrative names the system as its node / HUD / trace step do (no
    // catalog loaded here: the id-derived fallback), never the raw target id.
    expect(s.trace[1].text).toContain('Asking MES Mil: “Find work order WO-2042 and return its lot number.”');
    expect(s.trace[1].callKey).toBe(s.hud.find((h) => h.kind === 'tool')?.id);
  });

  it('state survives across reads (session persistence between view switches)', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    const first = store.getState();
    const second = store.getState();
    expect(second).toBe(first); // cached snapshot — stable identity
    expect(second.nodes.length).toBeGreaterThan(0);
  });
});

describe('arch-store: reset (resetArchitectureState / "+ New")', () => {
  it('clears nodes, edges, trace, HUD, counters and filter', () => {
    const store = createArchStore();
    ask(store, 'q1');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: '1 record found', input: {} });
    store.reset();
    const s = store.getState();
    expect(s.nodes).toHaveLength(0);
    expect(s.edges).toHaveLength(0);
    expect(s.trace).toHaveLength(0);
    expect(s.hud).toHaveLength(0);
    expect(s.questionCounter).toBe(0);
    expect(s.currentQuestionId).toBe('question-root');
    expect(s.activeQuestion).toBe('');
    expect(s.activeQuestionFilter).toBeNull();
    expect(s.flowingEdges).toHaveLength(0);
    // Counters restart — the next question is question-1 again.
    ask(store, 'q2');
    expect(store.getState().currentQuestionId).toBe('question-1');
  });

  it('notifies subscribers and emits a reset event', () => {
    const store = createArchStore();
    const onState = vi.fn();
    const onEvent = vi.fn();
    store.subscribe(onState);
    store.subscribeEvents(onEvent);
    store.reset();
    expect(onState).toHaveBeenCalledTimes(1);
    expect(onEvent).toHaveBeenCalledWith('reset', {});
  });
});

describe('arch-store: Wave-1 stub interface backward compatibility', () => {
  it('addNodes dedupes by id and notifies; addEdge appends', () => {
    const store = createArchStore();
    const listener = vi.fn();
    store.subscribe(listener);
    store.addNodes([{ id: 'a' }, { id: 'b' }]);
    store.addNodes([{ id: 'a' }]); // duplicate — no notify
    expect(store.getState().nodes.map((n) => n.id)).toEqual(['a', 'b']);
    expect(listener).toHaveBeenCalledTimes(1);
    store.addEdge({ source: 'a', target: 'b' });
    expect(store.getState().edges).toHaveLength(1);
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it('subscribe returns a working unsubscribe', () => {
    const store = createArchStore();
    const listener = vi.fn();
    const unsub = store.subscribe(listener);
    unsub();
    store.addNodes([{ id: 'x' }]);
    expect(listener).not.toHaveBeenCalled();
  });
});

describe('arch-store: pure helpers', () => {
  it('deriveInvNode collapses KG verbs onto the neptune hub and keys leaves by system id', () => {
    expect(deriveInvNode('explore_graph')).toEqual({ sysId: 'system-neptune', nodeType: 'neptune', label: 'Knowledge Graph' });
    expect(deriveInvNode('kg___save_value_mapping').sysId).toBe('system-neptune');
    expect(deriveInvNode('get_system_schema')).toEqual({ sysId: 'system-get_system_schema', nodeType: 'schema', label: 'Get Schema' });
    expect(deriveInvNode('mes-mil___query_mes_mil')).toEqual({ sysId: 'system-mes-mil', nodeType: 'system', label: 'MES Mil' });
    expect(deriveInvNode('query_erp')).toEqual({ sysId: 'system-erp', nodeType: 'system', label: 'ERP' });
  });

  it('composeInvLabel adds the ×N badge and truncates the request sub-line', () => {
    expect(composeInvLabel('ERP', 1)).toBe('ERP');
    expect(composeInvLabel('ERP', 3)).toBe('ERP  ×3');
    const long = 'x'.repeat(60);
    expect(composeInvLabel('ERP', 2, long)).toBe(`ERP  ×2\n${'x'.repeat(52)}…`);
  });

  it('getSystemLabel handles gateway, legacy and fallback shapes', () => {
    // No catalog loaded: the shared resolver's id fallback (lib/system-names.ts).
    expect(getSystemLabel('erp___query_erp')).toBe('ERP');
    expect(getSystemLabel('lake-athena___athena_run_query')).toBe('Lake Athena');
    expect(getSystemLabel('query_mes_mil')).toBe('MES Mil');
    expect(getSystemLabel('explore_graph')).toBe('Knowledge Graph');
  });

  it('isTransientError / isNoResults classify summaries like the original', () => {
    expect(isTransientError('Query error: boom')).toBe(true);
    expect(isTransientError('COLUMN_NOT_FOUND: x')).toBe(true);
    expect(isTransientError('3 records found')).toBe(false);
    // Task 1F: MCP transport/auth failures are transient (self-heal / 1E retry).
    expect(isTransientError('McpException - MCP invocation failed: Authorization error when sending message')).toBe(
      true,
    );
    expect(isTransientError('McpException - something broke')).toBe(true);
    expect(isTransientError('MCP invocation failed: timeout')).toBe(true);
    expect(isTransientError('Authorization error when sending message')).toBe(true);
    expect(isNoResults('No matching nodes found.')).toBe(true);
    expect(isNoResults('No records found.', 'erp___query_erp')).toBe(true);
    expect(isNoResults('No records found.', 'sitewise_query')).toBe(false);
  });

  it('edgeLabelFromInput picks domain labels from the query text', () => {
    expect(edgeLabelFromInput('')).toBe('result');
    expect(edgeLabelFromInput('MATCH (r:IdentityRule) RETURN r')).toBe('identity rules');
    expect(edgeLabelFromInput('SELECT * FROM production_runs')).toBe('production data');
    expect(edgeLabelFromInput('something else entirely with no keywords!!')).toBe(
      'something else entirely with no keywor'.slice(0, 35),
    );
  });

  it('buildInvNodeModal renders per-request blocks, falling back to detail', () => {
    expect(buildInvNodeModal({ id: 'n', detail: 'plain detail' })).toBe('plain detail');
    const md = buildInvNodeModal({
      id: 'n',
      _requests: ['Fetch orders'],
      _results: ['Returned ~3 rows'],
      _fullBodies: ['| a |'],
    });
    expect(md).toContain('### Request 1');
    expect(md).toContain('Fetch orders');
    expect(md).toContain('**Result:** Returned ~3 rows');
    expect(md).toContain('| a |');
  });
});

describe('arch-store: knowledge-graph visibility (self-learning port)', () => {
  let store: ArchStore;
  beforeEach(() => {
    store = createArchStore();
  });

  it('explore_graph with a scalar summary (no entities parsed) renders a compact KG finding node', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'explore_graph', input: { q: 'MATCH (n) RETURN count(n)' } });
    store.handleArchEvent('tool_result', {
      tool: 'explore_graph',
      summary: '42', // scalar row — parseNeptuneEntities adds nothing
      input: { q: 'MATCH (n) RETURN count(n)' },
    });
    const s = store.getState();
    const finding = s.nodes.find((n) => n.id.startsWith('finding-kg-'));
    expect(finding).toBeTruthy();
    expect(String(finding!.label)).toContain('Knowledge Graph');
    expect(String(finding!.label)).toContain('42');
    // Task 1F: the fallback finding hangs off this call's qinput node.
    const qinput = s.nodes.find((n) => String(n.id).startsWith('qinput-'));
    expect(qinput).toBeTruthy();
    expect(
      s.edges.some((e) => e.source === qinput!.id && e.target === finding!.id && e.label === 'returned'),
    ).toBe(true);
  });

  it('explore_graph that parses entities does NOT add the fallback finding node', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'explore_graph', input: {} });
    store.handleArchEvent('tool_result', {
      tool: 'explore_graph',
      summary: 'Concept: customer (identity)',
      input: {},
    });
    const s = store.getState();
    expect(s.nodes.some((n) => n.type === 'entity')).toBe(true);
    expect(s.nodes.some((n) => n.id.startsWith('finding-kg-'))).toBe(false);
  });

  it('save_* KG write renders a "Learned (…)" finding node and a decision trace entry', () => {
    ask(store, 'q');
    store.handleArchEvent('reasoning', { text: 'The cross-system values matched, so this evidence can be reviewed later.' });
    store.handleArchEvent('tool_start', {
      tool: 'kg___save_correlation',
      input: { pattern: 'ERP batch matches MES lot', systems: ['erp', 'mes-mil'] },
    });
    store.handleArchEvent('tool_result', {
      tool: 'kg___save_correlation',
      summary: 'Result with 3 fields: status, mapping_id, type',
      input: { status: 'logged', mapping_id: 'corr-123456789', type: 'insight' },
    });
    const s = store.getState();
    const finding = s.nodes.find((n) => n.id.startsWith('finding-kgwrite-'));
    expect(finding).toBeTruthy();
    expect(String(finding!.label)).toContain('Learned (correlation)');
    // Task 1F: the KG write's finding attaches to ITS OWN qinput node.
    const qinput = s.nodes.find((n) => String(n.id).startsWith('qinput-'));
    expect(qinput).toBeTruthy();
    expect(s.edges.some((e) => e.source === 'system-neptune' && e.target === qinput!.id && e.label === 'asks')).toBe(
      true,
    );
    expect(
      s.edges.some((e) => e.source === qinput!.id && e.target === finding!.id && e.label === 'saved'),
    ).toBe(true);
    const decisions = s.trace.filter((t) => t.type === 'decision');
    expect(decisions[0].text).toContain('Saving learned correlation: erp → mes-mil');
    expect(decisions[1].text).toBe('Saved correlation corr-123…');
  });

  it('KG-write brief is truncated at 80 chars with an ellipsis', () => {
    ask(store, 'q');
    const long = 'x'.repeat(200);
    store.handleArchEvent('tool_start', { tool: 'kg___save_field_annotation', input: {} });
    store.handleArchEvent('tool_result', { tool: 'kg___save_field_annotation', summary: long, input: {} });
    const s = store.getState();
    const finding = s.nodes.find((n) => n.id.startsWith('finding-kgwrite-'));
    const brief = String(finding!.label).split('\n')[1];
    expect(brief.length).toBeLessThanOrEqual(81); // 80 + ellipsis char
    expect(brief.endsWith('…')).toBe(true);
    expect(finding!.detail).toBe(long); // full body kept for the modal
  });

  it('KG-write SPLIT: the trace row text is UNCAPPED while the node label sub-line keeps the 80 cap', () => {
    ask(store, 'q');
    // First line ≤100 chars → describeToolResult's /^Saved\b/ passthrough
    // returns it VERBATIM (>80 chars), proving the trace no longer 80-caps.
    const first = `Saved ${'x'.repeat(90)}`; // 96 chars
    store.handleArchEvent('tool_start', { tool: 'kg___save_field_annotation', input: {} });
    store.handleArchEvent('tool_result', { tool: 'kg___save_field_annotation', summary: first, input: {} });
    const s = store.getState();
    const finding = s.nodes.find((n) => n.id.startsWith('finding-kgwrite-'))!;
    const briefLine = String(finding.label).split('\n')[1];
    expect(briefLine.length).toBeLessThanOrEqual(81); // graph label keeps the cap
    expect(briefLine.endsWith('…')).toBe(true);
    const decision = s.trace.filter((t) => t.type === 'decision').slice(-1)[0];
    expect(decision.text).toBe(first); // full 96 chars — the 80 cap no longer applies
    expect(decision.text.endsWith('…')).toBe(false);
  });
});

describe('arch-store: KG per-query question nodes (Task 1F)', () => {
  let store: ArchStore;
  beforeEach(() => {
    store = createArchStore();
  });

  it('two explore_graph calls mint two DISTINCT qinputs; each result binds to ITS qinput; hub label is base + ×2 with NO question text', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'explore_graph', input: { question: 'first kg question' } });
    store.handleArchEvent('tool_start', { tool: 'explore_graph', input: { question: 'second kg question' } });
    // FIFO: first result → first qinput (entities), second → second (scalar → finding-kg-*).
    store.handleArchEvent('tool_result', {
      tool: 'explore_graph',
      summary: 'Concept: customer (identity)',
      input: {},
    });
    store.handleArchEvent('tool_result', { tool: 'explore_graph', summary: '42', input: {} });
    const s = store.getState();

    const qinputs = s.nodes.filter((n) => String(n.id).startsWith('qinput-'));
    expect(qinputs).toHaveLength(2);
    expect(qinputs[0].label).toBe('first kg question');
    expect(qinputs[1].label).toBe('second kg question');
    // Both hang under the hub via 'asks' edges.
    for (const q of qinputs) {
      expect(s.edges.some((e) => e.source === 'system-neptune' && e.target === q.id && e.label === 'asks')).toBe(true);
    }
    // First result's entity attaches to the FIRST qinput.
    const entity = s.nodes.find((n) => n.type === 'entity');
    expect(entity).toBeTruthy();
    expect(s.edges.some((e) => e.source === qinputs[0].id && e.target === entity!.id)).toBe(true);
    // Second result's fallback finding attaches to the SECOND qinput.
    const finding = s.nodes.find((n) => n.id.startsWith('finding-kg-'));
    expect(finding).toBeTruthy();
    expect(s.edges.some((e) => e.source === qinputs[1].id && e.target === finding!.id && e.label === 'returned')).toBe(
      true,
    );
    // Nothing attaches to the hub directly, and the hub label carries the ×2
    // badge with NO question sub-line (label mutation removed).
    const hub = s.nodes.find((n) => n.id === 'system-neptune');
    expect(hub!.label).toBe('Knowledge Graph  ×2');
    expect(s.edges.some((e) => e.source === 'system-neptune' && e.target === entity!.id)).toBe(false);
    expect(s.edges.some((e) => e.source === 'system-neptune' && e.target === finding!.id)).toBe(false);
    // The tooltip/modal aggregation is preserved.
    expect(hub!._kgQuestions).toEqual(['first kg question', 'second kg question']);
    expect(String(hub!.detail)).toContain('KG: first kg question');
  });

  it('an MCP auth-error result yields NO finding/_results entry and the next success binds to the CORRECT qinput (FIFO shifts on error)', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'explore_graph', input: { question: 'failing question' } });
    store.handleArchEvent('tool_start', { tool: 'explore_graph', input: { question: 'succeeding question' } });
    store.handleArchEvent('tool_result', {
      tool: 'explore_graph',
      summary: 'McpException - MCP invocation failed: Authorization error when sending message',
      input: {},
    });
    store.handleArchEvent('tool_result', { tool: 'explore_graph', summary: '7', input: {} });
    const s = store.getState();

    // The error surfaced NO finding node and no trace entry for it.
    const findings = s.nodes.filter((n) => n.id.startsWith('finding-kg-'));
    expect(findings).toHaveLength(1);
    expect(String(findings[0].label)).toContain('7');
    expect(String(findings[0].label)).not.toContain('Authorization');
    // No _results/_fullBodies pollution from the error (only the success).
    const hub = s.nodes.find((n) => n.id === 'system-neptune');
    expect(hub!._results).toHaveLength(1);
    expect(hub!._fullBodies).toEqual(['7']);
    // FIFO shifted on the error: the success binds to the SECOND qinput.
    const qinputs = s.nodes.filter((n) => String(n.id).startsWith('qinput-'));
    expect(qinputs).toHaveLength(2);
    const successQinput = qinputs.find((n) => n.label === 'succeeding question')!;
    const failedQinput = qinputs.find((n) => n.label === 'failing question')!;
    expect(s.edges.some((e) => e.source === successQinput.id && e.target === findings[0].id)).toBe(true);
    // The failed call's qinput carries its converted honest error node (the
    // placeholder no longer vanishes silently) — and nothing else.
    const failedChildren = s.edges.filter((e) => e.source === failedQinput.id);
    expect(failedChildren).toHaveLength(1);
    const errNode = s.nodes.find((n) => n.id === failedChildren[0].target)!;
    expect(errNode.outcome).toBe('failed');
    expect(errNode.label).toBe('failed');
  });

  it('a KG result without a matching tool_start mints the qinput on the fly (mint-on-miss)', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_result', { tool: 'explore_graph', summary: '13', input: {} });
    const s = store.getState();
    const qinput = s.nodes.find((n) => String(n.id).startsWith('qinput-'));
    expect(qinput).toBeTruthy();
    expect(s.edges.some((e) => e.source === 'system-neptune' && e.target === qinput!.id && e.label === 'asks')).toBe(
      true,
    );
    const finding = s.nodes.find((n) => n.id.startsWith('finding-kg-'));
    expect(finding).toBeTruthy();
    expect(s.edges.some((e) => e.source === qinput!.id && e.target === finding!.id)).toBe(true);
  });

  it('an errored KG result with NO pending input mints an honest error node under the hub and reports a failed trace step', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_result', {
      tool: 'explore_graph',
      summary: 'McpException - MCP invocation failed: Authorization error when sending message',
      input: {},
    });
    const s = store.getState();
    // No qinput is minted for the failed call (no request to show)…
    expect(s.nodes.some((n) => String(n.id).startsWith('qinput-'))).toBe(false);
    // …but the failure is visible in the graph: a small honest error node
    // hangs off the hub (mint-on-miss — there was no placeholder to convert).
    const errNode = s.nodes.find((n) => String(n.id).startsWith('failed-'));
    expect(errNode).toBeTruthy();
    expect(errNode!.outcome).toBe('failed');
    expect(s.edges.some((e) => e.source === 'system-neptune' && e.target === errNode!.id)).toBe(true);
    // HUD/trace honesty: the failure is visible as a failed trace step.
    expect(s.trace).toHaveLength(2); // question anchor + the failed step
    expect(s.trace[1].outcome).toBe('failed');
    expect(s.trace[1].text).toContain('McpException');
  });
});

describe('arch-store: investigation liveness (Task 1H)', () => {
  let store: ArchStore;
  const g = globalThis as Record<string, unknown>;
  beforeEach(() => {
    store = createArchStore();
    delete g._lastDisplayText;
  });

  it('user_msg → reset → tool_start still renders a question node with the system edge attached (mint-on-miss)', () => {
    ask(store, 'Which batches failed QC?');
    g._lastDisplayText = 'Which batches failed QC?'; // set by runAskTurn before the stream
    store.reset(); // the (removed) legacy tab-switch reset racing the stream
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: { sql: 'SELECT 1' } });
    const s = store.getState();
    const qNode = s.nodes.find((n) => n.type === 'question');
    expect(qNode).toBeTruthy();
    expect(qNode!.id).toBe('question-1'); // counter re-incremented past question-root
    expect(qNode!.label).toBe('Which batches failed QC?');
    expect(s.activeQuestion).toBe('Which batches failed QC?'); // title restored
    // The system subtree is ROOTED — no silently-dropped question-root edge.
    expect(s.edges.some((e) => e.source === qNode!.id && e.target === 'system-erp')).toBe(true);
    // Minting re-arms the live flag (tool events only arrive mid-stream).
    expect(s.investigationActive).toBe(true);
    expect(s.activeQuestionFilter).toBe(1);
  });

  it('a tool_result that races a reset also mints the question node (label falls back to "Question")', () => {
    ask(store, 'q');
    store.reset();
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: '2 records found', input: {} });
    const s = store.getState();
    const qNode = s.nodes.find((n) => n.type === 'question');
    expect(qNode).toBeTruthy();
    expect(qNode!.label).toBe('Question'); // no activeQuestion, no _lastDisplayText
    expect(s.activeQuestion).toBe('Question');
    expect(s.edges.some((e) => e.source === qNode!.id && e.target === 'system-erp')).toBe(true);
    // The finding chain is fully rooted too.
    expect(s.nodes.some((n) => n.type === 'finding')).toBe(true);
  });

  it('mint-on-miss does NOT fire when the question node exists (normal flow unchanged)', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    const s = store.getState();
    expect(s.nodes.filter((n) => n.type === 'question')).toHaveLength(1);
    expect(s.questionCounter).toBe(1);
  });

  it('snapshot exposes investigationActive/hudInFlight with the documented lifecycle', () => {
    expect(store.getState().investigationActive).toBe(false);
    expect(store.getState().hudInFlight).toBe(0);
    ask(store, 'q');
    expect(store.getState().investigationActive).toBe(true);
    expect(store.getState().hudInFlight).toBe(0);
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    expect(store.getState().hudInFlight).toBe(1);
    store.handleArchEvent('tool_start', { tool: 'explore_graph', input: {} });
    expect(store.getState().hudInFlight).toBe(2);
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: '1 record found', input: {} });
    expect(store.getState().hudInFlight).toBe(1);
    store.handleArchEvent('tool_result', { tool: 'explore_graph', summary: '42', input: {} });
    expect(store.getState().hudInFlight).toBe(0);
    expect(store.getState().investigationActive).toBe(true); // still live between batches
    store.handleArchEvent('done', {});
    expect(store.getState().investigationActive).toBe(false);
  });

  it('an error-path done (no tool events) also clears investigationActive; reset clears it too', () => {
    ask(store, 'q');
    expect(store.getState().investigationActive).toBe(true);
    store.handleArchEvent('done', {}); // useAskStream error branches emit this
    expect(store.getState().investigationActive).toBe(false);
    ask(store, 'q2');
    expect(store.getState().investigationActive).toBe(true);
    store.reset();
    expect(store.getState().investigationActive).toBe(false);
  });

  it('archPlaceholderHidden is presence-driven: any question OR any node hides it', () => {
    expect(archPlaceholderHidden({ questionCounter: 0, nodes: [] })).toBe(false); // shown
    expect(archPlaceholderHidden({ questionCounter: 0, nodes: [{ id: 'x' }] })).toBe(true); // hidden
    expect(archPlaceholderHidden({ questionCounter: 1, nodes: [] })).toBe(true); // hidden
    // Live store: a reset-raced stream (nodes but questionCounter bump from
    // the mint) still hides the placeholder.
    ask(store, 'q');
    expect(archPlaceholderHidden(store.getState())).toBe(true);
    store.reset();
    expect(archPlaceholderHidden(store.getState())).toBe(false);
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    expect(archPlaceholderHidden(store.getState())).toBe(true);
  });
});

describe('arch-store: trace narrative (keyed heartbeat upgrade + question row)', () => {
  let store: ArchStore;
  beforeEach(() => {
    store = createArchStore();
  });

  const HB_A = 'preparing sys_a___query_sys_a query…';
  const HB_B = 'preparing sys_b___query_sys_b query…';

  it('user_msg adds ONE question trace row per turn', () => {
    ask(store, 'Which work orders consumed the recalled lot?');
    let s = store.getState();
    const qRows = s.trace.filter((t) => t.label === 'Question');
    expect(qRows).toHaveLength(1);
    expect(qRows[0].type).toBe('discovery');
    expect(qRows[0].text).toBe('Which work orders consumed the recalled lot?');
    expect(qRows[0].nodeId).toBe('question-1');
    expect(qRows[0].question).toBe(1);
    // Second turn → exactly one more question row, tied to Q2.
    ask(store, 'And which suppliers were involved?');
    s = store.getState();
    expect(s.trace.filter((t) => t.label === 'Question')).toHaveLength(2);
    expect(s.trace.filter((t) => t.label === 'Question' && t.question === 2)).toHaveLength(1);
  });

  it('a >200-char question reaches TraceEntry.text UNCLAMPED (SHOW MORE is the only length control)', () => {
    const q = `Which orders consumed the recalled lot ${'x'.repeat(560)}`;
    ask(store, q);
    const qRow = store.getState().trace.find((t) => t.label === 'Question')!;
    expect(qRow.text).toBe(q); // full collapsed question — no slice, no ellipsis
    expect(qRow.text.length).toBeGreaterThan(200);
    expect(qRow.text.endsWith('…')).toBe(false);
  });

  it('a >200-char sub-question reaches the ASKING trace row uncapped; graph forms stay capped (D1)', () => {
    const q = `Find every work order that consumed the recalled lot ${'x'.repeat(450)}`;
    ask(store, 'q');
    store.handleArchEvent('reasoning', { text: HB_A });
    store.handleArchEvent('tool_start', { tool: 'sys_a___query_sys_a', input: { question: q } });
    const s = store.getState();
    const row = s.trace.find((t) => t.type === 'decision')!;
    expect(row.text).toBe(`Asking Sys A: “${q}”`); // FULL sub-question, no ellipsis
    expect(row.text).not.toContain('…');
    // Graph aggregation keeps describeToolInput's 120-char capped form.
    const node = s.nodes.find((n) => n.id === 'system-sys_a')!;
    const lastReq = String(node._lastRequest);
    expect(lastReq.endsWith('…')).toBe(true);
    expect(lastReq.length).toBeLessThanOrEqual(121); // 120 + ellipsis char
    // The blue qinput label stays at its 90-char graph cap.
    const qinput = s.nodes.find((n) => String(n.id).startsWith('qinput-'))!;
    expect(String(qinput.label).length).toBeLessThanOrEqual(91); // 90 + ellipsis char
    expect(String(qinput.label).endsWith('…')).toBe(true);
  });

  it('single-tool: tool_start REPLACES its composition heartbeat with the actual ask', () => {
    ask(store, 'q');
    store.handleArchEvent('reasoning', { text: HB_A });
    store.handleArchEvent('tool_start', {
      tool: 'sys_a___query_sys_a',
      input: { question: 'Find the work order for lot L-1.' },
    });
    const s = store.getState();
    const row = s.trace.find((t) => t.type === 'decision')!;
    expect(row.text).toBe('Asking Sys A: “Find the work order for lot L-1.”');
    expect(row.text).not.toContain('preparing');
    expect(row.nodeId).toBe('system-sys_a');
    expect(row.callKey).toBe(s.hud.find((h) => h.kind === 'tool')?.id);
  });

  it('parallel batch: each heartbeat row upgrades with ITS OWN tool’s ask (no attach-to-latest misbind)', () => {
    ask(store, 'q');
    store.handleArchEvent('reasoning', { text: HB_A });
    store.handleArchEvent('reasoning', { text: HB_B });
    store.handleArchEvent('tool_start', {
      tool: 'sys_a___query_sys_a',
      input: { question: 'Which orders consumed lot L-1?' },
    });
    store.handleArchEvent('tool_start', {
      tool: 'sys_b___query_sys_b',
      input: { question: 'Which shipments carried lot L-1?' },
    });
    const s = store.getState();
    const decisions = s.trace.filter((t) => t.type === 'decision');
    expect(decisions).toHaveLength(2);
    // Heartbeat A (added FIRST) carries sys_a's ask; heartbeat B carries sys_b's.
    expect(decisions[0].text).toBe('Asking Sys A: “Which orders consumed lot L-1?”');
    expect(decisions[0].nodeId).toBe('system-sys_a');
    expect(decisions[1].text).toBe('Asking Sys B: “Which shipments carried lot L-1?”');
    expect(decisions[1].nodeId).toBe('system-sys_b');
    // No row is left with the bare composing placeholder.
    expect(s.trace.some((t) => t.text.startsWith('preparing'))).toBe(false);
    // Correlation keys point at each call's OWN HUD row.
    const hudA = s.hud.find((h) => h.kind === 'tool' && h.tool === 'sys_a___query_sys_a')!;
    const hudB = s.hud.find((h) => h.kind === 'tool' && h.tool === 'sys_b___query_sys_b')!;
    expect(decisions[0].callKey).toBe(hudA.id);
    expect(decisions[1].callKey).toBe(hudB.id);
  });

  it('an empty tool input leaves the heartbeat row untouched (honest) and unclaimed', () => {
    ask(store, 'q');
    store.handleArchEvent('reasoning', { text: HB_A });
    store.handleArchEvent('tool_start', { tool: 'sys_a___query_sys_a', input: {} });
    const row = store.getState().trace.find((t) => t.type === 'decision')!;
    expect(row.text).toBe(HB_A);
    expect(row.callKey).toBeUndefined();
  });

  it('a GENUINE reasoning row still gets the tool context APPENDED (fallback intact)', () => {
    ask(store, 'q');
    store.handleArchEvent('reasoning', { text: 'I should check the production system first.' });
    store.handleArchEvent('tool_start', {
      tool: 'sys_a___query_sys_a',
      input: { question: 'List runs for line 2.' },
    });
    const row = store.getState().trace.find((t) => t.type === 'decision')!;
    expect(row.text).toContain('I should check the production system first.');
    expect(row.text).toContain('Asking Sys A: “List runs for line 2.”');
    expect(row.callKey).toBeTruthy();
  });

  it('a question boundary clears pending heartbeats (stale rows are never upgraded)', () => {
    ask(store, 'q1');
    store.handleArchEvent('reasoning', { text: HB_A }); // never gets its tool_start
    ask(store, 'q2');
    store.handleArchEvent('reasoning', { text: 'Now checking the production system.' });
    store.handleArchEvent('tool_start', {
      tool: 'sys_a___query_sys_a',
      input: { question: 'List runs for line 2.' },
    });
    const s = store.getState();
    // Q1's abandoned heartbeat keeps its honest composing text…
    const stale = s.trace.find((t) => t.question === 1 && t.type === 'decision')!;
    expect(stale.text).toBe(HB_A);
    expect(stale.callKey).toBeUndefined();
    // …and Q2's genuine reasoning row got the fallback context (the stale
    // FIFO entry did not swallow the narrative).
    const genuine = s.trace.find((t) => t.question === 2 && t.type === 'decision')!;
    expect(genuine.text).toContain('Now checking the production system.');
    expect(genuine.text).toContain('Asking Sys A: “List runs for line 2.”');
  });

  it('reset clears pending heartbeats', () => {
    ask(store, 'q1');
    store.handleArchEvent('reasoning', { text: HB_A });
    store.reset();
    ask(store, 'q2');
    store.handleArchEvent('reasoning', { text: 'Fresh reasoning after reset.' });
    store.handleArchEvent('tool_start', {
      tool: 'sys_a___query_sys_a',
      input: { question: 'List runs for line 2.' },
    });
    const genuine = store.getState().trace.find((t) => t.type === 'decision')!;
    expect(genuine.text).toContain('Fresh reasoning after reset.');
    expect(genuine.text).toContain('Asking Sys A: “List runs for line 2.”');
  });
});

/* ── D1 trace de-truncation helpers: fullToolInputText / narrativeInputText ─
 * fullToolInputText deliberately duplicates architecture-util.js
 * _pickInputValue's priority-key extraction (the util pair is byte-pinned
 * with the legacy js bundle and cannot grow exports) — this parity suite pins the
 * duplication against the util's exported describeToolInput. */
describe('D1: fullToolInputText / narrativeInputText', () => {
  // The side-effect import in arch-store.ts registers describeToolInput on
  // globalThis (verbatim CJS IIFE — same load pattern as the app).
  const describeToolInput = (globalThis as Record<string, unknown>).describeToolInput as (
    tool: string,
    input: unknown,
  ) => string;

  it('parity: extracts the SAME field describeToolInput picks, for every priority key', () => {
    const KEYS = ['question', 'sub_question', 'subquestion', 'query', 'sql', 'statement',
      'filter', 'expression', 'cypher', 'gremlin', 'prompt', 'text', 'input'];
    for (const k of KEYS) {
      // Under 120 chars + query_* tool → describeToolInput output == collapse(value).
      const input = { [k]: `find the ${k} rows now` };
      expect(fullToolInputText(input)).toBe(describeToolInput('sys___query_sys', input));
    }
  });

  it('parity: string / first-string-value / scalar-JSON fallbacks match too', () => {
    expect(fullToolInputText('plain  string')).toBe(describeToolInput('sys___query_sys', 'plain  string'));
    const firstString = { other_key: 'first string value' };
    expect(fullToolInputText(firstString)).toBe(describeToolInput('sys___query_sys', firstString));
    const scalars = { count: 5 };
    expect(fullToolInputText(scalars)).toBe(describeToolInput('sys___query_sys', scalars));
    expect(fullToolInputText(null)).toBe('');
    expect(fullToolInputText({})).toBe('');
  });

  it('fullToolInputText never caps: a 500-char question comes back whole (collapsed)', () => {
    const q = `lot   trace ${'x'.repeat(500)}`;
    expect(fullToolInputText({ question: q })).toBe(`lot trace ${'x'.repeat(500)}`);
  });

  it('narrativeInputText swaps in the full text ONLY for a truncated prefix', () => {
    const long = `find work orders ${'x'.repeat(200)}`;
    const capped = `${long.slice(0, 120).trimEnd()}…`; // _truncate(sval, 120) shape
    expect(narrativeInputText(capped, { question: long })).toBe(long);
    // Not truncated → unchanged.
    expect(narrativeInputText('short ask', { question: long })).toBe('short ask');
    // Ends with … but is a REWRITE (not a prefix of the raw input) → unchanged:
    // Englishized Cypher and save_* phrasings keep their phrased form.
    const rewrite = 'Saving learned correlation — “pattern…';
    expect(narrativeInputText(rewrite, { question: long })).toBe(rewrite);
    // Empty request English stays empty (honest heartbeat keeps its text).
    expect(narrativeInputText('', { question: long })).toBe('');
  });
});
