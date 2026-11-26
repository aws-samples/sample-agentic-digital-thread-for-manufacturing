// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Reducer tests for the chat thread store (jsx-migration W1.2) — replays
 * recorded /api/ask SSE event sequences (fixture grammar mined from
 * src/test/sse.test.ts and the runAskTurn logic in chat/useAskStream.ts) and
 * asserts the final turn shape:
 *   - text runs PRESERVED across tool boundaries (selectFinalAnswer picks the
 *     longest run + trailing epilogue — the P0 "missing answer" bug guard);
 *   - the `_toolInput` stash: tool_result's `input` carries the RESULT, the
 *     trace must keep + render the REAL args from tool_start;
 *   - KG pill actual-question label (data-kgq) kept through the tool_result
 *     explore_graph relabel; non-kgq pills DO relabel;
 *   - done collapse (reasoningCollapsed + the summarizeReasoning label);
 *   - citations numbered per non-explore_graph tool, sequential from 1;
 *   - abort → quiet canceled marker with partial content preserved;
 *   - rAF-coalesced notify for token events (one notify per frame). */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { createThreadStore } from '../chat/thread-store';
import { formatToolInput } from '../chat/tool-labels';
import { summarizeReasoning } from '../chat/tool-activity';

type Ev = [string, unknown];

/** The canonical stream shape from sse.test.ts, extended with the semantics
 * under test: reasoning run → KG call (question input) → system tool calls
 * (stash + citations) → a long run → one more DATA call (ERP) → a short
 * final run → done. The run before the ERP call was written before the
 * model had that call's data, so the answer is the final run. */
const LONG_ANSWER =
  'Line 2 is running at 92% OEE. The bottleneck is the filler on shift 3; ' +
  'CMMS shows an open work order for the capper. Recommend scheduling PM.';

const REPLAY: Ev[] = [
  ['token', { text: 'Checking ' }],
  ['token', { text: 'the line…' }],
  [
    'tool_start',
    { tool: 'explore_graph', tool_id: 'kg1', input: { question: 'which systems know line 2' } },
  ],
  [
    'tool_result',
    { tool: 'explore_graph', tool_id: 'kg1', input: 'Entity: Line matched 4 systems', summary: 'Found 4 systems tracking Line entities in the graph.' },
  ],
  ['tool_end', { tool: 'explore_graph', tool_id: 'kg1' }],
  [
    'tool_start',
    { tool: 'query_mes', tool_id: 't1', input: { question: 'line 2 status' } },
  ],
  [
    'tool_result',
    { tool: 'query_mes', tool_id: 't1', input: 'RESULT text', summary: '3 rows' },
  ],
  ['tool_end', { tool: 'query_mes', tool_id: 't1' }],
  ['token', { text: LONG_ANSWER }],
  ['tool_start', { tool: 'erp___query_erp', tool_id: 't2', input: { sql: 'SELECT 1' } }],
  ['tool_result', { tool: 'erp___query_erp', tool_id: 't2', input: 'ok', summary: '1 row' }],
  ['tool_end', { tool: 'erp___query_erp', tool_id: 't2' }],
  ['token', { text: 'Anything else?' }],
  ['done', { status: 'complete' }],
];

function replay(events: Ev[]) {
  const store = createThreadStore();
  store.startTurn('what is line 2 status?');
  for (const [event, data] of events) store.applyEvent(event, data);
  return store;
}

const lastTurn = (store: ReturnType<typeof createThreadStore>) => {
  const { turns } = store.getState();
  return turns[turns.length - 1];
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('thread store — full replay', () => {
  it('preserves text runs across tool boundaries and reconstructs the final answer', () => {
    const turn = lastTurn(replay(REPLAY));
    // The answer is chosen from the runs after the last DATA call (t2, ERP):
    // the long run before it was written before the model had t2's data.
    // Every run is still preserved for the reasoning trace; a run before a
    // WRITE-BACK call stays in the answer (the P0 guard below).
    expect(turn.assistant.answerMd).toBe('Anything else?');
    expect(turn.assistant.segments.completedRuns).toEqual([
      'Checking the line…', LONG_ANSWER, 'Anything else?']);
    expect(turn.assistant.streaming).toBe(false);
  });

  it('keeps a PRE-tool run as the answer when it is the longest (P0 missing-answer guard)', () => {
    const store = createThreadStore();
    store.startTurn('q');
    store.applyEvent('token', { text: LONG_ANSWER }); // full report FIRST
    // trailing housekeeping (write-back) call
    store.applyEvent('tool_start', { tool: 'digital-thread___save_correlation', tool_id: 's1', input: {} });
    store.applyEvent('tool_result', { tool: 'digital-thread___save_correlation', tool_id: 's1', input: 'ok', summary: 'saved' });
    store.applyEvent('token', { text: 'Saved.' }); // short epilogue
    store.applyEvent('done', {});
    // The run before the tool boundary was preserved and selected.
    expect(lastTurn(store).assistant.answerMd).toBe(LONG_ANSWER + '\n\nSaved.');
  });

  it('stashes the REAL tool_start args (_toolInput) and renders them, not the RESULT', () => {
    const turn = lastTurn(replay(REPLAY));
    const trace = turn.assistant.traces.get('t1')!;
    // The stash survives tool_result…
    expect(trace.toolInput).toEqual({ question: 'line 2 status' });
    // …and the input block <ToolInputBlock/> derives from the stashed args,
    // NOT from tool_result's `input` field (which carries the RESULT text).
    const { summary } = formatToolInput(trace.tool, trace.toolInput);
    expect(summary).toContain('line 2 status');
    expect(summary).not.toContain('RESULT text');
    expect(trace.resultMd).toBe('3 rows');
    expect(trace.visible).toBe(true);
  });

  it('keeps the KG actual-question pill label (kgq) through the tool_result relabel', () => {
    const turn = lastTurn(replay(REPLAY));
    const pill = turn.assistant.pills.get('kg1')!;
    expect(pill.kgq).toBe(true);
    // Set at tool_start from the question input; the explore_graph relabel
    // (labelSrc contains 'entity' → 'Querying entities') must NOT clobber it.
    expect(pill.label).toBe('KG: which systems know line 2');
  });

  it('relabels a NON-kgq explore_graph pill from the tool_result', () => {
    const store = createThreadStore();
    store.startTurn('q');
    // No derivable question → generic label, kgq unset.
    store.applyEvent('tool_start', { tool: 'explore_graph', tool_id: 'kg2', input: null });
    let pill = lastTurn(store).assistant.pills.get('kg2')!;
    expect(pill.kgq).toBe(false);
    expect(pill.label).toBe('Querying knowledge graph');
    store.applyEvent('tool_result', {
      tool: 'explore_graph',
      tool_id: 'kg2',
      input: 'MATCH (e:Entity) RETURN count(e)',
      summary: 'Looked up Entity nodes in the knowledge graph.',
    });
    pill = lastTurn(store).assistant.pills.get('kg2')!;
    expect(pill.label).toBe('Querying entities');
  });

  it('collapses the reasoning trace on done with the activity summary as its label', () => {
    const turn = lastTurn(replay(REPLAY));
    expect(turn.assistant.reasoningCollapsed).toBe(true);
    expect(turn.assistant.toolCallCount).toBe(3);
    const fold = summarizeReasoning([...turn.assistant.pills.values()], turn.assistant.traces);
    expect(fold.text).toBe('Searched the knowledge graph · Queried MES and ERP');
    expect(fold.meta).toBe('3 calls');
    expect(fold.tone).toBe('ok');
    // All pills done after the stream settles.
    for (const pill of turn.assistant.pills.values()) expect(pill.done).toBe(true);
  });

  it('sweeps ALL pills done on done — a stranded result-less pill never pulses forever', () => {
    const store = createThreadStore();
    store.startTurn('q');
    // A call whose tool_result/tool_end never arrive (stream hiccup)…
    store.applyEvent('tool_start', { tool: 'query_mes', tool_id: 's1', input: {} });
    // …displaced as the ACTIVE pill by a later call that completes normally,
    // so s1 is neither own-result-finalized nor the active pill at done.
    store.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 's2', input: {} });
    store.applyEvent('tool_result', { tool: 'erp___query_erp', tool_id: 's2', input: 'ok', summary: '1 row' });
    store.applyEvent('tool_end', { tool: 'erp___query_erp', tool_id: 's2' });
    store.applyEvent('token', { text: 'answer' });
    store.applyEvent('done', {});
    const a = lastTurn(store).assistant;
    expect(a.pills.get('s1')!.done).toBe(true);
    for (const pill of a.pills.values()) expect(pill.done).toBe(true);
    expect(a.activePillId).toBe(null);
  });

  it('numbers citations sequentially for non-explore_graph tools only', () => {
    const turn = lastTurn(replay(REPLAY));
    const cites = turn.assistant.citations;
    expect(cites.map((c) => [c.num, c.tool, c.label])).toEqual([
      [1, 'query_mes', 'MES'], // getSystemLabel('query_mes') — shared resolver fallback
      [2, 'erp___query_erp', 'ERP'],
    ]);
    expect(cites.every((c) => c.nodeId === `system-${c.tool}`)).toBe(true);
  });

  it('links the finding node id passed with tool_result to the matching citation', () => {
    const store = createThreadStore();
    store.startTurn('q');
    store.applyEvent('tool_start', { tool: 'query_mes', tool_id: 't1', input: {} });
    store.applyEvent(
      'tool_result',
      { tool: 'query_mes', tool_id: 't1', input: 'r', summary: 'rows' },
      'finding-42',
    );
    const cites = lastTurn(store).assistant.citations;
    expect(cites[0].findingNodeId).toBe('finding-42');
  });

  it('keeps one pill/trace PER CALL for duplicate tools without tool_id (no overwrite)', () => {
    const store = createThreadStore();
    store.startTurn('q');
    // Two calls to the same tool, no tool_id on either (the legacy UI
    // appended fresh DOM per call — the Map must not overwrite).
    store.applyEvent('tool_start', { tool: 'query_mes', input: { q: 'first' } });
    store.applyEvent('tool_result', { tool: 'query_mes', input: 'r1', summary: 'first rows' });
    store.applyEvent('tool_end', { tool: 'query_mes' });
    store.applyEvent('tool_start', { tool: 'query_mes', input: { q: 'second' } });
    store.applyEvent('tool_result', { tool: 'query_mes', input: 'r2', summary: 'second rows' });
    store.applyEvent('tool_end', { tool: 'query_mes' });
    const a = lastTurn(store).assistant;
    // Pill count matches toolCallCount; the first trace content survives.
    expect(a.toolCallCount).toBe(2);
    expect(a.pills.size).toBe(2);
    expect(a.traces.size).toBe(2);
    const traces = [...a.traces.values()];
    expect(traces[0].toolInput).toEqual({ q: 'first' });
    expect(traces[0].resultMd).toBe('first rows');
    // tool_result without tool_id resolves to the LAST matching call.
    expect(traces[1].toolInput).toEqual({ q: 'second' });
    expect(traces[1].resultMd).toBe('second rows');
    // Pills and traces stay key-paired (thread-clicks toggles by data-tool-id).
    expect([...a.pills.keys()]).toEqual([...a.traces.keys()]);
  });

  it('marks a turn canceled on abort, preserving partial content (quiet marker)', () => {
    const store = createThreadStore();
    store.startTurn('q');
    store.applyEvent('token', { text: 'partial answer text' });
    store.applyEvent('tool_start', { tool: 'query_mes', tool_id: 't1', input: {} });
    store.abortTurn();
    const a = lastTurn(store).assistant;
    expect(a.canceled).toBe(true);
    expect(a.streaming).toBe(false);
    expect(a.error).toBeNull(); // no red banner — quiet marker only
    // Partial content is preserved (pill + the preserved text run).
    expect(a.pills.size).toBe(1);
    expect(a.answerMd).toBeNull(); // never finalized
  });

  it('stores the error text on `event: error` (partial content kept, error shown below)', () => {
    const store = createThreadStore();
    store.startTurn('q');
    store.applyEvent('token', { text: 'partial' });
    store.applyEvent('error', { text: 'agent runtime unavailable' });
    const a = lastTurn(store).assistant;
    expect(a.error).toBe('agent runtime unavailable');
    expect(a.streaming).toBe(false);
    expect(a.canceled).toBe(false);
  });

  it('splits ---FOLLOWUPS--- into chips and strips proposal blocks from the answer', () => {
    const store = createThreadStore();
    store.startTurn('q');
    store.applyEvent('token', {
      text:
        'The answer body.\n---VALUE-MAPPING-PROPOSAL---\nsecret\n---END-PROPOSAL---\n' +
        '---FOLLOWUPS---\nQ1: What about line 3?\nA1: Schedule PM\n',
    });
    store.applyEvent('done', {});
    const a = lastTurn(store).assistant;
    expect(a.answerMd).toBe('The answer body.');
    expect(a.followups).toEqual(['What about line 3?']);
    expect(a.actions).toEqual(['Schedule PM']);
  });

  it('assembles the analysis panel INPUTS on done (metadata + browser counts)', () => {
    const store = createThreadStore();
    store.startTurn('q');
    store.applyEvent('tool_start', { tool: 'explore_graph', tool_id: 'kg1', input: null });
    store.applyEvent('tool_result', {
      tool: 'explore_graph',
      tool_id: 'kg1',
      summary: 'Found 4 concept mappings for the requested entities.',
    });
    store.applyEvent('tool_start', { tool: 'query_mes', tool_id: 't1', input: {} });
    store.applyEvent('tool_result', { tool: 'query_mes', tool_id: 't1', summary: 'rows' });
    store.applyEvent('metadata', {
      intelligence_summary: { total_tool_calls: 2, neptune_queries: 1, new_proposed_mappings: 1 },
    });
    store.applyEvent('done', {});
    const panel = lastTurn(store).assistant.analysisPanel!;
    expect(panel).not.toBeNull();
    expect(panel.totalTools).toBe(2);
    expect(panel.graphLookups).toBe(1);
    expect(panel.noDataSteps).toBe(0);
    // Structured data only (zero-innerHTML): the browser composition renders
    // from toolCallSequence + counts, the Self-Learning link from the summary.
    expect(panel.hasBrowser).toBe(true);
    expect(panel.summary?.new_proposed_mappings).toBe(1);
    // graph-guided classification: the system call followed a useful KG result
    const seq = lastTurn(store).assistant.scratch.toolCallSequence;
    expect(seq).toHaveLength(1);
    expect(seq[0].guided).toBe(true);
  });

  it('records the run/pill interleave via segBoundary', () => {
    const turn = lastTurn(replay(REPLAY));
    // Run 1 ('Checking the line…') precedes kg1; no run between kg1 and t1;
    // the long answer run precedes t2.
    expect(turn.assistant.pills.get('kg1')!.segBoundary).toBe(1);
    expect(turn.assistant.pills.get('t1')!.segBoundary).toBe(1);
    expect(turn.assistant.pills.get('t2')!.segBoundary).toBe(2);
  });
});

describe('thread store — rAF-coalesced token notify', () => {
  it('coalesces token notifications into one per animation frame', () => {
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => frames.push(cb));
    vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.splice(id - 1, 1));

    const store = createThreadStore();
    const notify = vi.fn();
    store.subscribe(notify);
    store.startTurn('q'); // 1 sync notify
    expect(notify).toHaveBeenCalledTimes(1);

    store.applyEvent('token', { text: 'a' });
    store.applyEvent('token', { text: 'b' });
    store.applyEvent('token', { text: 'c' });
    // No sync notify per token — exactly ONE frame scheduled.
    expect(notify).toHaveBeenCalledTimes(1);
    expect(frames).toHaveLength(1);
    frames.shift()!(0);
    expect(notify).toHaveBeenCalledTimes(2);
    // The accumulated text is visible in one shot.
    expect(lastTurn(store).assistant.segments.streamingText).toBe('abc');
  });

  it('tool_start flushes a pending token frame immediately (flushStreamNow)', () => {
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => frames.push(cb));
    const canceled: number[] = [];
    vi.stubGlobal('cancelAnimationFrame', (id: number) => canceled.push(id));

    const store = createThreadStore();
    const notify = vi.fn();
    store.subscribe(notify);
    store.startTurn('q');
    notify.mockClear();

    store.applyEvent('token', { text: 'thinking…' });
    expect(notify).not.toHaveBeenCalled();
    store.applyEvent('tool_start', { tool: 'query_mes', tool_id: 't1', input: {} });
    // Pending frame canceled + flushed synchronously, then the tool_start
    // notify — subscribers saw the buffered text before the pill.
    expect(canceled).toHaveLength(1);
    expect(notify).toHaveBeenCalledTimes(2);
  });

  it('falls back to synchronous notify when rAF is unavailable (node)', () => {
    // vitest node env: no requestAnimationFrame global by default.
    expect(typeof globalThis.requestAnimationFrame).toBe('undefined');
    const store = createThreadStore();
    const notify = vi.fn();
    store.subscribe(notify);
    store.startTurn('q');
    notify.mockClear();
    store.applyEvent('token', { text: 'x' });
    expect(notify).toHaveBeenCalledTimes(1);
  });
});

describe('thread store — snapshot + lifecycle', () => {
  it('returns a fresh snapshot identity on every notify (useSyncExternalStore contract)', () => {
    const store = createThreadStore();
    const s0 = store.getState();
    store.startTurn('q');
    const s1 = store.getState();
    expect(s1).not.toBe(s0);
    store.applyEvent('tool_start', { tool: 'query_mes', tool_id: 't1', input: {} });
    expect(store.getState()).not.toBe(s1);
  });

  it("clear() empties the thread ('+ New' semantics)", () => {
    const store = createThreadStore();
    store.startTurn('q');
    store.applyEvent('token', { text: 'x' });
    store.setHistory([{ role: 'user', content: 'old' }]);
    store.startTurn('q2');
    store.clear();
    expect(store.getState().turns).toEqual([]);
    expect(store.getState().history).toEqual([]);
  });

  it('setHistory replaces the whole thread (session rehydration semantics)', () => {
    const store = createThreadStore();
    store.startTurn('live turn');
    store.setHistory([
      { role: 'user', content: 'earlier question' },
      { role: 'assistant', content: 'earlier answer' },
    ]);
    const s = store.getState();
    expect(s.turns).toEqual([]); // old #messages innerHTML='' before re-render
    expect(s.history.map((m) => m.role)).toEqual(['user', 'assistant']);
  });

  it('WIPE GUARD: setHistory([]) over a non-empty live thread is a no-op + warns (chat-persistence regression)', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const store = createThreadStore();
    store.startTurn('live question');
    store.applyEvent('token', { text: 'partial answer' });
    store.setHistory([]); // empty rehydrate result must NOT wipe live turns
    const s = store.getState();
    expect(s.turns).toHaveLength(1);
    expect(s.turns[0].user.text).toBe('live question');
    expect(warn).toHaveBeenCalledTimes(1);
    warn.mockRestore();
  });

  it('WIPE GUARD: setHistory([]) on an EMPTY thread still applies (no live content to lose)', () => {
    const store = createThreadStore();
    store.setHistory([]);
    expect(store.getState()).toMatchObject({ history: [], turns: [] });
  });

  it('toggleTrace flips visibility and bumps the turn rev (W3.1 memo key)', () => {
    const store = createThreadStore();
    const turnId = store.startTurn('q');
    store.applyEvent('tool_start', { tool: 'query_mes', tool_id: 't1', input: {} });
    store.applyEvent('tool_result', { tool: 'query_mes', tool_id: 't1', summary: 'rows' });
    const turn = lastTurn(store);
    expect(turn.assistant.traces.get('t1')!.visible).toBe(true);
    const revBefore = turn.rev;
    store.toggleTrace(turnId, 't1');
    expect(turn.assistant.traces.get('t1')!.visible).toBe(false);
    expect(turn.rev).toBeGreaterThan(revBefore);
    store.toggleTrace(turnId, 't1');
    expect(turn.assistant.traces.get('t1')!.visible).toBe(true);
  });

  it('bumps the turn rev on every mutating event (per-turn memo contract)', () => {
    const store = createThreadStore();
    store.startTurn('q');
    const turn = lastTurn(store);
    expect(turn.rev).toBe(0);
    store.applyEvent('token', { text: 'a' });
    expect(turn.rev).toBe(1);
    store.applyEvent('tool_start', { tool: 'query_mes', tool_id: 't1', input: {} });
    expect(turn.rev).toBe(2);
    store.applyEvent('done', {});
    expect(turn.rev).toBe(3);
  });

  it('captures the display-text override separately from the raw question', () => {
    const store = createThreadStore();
    store.startTurn('raw question for the api', 'Pretty display text');
    const turn = lastTurn(store);
    expect(turn.user.text).toBe('Pretty display text');
    expect(turn.user.raw).toBe('raw question for the api');
  });

  it('ignores events with no active turn', () => {
    const store = createThreadStore();
    expect(() => store.applyEvent('token', { text: 'x' })).not.toThrow();
    expect(() => store.abortTurn()).not.toThrow();
    expect(store.getState().turns).toEqual([]);
  });
});

describe('parallel-batch pill honesty (own-result completion)', () => {
  it('a sibling tool_start does NOT green the running pill; its own result does', () => {
    const store = createThreadStore();
    store.startTurn('q');
    store.applyEvent('tool_start', { tool: 'mes___query', tool_id: 't1', input: {} });
    store.applyEvent('tool_start', { tool: 'erp___query', tool_id: 't2', input: {} });
    const a = () => lastTurn(store).assistant;
    expect(a().pills.get('t1')!.done).toBe(false); // sibling start must not complete t1
    store.applyEvent('tool_result', { tool: 'mes___query', tool_id: 't1', summary: 'ok', input: 'ok' });
    expect(a().pills.get('t1')!.done).toBe(true);
    expect(a().pills.get('t2')!.done).toBe(false); // t2 still honestly running
    store.applyEvent('done', {});
  });
});
