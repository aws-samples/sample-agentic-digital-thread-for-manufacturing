// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — timeout/partial honesty + bidirectional graph↔trace↔HUD correlation
 * + the ToolHud collapse fix (arch-interactivity wave).
 *
 * Evidence driving this suite:
 *  - timed-out calls rendered as '○ no data' ("call it what it is — request
 *    timed out"): a failed/empty result whose server elapsed_ms exceeds the
 *    budget is a TIMEOUT (red '⏱ request timed out' chip, 'Request timed out
 *    after Ns' trace body, honest red graph node);
 *  - the SiteWise budget marker ('partial: stopped at …') is a PARTIAL —
 *    amber, data kept;
 *  - failed calls left NOTHING in the graph (placeholder consumed silently);
 *  - collapsed HUD rendered overlapping colored slivers (inline padding beat
 *    the CSS max-height squeeze). */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  classifyToolResult,
  partialNote,
  timeoutText,
  TIMEOUT_ELAPSED_MS,
  type ArchStore,
  type HudEntry,
  type InvCallDetail,
  type TraceEntry,
} from '../lib/arch-store';
import { clearCorrelation, correlationStore, selectCall } from '../views/architecture/correlation-store';
import ToolHud, { hudMark } from '../views/architecture/ToolHud';
import TraceEntries from '../views/architecture/TraceEntries';
import { installArchStoreDisposal, trackedArchStore as createArchStore } from './arch-store-tracking';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

installArchStoreDisposal();

function ask(store: ArchStore, question: string): void {
  store.handleArchEvent('user_msg', { question });
}

/* ── classifyToolResult: timeout + partial precedence ────────────────────── */

describe('classifyToolResult: timeout/partial layer', () => {
  it('a failed/empty envelope with elapsed over the budget is a TIMEOUT (timeout beats empty AND failed)', () => {
    expect(classifyToolResult('', 60_000)).toBe('timeout');
    expect(classifyToolResult('No records found.', 60_000)).toBe('timeout');
    expect(classifyToolResult('Error: gateway gave up', 60_000)).toBe('timeout');
    expect(classifyToolResult('', TIMEOUT_ELAPSED_MS + 1)).toBe('timeout');
  });

  it('under the budget the base classification is unchanged', () => {
    expect(classifyToolResult('', 30_000)).toBe('empty');
    expect(classifyToolResult('No records found.', TIMEOUT_ELAPSED_MS)).toBe('empty');
    expect(classifyToolResult('Error: boom', 5_000)).toBe('failed');
    expect(classifyToolResult('', undefined)).toBe('empty');
  });

  it('a SUCCESSFUL long call is NOT a timeout (data came back)', () => {
    expect(classifyToolResult('3 records found\n| a |', 90_000)).toBe('success');
  });

  it('the SiteWise budget marker is a PARTIAL, even on a long call', () => {
    expect(classifyToolResult('12 records found\npartial: stopped at asset 7 of 20')).toBe('partial');
    expect(classifyToolResult('Partial: stopped at 2026-07-01 (budget)', 90_000)).toBe('partial');
  });

  it('timeoutText / partialNote produce the honest labels', () => {
    expect(timeoutText(60_400)).toBe('Request timed out after 60s');
    expect(partialNote('12 records found\npartial: stopped at asset 7')).toBe('partial: stopped at asset 7');
  });
});

/* ── Store: timeout end-to-end (HUD chip + trace body + graph node) ──────── */

describe('arch-store: timeout + partial honesty end-to-end', () => {
  let store: ArchStore;
  beforeEach(() => {
    store = createArchStore();
  });

  const hudTool = (): HudEntry => store.getState().hud.filter((h) => h.kind === 'tool').slice(-1)[0];

  it('empty result + server elapsed_ms 60s → timeout HUD row, "Request timed out after 60s" trace body (NOT "No data returned"), red graph node', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'telem-sitewise___sitewise_query', input: {} });
    store.handleArchEvent('tool_result', {
      tool: 'telem-sitewise___sitewise_query',
      summary: '',
      input: {},
      elapsed_ms: 60_000,
    });
    const row = hudTool();
    expect(row.outcome).toBe('timeout');
    expect(row.errorLine).toBe('Request timed out after 60s');
    const t = store.getState().trace[1]; // [0] is the question anchor row
    expect(t.outcome).toBe('timeout');
    expect(t.text).toBe('Request timed out after 60s');
    expect(t.text).not.toContain('No data returned');
    // Graph honesty: the placeholder converted to a persistent 'timed out' node.
    const node = store.getState().nodes.find((n) => n.outcome === 'timeout')!;
    expect(node).toBeTruthy();
    expect(node.label).toBe('timed out');
    expect(node._inflight).toBeUndefined();
    expect((node._call as InvCallDetail).elapsedMs).toBe(60_000);
    // done does not sweep it (resolved-failed nodes persist like data nodes).
    store.handleArchEvent('done', {});
    expect(store.getState().nodes.some((n) => n.id === node.id)).toBe(true);
  });

  it('timeout beats empty: a timed-out row is NOT recorded for the empty→failed retry upgrade', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: '', input: {}, elapsed_ms: 60_000 });
    // A re-call to the same system must not rewrite the timeout as 'failed
    // (no payload)' — it already carries the honest timeout verdict.
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    const rows = store.getState().hud.filter((h) => h.kind === 'tool');
    expect(rows[0].outcome).toBe('timeout');
    expect(rows[0].errorLine).toBe('Request timed out after 60s');
    expect(store.getState().trace[1].outcome).toBe('timeout');
  });

  it('empty-then-retried upgrade KEEPS working for genuine (sub-budget) empties', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: 'No records found.', input: {}, elapsed_ms: 900 });
    expect(store.getState().hud.filter((h) => h.kind === 'tool')[0].outcome).toBe('empty');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    expect(store.getState().hud.filter((h) => h.kind === 'tool')[0].outcome).toBe('failed');
  });

  it('the timed-out call earns the retry badge on the system re-call', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: '', input: {}, elapsed_ms: 60_000 });
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: '2 records found', input: {} });
    expect(store.getState().trace[2].retry).toBe(true); // [0] is the question anchor row
  });

  it('SiteWise budget marker → partial outcome: amber HUD row with the partial note, data finding kept + tagged', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'telem-sitewise___sitewise_query', input: { q: 'SELECT x FROM y' } });
    store.handleArchEvent('tool_result', {
      tool: 'telem-sitewise___sitewise_query',
      summary: '12 records found\npartial: stopped at asset 7 of 20 (budget)',
      input: { q: 'SELECT x FROM y' },
      elapsed_ms: 48_000,
    });
    const row = hudTool();
    expect(row.outcome).toBe('partial');
    expect(row.errorLine).toContain('partial: stopped at asset 7');
    const t = store.getState().trace[1]; // [0] is the question anchor row
    expect(t.outcome).toBe('partial');
    expect(t.text).toContain('partial: stopped at'); // the partial note IS in the body
    // The data node is kept (real records) and tagged amber-partial.
    const finding = store.getState().nodes.find((n) => String(n.id).startsWith('finding-') && n.outcome === 'partial');
    expect(finding).toBeTruthy();
  });

  it('a partial marker with NO parsable records line still leaves an honest amber node', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'telem-sitewise___sitewise_query', input: {} });
    store.handleArchEvent('tool_result', {
      tool: 'telem-sitewise___sitewise_query',
      summary: 'partial: stopped at asset 3 (budget exhausted)',
      input: {},
    });
    const node = store.getState().nodes.find((n) => n.outcome === 'partial')!;
    expect(node).toBeTruthy();
    expect(String(node.label)).toContain('partial');
    expect((node._call as InvCallDetail).resultSummary).toContain('stopped at asset 3');
  });

  it('unresolved in-flight placeholders are STILL swept on done (only resolved failures persist)', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_start', { tool: 'mes-mil___query_mes_mil', input: {} });
    // Only the ERP call resolves — and it fails.
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: 'Error: boom', input: {} });
    store.handleArchEvent('done', {});
    const s = store.getState();
    expect(s.nodes.filter((n) => n._inflight === true)).toHaveLength(0);
    // The resolved-failed node persists; the unresolved sibling's placeholder is gone.
    const persisted = s.nodes.filter((n) => n.outcome === 'failed');
    expect(persisted).toHaveLength(1);
  });

  it('the failed call carries a callKey linking node ↔ trace ↔ HUD', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: 'Error: boom', input: {} });
    const s = store.getState();
    const row = s.hud.filter((h) => h.kind === 'tool')[0];
    const node = s.nodes.find((n) => n.outcome === 'failed')!;
    expect(node._callKey).toBe(row.id);
    expect(s.trace[1].callKey).toBe(row.id);
  });

  it('successful calls thread the SAME callKey through qinput, finding, trace and HUD', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: { sql: 'SELECT 1' } });
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: '3 records found', input: { sql: 'SELECT 1' } });
    const s = store.getState();
    const row = s.hud.filter((h) => h.kind === 'tool')[0];
    const qinput = s.nodes.find((n) => String(n.id).startsWith('qinput-'))!;
    const finding = s.nodes.find((n) => n.type === 'finding')!;
    expect(qinput._callKey).toBe(row.id);
    expect(finding._callKey).toBe(row.id);
    expect(s.trace[1].callKey).toBe(row.id);
  });
});

/* ── Correlation store round-trips ───────────────────────────────────────── */

describe('correlation store: graph↔trace↔hud round-trips', () => {
  beforeEach(() => clearCorrelation());

  it('selecting from each surface publishes {source, callKey}', () => {
    selectCall('graph', 'hud-erp-q1-1');
    expect(correlationStore.get()).toEqual({ source: 'graph', callKey: 'hud-erp-q1-1' });
    selectCall('trace', 'hud-erp-q1-1');
    expect(correlationStore.get()).toEqual({ source: 'trace', callKey: 'hud-erp-q1-1' });
    selectCall('hud', 'hud-erp-q1-1');
    expect(correlationStore.get()).toEqual({ source: 'hud', callKey: 'hud-erp-q1-1' });
  });

  it('re-selecting the same call from the SAME surface toggles off; a falsy key is ignored', () => {
    selectCall('hud', 'k1');
    selectCall('hud', 'k1');
    expect(correlationStore.get()).toBeNull();
    selectCall('trace', undefined);
    expect(correlationStore.get()).toBeNull();
  });

  it('clearCorrelation empties the selection (empty-space click / new question)', () => {
    selectCall('graph', 'k2');
    clearCorrelation();
    expect(correlationStore.get()).toBeNull();
  });
});

/* ── Component rendering: chips, rows, collapse ──────────────────────────── */

describe('ToolHud / TraceEntries: timeout chips, correlation, collapse', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    clearCorrelation();
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    clearCorrelation();
  });

  const hudEntry = (over: Partial<HudEntry>): HudEntry => ({
    id: 'hud-x-q1-1',
    kind: 'tool',
    question: 1,
    tool: 'telem-sitewise___sitewise_query',
    label: 'Telem Sitewise',
    t0: 0,
    num: 1,
    status: 'ok',
    elapsedMs: 60_000,
    ...over,
  });

  const renderHud = (entries: HudEntry[]) =>
    act(() => {
      root.render(<ToolHud entries={entries} activeQuestionFilter={null} onFilterQuestion={() => {}} />);
    });

  it('a timeout completion renders the red "request timed out" chip (clock, no ✓, not "no data")', () => {
    renderHud([hudEntry({ outcome: 'timeout', errorLine: 'Request timed out after 60s' })]);
    const row = container.querySelector<HTMLElement>('[data-outcome="timeout"]')!;
    expect(row).toBeTruthy();
    expect(row.textContent).toContain('request timed out');
    expect(row.textContent).toContain('⏱');
    expect(row.textContent).not.toContain('✓');
    expect(row.textContent).not.toContain('no data');
    expect(row.title).toBe('Request timed out after 60s');
  });

  it('a partial completion renders the amber ◐ partial chip', () => {
    renderHud([hudEntry({ outcome: 'partial', errorLine: 'partial: stopped at asset 7' })]);
    const row = container.querySelector<HTMLElement>('[data-outcome="partial"]')!;
    expect(row.textContent).toContain('◐ partial');
    expect(row.textContent).not.toContain('✓');
    expect(row.title).toContain('partial: stopped at asset 7');
  });

  it('hudMark covers every outcome', () => {
    expect(hudMark({ status: 'running' })).toBe('');
    expect(hudMark({ status: 'ok', outcome: 'success' })).toBe(' ✓');
    expect(hudMark({ status: 'ok', outcome: 'failed' })).toBe(' ✗');
    expect(hudMark({ status: 'ok', outcome: 'timeout' })).toBe(' ⏱✗ request timed out');
    expect(hudMark({ status: 'ok', outcome: 'empty' })).toBe(' ○ no data');
    expect(hudMark({ status: 'ok', outcome: 'partial' })).toBe(' ◐ partial');
  });

  it('collapsing the HUD hides the chip list entirely (count badge only); expanding restores it', () => {
    renderHud([hudEntry({}), hudEntry({ id: 'hud-y-q1-2', num: 2 })]);
    expect(container.querySelectorAll('.hud-chip')).toHaveLength(2);

    const toggle = container.querySelector<HTMLButtonElement>('.tool-hud-toggle')!;
    act(() => toggle.click());
    // Chips are NOT rendered at all — no squished slivers possible.
    expect(container.querySelectorAll('.hud-chip')).toHaveLength(0);
    const badge = container.querySelector<HTMLElement>('.tool-hud-count')!;
    expect(badge.textContent).toBe('2');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');

    act(() => toggle.click());
    expect(container.querySelectorAll('.hud-chip')).toHaveLength(2);
    expect(container.querySelector('.tool-hud-count')).toBeNull();
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
  });

  it('the collapsed count badge folds prior-question summary chips in', () => {
    renderHud([
      { id: 'hud-summary-q1', kind: 'summary', question: 1, count: 3 },
      hudEntry({ id: 'hud-z-q2-1', question: 2 }),
    ]);
    act(() => container.querySelector<HTMLButtonElement>('.tool-hud-toggle')!.click());
    expect(container.querySelector('.tool-hud-count')!.textContent).toBe('4');
  });

  it('clicking a chip selects the call (hud source); the chip renders .correlated from the store', () => {
    renderHud([hudEntry({ outcome: 'failed' })]);
    const row = container.querySelector<HTMLElement>('.hud-chip')!;
    act(() => row.click());
    expect(correlationStore.get()).toEqual({ source: 'hud', callKey: 'hud-x-q1-1' });
    expect(container.querySelector('.hud-chip.correlated')).toBeTruthy();
    // Re-click toggles off.
    act(() => container.querySelector<HTMLElement>('.hud-chip')!.click());
    expect(correlationStore.get()).toBeNull();
    expect(container.querySelector('.hud-chip.correlated')).toBeNull();
  });

  const traceEntry = (over: Partial<TraceEntry>): TraceEntry => ({
    id: 1,
    type: 'discovery',
    label: 'Telem Sitewise',
    text: 'body',
    question: 1,
    step: 3,
    ...over,
  });

  const renderTrace = (entries: TraceEntry[]) =>
    act(() => {
      root.render(
        <TraceEntries
          entries={entries}
          questionCounter={1}
          activeQuestionFilter={null}
          onFilterQuestion={() => {}}
          visible
        />,
      );
    });

  it('a timeout step renders the timeout class and the honest body', () => {
    renderTrace([traceEntry({ outcome: 'timeout', text: 'Request timed out after 60s' })]);
    const entry = container.querySelector<HTMLElement>('.trace-entry.timeout')!;
    expect(entry).toBeTruthy();
    expect(entry.dataset.outcome).toBe('timeout');
    expect(entry.querySelector('.trace-body')!.textContent).toContain('Request timed out after 60s');
  });

  it('a partial step renders the partial class', () => {
    renderTrace([traceEntry({ outcome: 'partial', text: '12 records\npartial: stopped at asset 7' })]);
    expect(container.querySelector('.trace-entry.partial')).toBeTruthy();
  });

  it('clicking a call-bearing trace row selects it (trace source) and renders .correlated; keyless rows are inert', () => {
    renderTrace([traceEntry({ callKey: 'hud-x-q1-1' }), traceEntry({ id: 2, step: 4 })]);
    const rows = container.querySelectorAll<HTMLElement>('.trace-entry');
    act(() => rows[0].click());
    expect(correlationStore.get()).toEqual({ source: 'trace', callKey: 'hud-x-q1-1' });
    expect(rows[0].classList.contains('correlated')).toBe(true);
    expect(rows[0].classList.contains('has-call')).toBe(true);
    // Row without a callKey: click does nothing.
    act(() => rows[1].click());
    expect(correlationStore.get()).toEqual({ source: 'trace', callKey: 'hud-x-q1-1' });
    expect(rows[1].classList.contains('has-call')).toBe(false);
  });

  it('a hud-sourced selection highlights the matching trace row (cross-surface)', () => {
    selectCall('hud', 'hud-x-q1-1');
    renderTrace([traceEntry({ callKey: 'hud-x-q1-1' })]);
    expect(container.querySelector('.trace-entry.correlated')).toBeTruthy();
  });
});
