// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Activity line for back-to-back tool calls (chat/tool-activity.ts +
 * <ToolActivityGroup/>): grouping, the present/past-tense summary, honest
 * per-call outcomes, the click-to-expand contract, the thinking-indicator
 * hand-off, and the motion/reduced-motion CSS contract. */
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { ChatThread } from '../chat/ChatThread';
import { boundRecordFor, type ProvenanceRecord } from '../chat/provenance';
import { createThreadStore, type ThreadStore } from '../chat/thread-store';
import {
  callRowLabel,
  describeCalls,
  formatDuration,
  groupBackToBack,
  groupSpanMs,
  summarizeToolGroup,
} from '../chat/tool-activity';
import { systemDisplayName } from '../chat/tool-labels';
import type { ToolPill, ToolTrace } from '../chat/turn-model';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const pill = (toolId: string, tool: string, extra: Partial<ToolPill> = {}): ToolPill => ({
  toolId,
  tool,
  label: tool,
  kgq: false,
  done: false,
  segBoundary: 0,
  ...extra,
});

const trace = (toolId: string, tool: string, extra: Partial<ToolTrace> = {}): ToolTrace => ({
  toolId,
  tool,
  toolInput: null,
  resultMd: '',
  visible: false,
  ...extra,
});

describe('groupBackToBack', () => {
  it('folds calls with no narration between them and splits on a new run', () => {
    const groups = groupBackToBack([
      pill('a', 'erp___query_erp', { segBoundary: 1 }),
      pill('b', 'cmms___query_cmms', { segBoundary: 1 }),
      pill('c', 'plm___query_plm', { segBoundary: 2 }),
    ]);
    expect(groups.map((g) => g.map((p) => p.toolId))).toEqual([['a', 'b'], ['c']]);
  });
});

describe('describeCalls / summarizeToolGroup', () => {
  it('names only the in-flight calls in present tense, with progress', () => {
    const pills = [
      pill('a', 'erp___query_erp', { done: true }),
      pill('b', 'cmms___query_cmms'),
      pill('c', 'plm___query_plm'),
    ];
    const traces = new Map([['a', trace('a', 'erp___query_erp', { outcome: 'success', resultAt: 1 })]]);
    const s = summarizeToolGroup(pills, traces);
    expect(s.active).toBe(true);
    expect(s.text).toBe('Querying CMMS and PLM');
    expect(s.meta).toBe('1 of 3');
    expect(s.tone).toBe('active');
  });

  it('summarizes the whole group in past tense with honest issue counts and span', () => {
    const tools = ['digital-thread___explore_graph', 'erp___query_erp', 'cmms___query_cmms'];
    const pills = tools.map((t, i) => pill(`p${i}`, t, { done: true }));
    const traces = new Map(
      tools.map((t, i) => [
        `p${i}`,
        trace(`p${i}`, t, {
          startedAt: 1_000,
          resultAt: 1_000 + (i + 1) * 1_000,
          outcome: i === 2 ? 'failed' : 'success',
          outcomeReason: i === 2 ? 'the result opens with the error marker "Error:"' : '',
        }),
      ]),
    );
    const s = summarizeToolGroup(pills, traces);
    expect(s.active).toBe(false);
    expect(s.text).toBe('Searched the knowledge graph · Queried ERP and CMMS');
    expect(s.meta).toBe('3 calls · 1 failed · 3.0s');
    expect(s.tone).toBe('error');
  });

  it('keeps a lone KG call question and counts repeats / long system lists', () => {
    expect(
      describeCalls([pill('k', 'explore_graph', { kgq: true, label: 'KG: which systems know line 2' })], 'past'),
    ).toBe('Searched the knowledge graph: which systems know line 2');
    expect(
      describeCalls([pill('a', 'erp___query_erp'), pill('b', 'erp___query_erp')], 'past'),
    ).toBe('Queried ERP (2×)');
    expect(
      describeCalls(
        ['erp', 'cmms', 'plm', 'mes-det', 'mes-cr'].map((s) => pill(s, `${s}___query`)),
        'present',
      ),
    ).toBe('Querying ERP, CMMS and 3 more systems');
  });

  it('gives a call that finished without its own result no verdict', () => {
    const s = summarizeToolGroup([pill('a', 'erp___query_erp', { done: true })], new Map());
    expect(s.tone).toBe('neutral');
    expect(s.meta).toBe('');
  });

  it('phrases the orchestrator local tools and graph-target ops in plain words', () => {
    expect(describeCalls([pill('m', 'propose_metric_definition')], 'present')).toBe(
      'Proposing a metric definition',
    );
    expect(describeCalls([pill('v', 'digital-thread___verify_relationship')], 'past')).toBe(
      'Checked a relationship',
    );
    expect(describeCalls([pill('s', 'get_system_schema'), pill('t', 'get_system_schema')], 'past')).toBe(
      'Read 2 schemas',
    );
    expect(describeCalls([pill('x', 'some_new_tool')], 'past')).toBe('Ran some new tool');
    expect(
      describeCalls([pill('a', 'erp___query_erp'), pill('s', 'get_system_schema')], 'stopped'),
    ).toBe('Stopped querying ERP · Stopped reading a schema');
  });

  it('measures the group span on the server clock when every call has both stamps', () => {
    const server = [
      trace('a', 'erp', { startedAt: 5_000, resultAt: 5_010, serverStartTs: 1_000, serverEndTs: 2_500, elapsedMs: 1_500 }),
      trace('b', 'cmms', { startedAt: 5_000, resultAt: 5_012, serverStartTs: 1_100, serverEndTs: 4_100, elapsedMs: 3_000 }),
    ];
    // Browser receipts bunched within 12 ms (one wire burst); server truth is 3.1 s.
    expect(groupSpanMs(server)).toBe(3_100);
    const mixed = [server[0], trace('c', 'plm', { startedAt: 5_000, resultAt: 7_000 })];
    expect(groupSpanMs(mixed)).toBe(2_000); // falls back to ONE clock, never mixes
    expect(groupSpanMs([])).toBeNull();
  });

  it('labels expanded rows with the target and what was asked', () => {
    expect(
      callRowLabel(
        pill('a', 'erp___query_erp', { label: 'ERP — Query Erp' }),
        trace('a', 'erp___query_erp', { toolInput: { question: 'orders for B-2847' } }),
      ),
    ).toBe('ERP · orders for B-2847');
    expect(callRowLabel(pill('k', 'explore_graph', { kgq: true, label: 'KG: q' }), undefined)).toBe('KG: q');
    expect(callRowLabel(pill('m', 'propose_metric_definition', { label: 'Propose' }), undefined)).toBe('Propose');
  });

  it('formats unknown system ids generically and durations compactly', () => {
    expect(systemDisplayName('mes-ind')).toBe('MES Ind');
    expect(systemDisplayName('quality_lab')).toBe('Quality Lab');
    expect(systemDisplayName('sitewise-direct')).toBe('SiteWise');
    expect(formatDuration(420)).toBe('0.4s');
    expect(formatDuration(12_400)).toBe('12s');
    expect(formatDuration(65_000)).toBe('1m 05s');
  });
});

describe('<ToolActivityGroup/> in the thread', () => {
  let container: HTMLDivElement;
  let root: Root;
  let store: ThreadStore;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    store = createThreadStore();
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  const render = () => act(() => root.render(<ChatThread store={store} />));

  it('renders back-to-back calls as ONE active line beside the turn clock', () => {
    act(() => {
      store.startTurn('q');
      store.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 'a', input: { question: 'x' } });
      store.applyEvent('tool_start', { tool: 'cmms___query_cmms', tool_id: 'b', input: { question: 'y' } });
    });
    render();
    const lines = container.querySelectorAll('.tool-activity');
    expect(lines).toHaveLength(1);
    const line = lines[0] as HTMLElement;
    expect(line.dataset.state).toBe('active');
    expect(line.querySelector('.tool-activity-text')?.classList.contains('shimmer-text')).toBe(true);
    expect(line.querySelector('.tool-activity-text')?.textContent).toBe('Querying ERP and CMMS');
    expect(line.querySelector('.tool-activity-meta')?.textContent).toBe('0 of 2');
    // The turn clock stays: the line says what runs, the clock how long.
    expect(container.querySelector('.thinking-inline .thinking-elapsed')?.textContent).toMatch(
      /^Working\.\.\. \d:\d\d$/,
    );
  });

  it('expands on click and keeps the per-call pill/trace contract inside', () => {
    act(() => {
      store.startTurn('q');
      store.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 'a', input: { question: 'x' } });
      store.applyEvent('tool_result', { tool: 'erp___query_erp', tool_id: 'a', summary: '3 rows', elapsed_ms: 1200 });
      store.applyEvent('tool_start', { tool: 'cmms___query_cmms', tool_id: 'b', input: { question: 'y' } });
      store.applyEvent('tool_result', { tool: 'cmms___query_cmms', tool_id: 'b', summary: 'Error: connection refused' });
      store.applyEvent('done', { status: 'complete' });
    });
    render();
    const line = container.querySelector<HTMLElement>('.tool-activity')!;
    const button = line.querySelector<HTMLButtonElement>('button.tool-activity-line')!;
    expect(line.dataset.state).toBe('done');
    expect(line.dataset.tone).toBe('error');
    expect(button.getAttribute('aria-expanded')).toBe('false');
    expect(line.querySelector('.tool-activity-meta')?.textContent).toMatch(/^2 calls · 1 failed/);
    act(() => button.click());
    expect(button.getAttribute('aria-expanded')).toBe('true');
    expect(line.dataset.open).toBe('true');
    expect(document.getElementById(button.getAttribute('aria-controls')!)).not.toBeNull();
    const pills = [...line.querySelectorAll<HTMLElement>('.tool-pill.inline')];
    expect(pills.map((p) => p.dataset.toolId)).toEqual(['a', 'b']);
    const rows = [...line.querySelectorAll<HTMLElement>('.tool-activity-call')];
    expect(rows.map((r) => r.dataset.outcome)).toEqual(['success', 'failed']);
    expect(pills[1].title).toContain('error marker');
    expect(rows[0].querySelector('.tool-activity-call-time')?.textContent).toBe('1.2s');
    expect(line.querySelector('.tool-trace[data-tool-id="a"] .trace-result')?.textContent).toContain('3 rows');
    act(() => button.click());
    expect(line.dataset.open).toBe('false');
  });

  it('stops pulsing when the turn is canceled mid-call', () => {
    act(() => {
      store.startTurn('q');
      store.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 'a' });
      store.abortTurn();
    });
    render();
    const line = container.querySelector<HTMLElement>('.tool-activity')!;
    expect(line.dataset.state).toBe('done');
    expect(line.dataset.tone).toBe('neutral');
    // Never reads as a finished query: the call was stopped, not answered.
    expect(line.querySelector('.tool-activity-text')?.textContent).toBe('Stopped querying ERP');
    expect(line.querySelector<HTMLElement>('.tool-activity-call')?.dataset.outcome).toBe('stopped');
  });

  it('names stopped calls next to the finished ones and shows no partial duration', () => {
    act(() => {
      store.startTurn('q');
      store.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 'a' });
      store.applyEvent('tool_start', { tool: 'cmms___query_cmms', tool_id: 'b' });
      store.applyEvent('tool_result', { tool: 'erp___query_erp', tool_id: 'a', summary: 'ok', elapsed_ms: 900 });
      store.abortTurn();
    });
    render();
    const line = container.querySelector<HTMLElement>('.tool-activity')!;
    expect(line.querySelector('.tool-activity-text')?.textContent).toBe('Queried ERP · Stopped querying CMMS');
    // 2 calls, no duration: CMMS never ended, so any span would be partial.
    expect(line.querySelector('.tool-activity-meta')?.textContent).toBe('2 calls');
  });

  it('links each call row to its trace with aria-controls', () => {
    act(() => {
      store.startTurn('q');
      store.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 'a', input: { question: 'x' } });
      store.applyEvent('tool_result', { tool: 'erp___query_erp', tool_id: 'a', summary: 'ok' });
    });
    render();
    const row = container.querySelector<HTMLButtonElement>('.tool-pill[data-tool-id="a"]')!;
    const traceId = row.getAttribute('aria-controls')!;
    expect(document.getElementById(traceId)?.classList.contains('tool-trace')).toBe(true);
  });

  it('starts a new line when narration separates calls', () => {
    act(() => {
      store.startTurn('q');
      store.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 'a' });
      store.applyEvent('tool_result', { tool: 'erp___query_erp', tool_id: 'a', summary: 'ok' });
      store.applyEvent('token', { text: 'Now checking maintenance.' });
      store.applyEvent('tool_start', { tool: 'cmms___query_cmms', tool_id: 'b' });
    });
    render();
    expect(container.querySelectorAll('.tool-activity')).toHaveLength(2);
    act(() => {
      store.applyEvent('tool_result', { tool: 'cmms___query_cmms', tool_id: 'b', summary: 'ok' });
    });
    expect(container.querySelector('.thinking-inline')?.textContent).toContain('Working...');
  });
});

describe('store lifecycle edges the activity line depends on', () => {
  const last = (s: ThreadStore) => s.getState().turns[s.getState().turns.length - 1].assistant;

  it('drops a tool_start / token that arrives after the turn finished', () => {
    const s = createThreadStore();
    s.startTurn('q');
    s.applyEvent('token', { text: 'answer' });
    s.applyEvent('done', { status: 'complete' });
    s.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 'late' });
    s.applyEvent('token', { text: ' more' });
    const a = last(s);
    expect(a.pills.size).toBe(0);
    expect(a.answerMd).toBe('answer');
  });

  it('settles running calls as stopped when an error frame ends the turn', () => {
    const s = createThreadStore();
    s.startTurn('q');
    s.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 'a' });
    s.applyEvent('tool_result', { tool: 'erp___query_erp', tool_id: 'a', summary: 'ok' });
    s.applyEvent('tool_start', { tool: 'cmms___query_cmms', tool_id: 'b' });
    s.applyEvent('error', { text: 'agent runtime unavailable' });
    const a = last(s);
    const pills = [...a.pills.values()];
    expect(pills.map((p) => p.done)).toEqual([true, true]);
    expect(pills.map((p) => !!p.stopped)).toEqual([false, true]);
    expect(a.activePillId).toBeNull();
    const summary = summarizeToolGroup(pills, a.traces);
    expect(summary.active).toBe(false);
    expect(summary.text).toBe('Queried ERP · Stopped querying CMMS');
  });

  it('a Stop that races an error frame clears the error and keeps the calls stopped', () => {
    const s = createThreadStore();
    s.startTurn('q');
    s.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 'a' });
    s.applyEvent('error', { text: 'stream dropped' });
    s.abortTurn();
    const a = last(s);
    expect(a.error).toBeNull();
    expect(a.canceled).toBe(true);
    expect([...a.pills.values()].map((p) => !!p.stopped)).toEqual([true]);
  });

  it('keeps every call when the server repeats a tool_id, binding results in order', () => {
    const s = createThreadStore();
    s.startTurn('q');
    s.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 'dup', input: { question: 'one' } });
    s.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 'dup', input: { question: 'two' } });
    s.applyEvent('tool_result', { tool: 'erp___query_erp', tool_id: 'dup', summary: 'first rows' });
    const a = last(s);
    expect(a.pills.size).toBe(2);
    const [first, second] = [...a.traces.values()];
    expect(first.resultMd).toBe('first rows');
    expect(second.resultAt).toBeUndefined();
    expect([...a.pills.values()].map((p) => p.done)).toEqual([true, false]);
    s.applyEvent('tool_result', { tool: 'erp___query_erp', tool_id: 'dup', summary: 'second rows' });
    expect(second.resultMd).toBe('second rows');
  });

  it('fails closed on provenance when a tool_id repeats (no evidence reuse across calls)', () => {
    const s = createThreadStore();
    s.startTurn('q');
    s.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 'dup' });
    s.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 'dup' });
    const a = last(s);
    // The server dedups by tool_id and keeps the LAST occurrence's ordinal.
    const records = [{ tool_id: 'dup', citation_num: 2 }] as unknown as ProvenanceRecord[];
    expect(boundRecordFor(records, a.pills.values(), 1)).toBeNull();
    expect(boundRecordFor(records, a.pills.values(), 2)).toBe(records[0]);
    // A unique tool_id still primary-binds exactly as before.
    const u = createThreadStore();
    u.startTurn('q');
    u.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 'one' });
    const rec = [{ tool_id: 'one', citation_num: 9 }] as unknown as ProvenanceRecord[];
    expect(boundRecordFor(rec, last(u).pills.values(), 1)).toBe(rec[0]);
  });

  it('never lets a synthesized key collide with a real wire id', () => {
    const s = createThreadStore();
    s.startTurn('q');
    s.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 'x' });
    s.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 'x#3' });
    s.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 'x' }); // would synthesize x#3
    expect(last(s).pills.size).toBe(3);
  });

  it('pairs a name-only tool_end with the call whose result just landed (parallel batch)', () => {
    const s = createThreadStore();
    s.startTurn('q');
    s.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 'A' });
    s.applyEvent('tool_start', { tool: 'cmms___query_cmms', tool_id: 'B' });
    s.applyEvent('tool_result', { tool: 'erp___query_erp', tool_id: 'A', summary: 'ok' });
    s.applyEvent('tool_end', { tool: 'erp___query_erp' }); // the server sends the name only
    const a = last(s);
    // B is still in flight — tool_end(A) must not settle it.
    expect(a.pills.get('A')!.done).toBe(true);
    expect(a.pills.get('B')!.done).toBe(false);
    // Legacy stream with no result: tool_end settles that tool's running call.
    s.applyEvent('tool_end', { tool: 'cmms___query_cmms' });
    expect(a.pills.get('B')!.done).toBe(true);
  });
});

describe('activity-line motion contract', () => {
  const dir = path.dirname(fileURLToPath(import.meta.url));
  const css = readFileSync(path.resolve(dir, '../chat/tool-activity.css'), 'utf-8');
  const base = readFileSync(path.resolve(dir, '../styles/base.css'), 'utf-8');

  it('expands with grid rows (no max-height hack) and hides collapsed rows from AT', () => {
    expect(css).toMatch(/transition:\s*grid-template-rows var\(--motion-base\)/);
    expect(css).not.toMatch(/max-height/);
    expect(css).toMatch(/visibility:\s*hidden/);
  });

  it('is covered by the single reduced-motion block', () => {
    const start = base.search(/@media\s*\(\s*prefers-reduced-motion/);
    const block = base.slice(start, base.indexOf('\n  }\n', start));
    for (const selector of ['.tool-activity,', '.tool-activity-status', '.tool-activity-call::before', '.shimmer-text']) {
      expect(block, `${selector} must be reduced-motion covered`).toContain(selector);
    }
  });
});
