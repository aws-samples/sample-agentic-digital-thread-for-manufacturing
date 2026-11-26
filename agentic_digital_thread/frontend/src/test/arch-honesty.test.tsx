// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — HUD/trace honesty for failed/empty tool calls.
 *
 * Evidence driving this suite: instant 0.1s EMPTY/error tool returns rendered
 * the SAME green ✓ as real successes, and their trace steps were bare headers
 * with no body — users read ✓ as success and concluded "not working".
 *
 *  - classifyToolResult: client-side mirror of the server wasted-call markers
 *    (agents/query_intelligence.py _EMPTY_MARKERS/_ERROR_MARKERS — the
 *    tool_result SSE event carries NO per-call server flag);
 *  - store: completed HUD rows carry outcome/errorLine; failed calls surface
 *    an honest failed trace step; blank summaries get an explicit
 *    'No data returned' body; retry badge on re-calls after a failure;
 *  - ToolHud: ✗ (red, tooltip) / ○ no data (amber) / ✓ rendering;
 *  - TraceEntries: outcome classes, retry badge, hostile-string body is
 *    escaped/inert (no element injection). */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  classifyToolResult,
  errorFirstLine,
  type ArchStore,
  type HudEntry,
  type InvCallDetail,
  type TraceEntry,
} from '../lib/arch-store';
import ToolHud from '../views/architecture/ToolHud';
import TraceEntries from '../views/architecture/TraceEntries';
import { installArchStoreDisposal, trackedArchStore as createArchStore } from './arch-store-tracking';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

installArchStoreDisposal();

function ask(store: ArchStore, question: string): void {
  store.handleArchEvent('user_msg', { question });
}

/* ── classifyToolResult — mirror of the server marker semantics ─────────── */

describe('classifyToolResult (client mirror of query_intelligence markers)', () => {
  it('classifies error envelopes as failed (server _ERROR_MARKERS)', () => {
    expect(classifyToolResult('Error: connection refused')).toBe('failed');
    expect(classifyToolResult('error executing query against erp')).toBe('failed');
    expect(classifyToolResult('Query failed after 3 attempts')).toBe('failed');
    expect(classifyToolResult('Failed to execute statement')).toBe('failed');
    expect(classifyToolResult('Exception: boom')).toBe('failed');
    expect(classifyToolResult('Traceback (most recent call last):')).toBe('failed');
    // Transport/auth failures (isTransientError fold-in — no "error:" colon).
    expect(classifyToolResult('McpException - MCP invocation failed: Authorization error when sending message')).toBe(
      'failed',
    );
  });

  it('classifies blank and no-data envelopes as empty (server _EMPTY_MARKERS)', () => {
    expect(classifyToolResult('')).toBe('empty');
    expect(classifyToolResult('   \n ')).toBe('empty');
    expect(classifyToolResult(undefined)).toBe('empty');
    expect(classifyToolResult('No records found.')).toBe('empty');
    expect(classifyToolResult('No matching nodes found.')).toBe('empty');
    expect(classifyToolResult('The query returned no rows')).toBe('empty');
    expect(classifyToolResult('Empty result set')).toBe('empty');
  });

  it('classifies real content as success without mistaking domain text for an error', () => {
    expect(classifyToolResult('3 records found\n| a | b |')).toBe('success');
    expect(classifyToolResult('10 records found\n| lot | result |\n| L-1 | tolerance exceeded |')).toBe('success');
    expect(classifyToolResult(
      'Based on my investigation of the ERP database, here are my findings. '.repeat(4) +
        'Supplier A had no matching purchase order, but lot L-1 was returned.',
    )).toBe('success');
    expect(classifyToolResult('Bytes scanned limit exceeded')).toBe('failed');
    expect(classifyToolResult('Concept: customer (identity)')).toBe('success');
    expect(classifyToolResult('42')).toBe('success');
  });

  it('classifies filtered and rejected learning writes from raw result status', () => {
    expect(classifyToolResult('Result with 2 fields', 100, { status: 'dropped' }, 'kg___save_correlation')).toBe('filtered');
    expect(classifyToolResult('Result with 2 fields', 100, { status: 'rejected' }, 'kg___save_value_mapping')).toBe('failed');
  });

  it('errorFirstLine returns the trimmed, capped first line', () => {
    expect(errorFirstLine('Error: boom\nstack line 2')).toBe('Error: boom');
    expect(errorFirstLine('x'.repeat(300)).length).toBeLessThanOrEqual(201); // 200 + ellipsis
    // SPLIT (trace de-truncation): trace rows pass Infinity — full first line.
    expect(errorFirstLine('x'.repeat(300), Number.POSITIVE_INFINITY)).toBe('x'.repeat(300));
    expect(errorFirstLine(`${'x'.repeat(300)}\nline 2`, Number.POSITIVE_INFINITY)).toBe('x'.repeat(300));
  });
});

/* ── Store: HUD outcome + honest trace steps + retry heuristic ──────────── */

describe('arch-store: HUD/trace honesty', () => {
  let store: ArchStore;
  beforeEach(() => {
    store = createArchStore();
  });

  const hudTool = (): HudEntry => store.getState().hud.filter((h) => h.kind === 'tool').slice(-1)[0];

  it('an error-marker result marks the HUD row failed with the error first line', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'lake-athena___athena_run_query', input: {} });
    store.handleArchEvent('tool_result', {
      tool: 'lake-athena___athena_run_query',
      summary: 'Error: Athena workgroup not found\ndetails follow',
      input: {},
    });
    const row = hudTool();
    expect(row.status).toBe('ok'); // completed…
    expect(row.outcome).toBe('failed'); // …but honestly failed
    expect(row.errorLine).toBe('Error: Athena workgroup not found');
    // Trace: an explicit failed step with the error line as its body.
    const t = store.getState().trace[1]; // [0] is the question anchor row
    expect(t.outcome).toBe('failed');
    expect(t.text).toBe('Error: Athena workgroup not found');
  });

  it('SPLIT: a 400-char error first line reaches the TRACE + error-node detail in full; HUD errorLine stays 200-capped', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'lake-athena___athena_run_query', input: {} });
    const first = `Error: ${'e'.repeat(400)}`;
    store.handleArchEvent('tool_result', {
      tool: 'lake-athena___athena_run_query',
      summary: `${first}\nstack line 2`,
      input: {},
    });
    const row = hudTool();
    expect(row.outcome).toBe('failed');
    expect(row.errorLine!.length).toBeLessThanOrEqual(201); // 200 + ellipsis char
    expect(row.errorLine!.endsWith('…')).toBe(true);
    const s = store.getState();
    const t = s.trace[1]; // [0] is the question anchor row
    expect(t.outcome).toBe('failed');
    expect(t.text).toBe(first); // all 407 chars — first-LINE split stays, length cap gone
    const errNode = s.nodes.find((n) => n.outcome === 'failed')!;
    expect(errNode.detail).toBe(first); // modal-adjacent detail carries the full line too
  });

  it('a partial result keeps the FULL summary in the trace (pinned); HUD errorLine stays the capped partial note', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'telem-sitewise___sitewise_query', input: {} });
    const note = `partial: stopped at asset 7 — ${'p'.repeat(300)}`;
    const summary = `12 records found\n${note}`;
    store.handleArchEvent('tool_result', { tool: 'telem-sitewise___sitewise_query', summary, input: {} });
    const row = hudTool();
    expect(row.outcome).toBe('partial');
    expect(row.errorLine!.length).toBeLessThanOrEqual(201); // partialNote keeps its 200 cap (D2)
    expect(row.errorLine!.endsWith('…')).toBe(true);
    const t = store.getState().trace.filter((x) => x.outcome === 'partial').slice(-1)[0];
    expect(t.text).toBe(summary); // full summary — unchanged behavior, now pinned
  });

  it('an empty-content result marks the HUD row empty and the trace body says "No data returned"', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'telem-sitewise___sitewise_query', input: {} });
    store.handleArchEvent('tool_result', { tool: 'telem-sitewise___sitewise_query', summary: '', input: {} });
    const row = hudTool();
    expect(row.outcome).toBe('empty');
    expect(row.errorLine).toBeUndefined();
    const t = store.getState().trace[1]; // [0] is the question anchor row
    expect(t.outcome).toBe('empty');
    expect(t.text).toBe('No data returned'); // was a bare header (steps 12/13)
  });

  it('a successful result keeps outcome success and the trace/finding path unchanged', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: { sql: 'SELECT 1' } });
    store.handleArchEvent('tool_result', {
      tool: 'erp___query_erp',
      summary: '3 records found\n| a |',
      input: { sql: 'SELECT 1' },
    });
    const row = hudTool();
    expect(row.outcome).toBe('success');
    const s = store.getState();
    expect(s.nodes.some((n) => n.type === 'finding')).toBe(true);
    expect(s.trace[1].outcome).toBeUndefined();
    expect(s.trace[1].retry).toBeUndefined();
  });

  it('a re-call to the SAME system after a failure carries the retry badge; other systems do not', () => {
    ask(store, 'q');
    // Fail on athena.
    store.handleArchEvent('tool_start', { tool: 'lake-athena___athena_run_query', input: {} });
    store.handleArchEvent('tool_result', {
      tool: 'lake-athena___athena_run_query',
      summary: 'Error: boom',
      input: {},
    });
    // Unrelated system succeeds — NO retry badge.
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: '1 record found', input: {} });
    // Athena re-call succeeds — retry badge.
    store.handleArchEvent('tool_start', { tool: 'lake-athena___athena_run_query', input: {} });
    store.handleArchEvent('tool_result', {
      tool: 'lake-athena___athena_run_query',
      summary: '5 records found',
      input: {},
    });
    const trace = store.getState().trace; // trace[0] is the question anchor row
    expect(trace[1].outcome).toBe('failed');
    expect(trace[1].retry).toBeUndefined(); // first failure is not itself a retry
    expect(trace[2].retry).toBeUndefined(); // different system
    expect(trace[3].retry).toBe(true); // same system, after the failure
    expect(trace[3].outcome).toBeUndefined(); // the retry succeeded
    const athenaRows = store.getState().hud.filter((h) => h.kind === 'tool' && h.tool === 'lake-athena___athena_run_query');
    expect(athenaRows[0].recovered).toBe(true);
    expect(athenaRows[1].outcome).toBe('success');
    expect(trace[1].recovered).toBe(true);
  });

  it('the retry heuristic resets per question', () => {
    ask(store, 'q1');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: 'Error: boom', input: {} });
    ask(store, 'q2');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: '1 record found', input: {} });
    const trace = store.getState().trace;
    expect(trace[3].retry).toBeUndefined(); // new question — clean slate ([2] is q2's question row)
  });
});

/* ── Server-truth timing (stream-realtime wave) ──────────────────────────
 * tool_start may carry `ts` (server epoch ms) and tool_result `elapsed_ms`
 * (paired by toolUseId AT THE EMITTER). When present these are the displayed
 * truth; the browser receipt-clock pairing is only the deploy-skew fallback.
 * Evidence: 1KB-buffered SSE delivered start+result in one burst → the HUD
 * showed 0.1s ✓ for calls that actually took many seconds. */

describe('arch-store: server-truth timing', () => {
  let store: ArchStore;
  beforeEach(() => {
    store = createArchStore();
    vi.useFakeTimers();
    vi.setSystemTime(1_752_600_000_000);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const hudTool = (): HudEntry => store.getState().hud.filter((h) => h.kind === 'tool').slice(-1)[0];
  const sysCalls = (): InvCallDetail[] => {
    const node = store.getState().nodes.find((n) => Array.isArray(n._calls) && (n._calls as unknown[]).length);
    return (node?._calls as InvCallDetail[]) || [];
  };

  it('burst delivery with server elapsed_ms shows the SERVER duration, never the receipt gap', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: { sql: 'SELECT 1' }, ts: 1_752_599_991_000 });
    // Burst: the result arrives 100ms after the start on the RECEIPT clock,
    // but the server-paired duration was 8.4s.
    vi.advanceTimersByTime(100);
    store.handleArchEvent('tool_result', {
      tool: 'erp___query_erp',
      summary: '3 records found\n| a |',
      input: { sql: 'SELECT 1' },
      ts: 1_752_599_999_400,
      elapsed_ms: 8400,
    });
    expect(hudTool().elapsedMs).toBe(8400); // NOT 100 — 0.1s-✓ artifact impossible
    const call = sysCalls()[0];
    expect(call.elapsedMs).toBe(8400);
    expect(call.startedAt).toBe(1_752_599_991_000); // server ts, not receipt time
  });

  it('zero receipt gap (same-tick burst) still shows the server duration', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {}, ts: 1_752_599_990_000 });
    store.handleArchEvent('tool_result', {
      tool: 'erp___query_erp',
      summary: '1 record found',
      input: {},
      elapsed_ms: 12_345,
    });
    expect(hudTool().elapsedMs).toBe(12_345);
  });

  it('without server fields the browser receipt-clock fallback is unchanged (deploy skew)', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: { sql: 'SELECT 1' } });
    const t0 = Date.now();
    vi.advanceTimersByTime(5000);
    store.handleArchEvent('tool_result', {
      tool: 'erp___query_erp',
      summary: '3 records found\n| a |',
      input: { sql: 'SELECT 1' },
    });
    expect(hudTool().elapsedMs).toBe(5000);
    const call = sysCalls()[0];
    expect(call.elapsedMs).toBe(5000);
    expect(call.startedAt).toBe(t0); // browser receipt time
  });

  it('malformed server fields (negative / non-number) fall back to receipt pairing', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {}, ts: 'nope' });
    vi.advanceTimersByTime(700);
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: '1 record found', input: {}, elapsed_ms: -5 });
    expect(hudTool().elapsedMs).toBe(700);
  });
});

/* ── Empty→failed-when-retried (retroactive upgrade) ─────────────────────
 * A completed 'empty' call whose system is RE-CALLED later in the SAME
 * question upgrades to failed (no payload) in place — the retry proves the
 * model judged the empty result unusable. The re-call also earns the
 * existing 'retry' badge (the upgraded call IS a failure). */

describe('arch-store: empty outcome upgrades to failed when the system is retried', () => {
  let store: ArchStore;
  beforeEach(() => {
    store = createArchStore();
  });

  const toolRows = (): HudEntry[] => store.getState().hud.filter((h) => h.kind === 'tool');

  it('empty-then-retried upgrades the HUD row AND the trace entry in place', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: { sql: 'SELECT 1' } });
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: 'No records found.', input: {} });
    expect(toolRows()[0].outcome).toBe('empty');
    expect(store.getState().trace[1].outcome).toBe('empty');
    // The re-call to the SAME system is the retry verdict.
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: { sql: 'SELECT 2' } });
    const upgraded = toolRows()[0];
    expect(upgraded.outcome).toBe('failed');
    expect(upgraded.errorLine).toContain('no payload');
    expect(store.getState().trace[1].outcome).toBe('failed');
    // The retry's own completion carries the existing retry badge.
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: '5 records found', input: {} });
    const trace = store.getState().trace;
    expect(trace[2].retry).toBe(true);
    expect(trace[2].outcome).toBeUndefined(); // the retry itself succeeded
  });

  it('a BLANK-summary empty upgrades too (else-branch trace entry)', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'telem-sitewise___sitewise_query', input: {} });
    store.handleArchEvent('tool_result', { tool: 'telem-sitewise___sitewise_query', summary: '', input: {} });
    expect(toolRows()[0].outcome).toBe('empty');
    store.handleArchEvent('tool_start', { tool: 'telem-sitewise___sitewise_query', input: {} });
    expect(toolRows()[0].outcome).toBe('failed');
    expect(store.getState().trace[1].outcome).toBe('failed');
  });

  it('empty-not-retried STAYS empty (calls to other systems are not a verdict)', () => {
    ask(store, 'q');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: 'No records found.', input: {} });
    // A DIFFERENT system is called afterwards — no upgrade.
    store.handleArchEvent('tool_start', { tool: 'mes___query_mes', input: {} });
    store.handleArchEvent('tool_result', { tool: 'mes___query_mes', summary: '2 records found', input: {} });
    store.handleArchEvent('done', {});
    expect(toolRows()[0].outcome).toBe('empty');
    expect(store.getState().trace[1].outcome).toBe('empty');
  });

  it('the upgrade window is per-question: a NEW question re-calling the system does not rewrite history', () => {
    ask(store, 'q1');
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: 'No records found.', input: {} });
    ask(store, 'q2'); // q1's tool rows collapse into a summary chip; the trace persists
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: {} });
    expect(store.getState().trace[1].outcome).toBe('empty'); // q1's empty stays empty
  });
});

/* ── Component rendering ─────────────────────────────────────────────────── */

describe('ToolHud / TraceEntries rendering', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  const hudEntry = (over: Partial<HudEntry>): HudEntry => ({
    id: 'hud-x-q1-1',
    kind: 'tool',
    question: 1,
    tool: 'lake-athena___athena_run_query',
    label: 'Lake Athena',
    t0: 0,
    num: 4,
    status: 'ok',
    elapsedMs: 100,
    ...over,
  });

  const renderHud = (entries: HudEntry[]) =>
    act(() => {
      root.render(<ToolHud entries={entries} activeQuestionFilter={null} onFilterQuestion={() => {}} />);
    });

  it('a failed completion renders a red ✗ with the error first line as tooltip (no green ✓)', () => {
    renderHud([hudEntry({ outcome: 'failed', errorLine: 'Error: Athena workgroup not found' })]);
    const row = container.querySelector<HTMLElement>('[data-outcome="failed"]')!;
    expect(row).toBeTruthy();
    expect(row.textContent).toContain('✗');
    expect(row.textContent).not.toContain('✓');
    expect(row.title).toBe('Error: Athena workgroup not found');
  });

  it('an empty completion renders the hollow ○ "no data" (no ✓, no ✗)', () => {
    renderHud([hudEntry({ outcome: 'empty' })]);
    const row = container.querySelector<HTMLElement>('[data-outcome="empty"]')!;
    expect(row).toBeTruthy();
    expect(row.textContent).toContain('○ no data');
    expect(row.textContent).not.toContain('✓');
    expect(row.textContent).not.toContain('✗');
  });

  it('a failed call followed by a successful retry renders the final recovered outcome', () => {
    renderHud([hudEntry({ outcome: 'failed', recovered: true, errorLine: 'Error: first attempt failed' })]);
    const row = container.querySelector<HTMLElement>('[data-outcome="recovered"]')!;
    expect(row.textContent).toContain('✗ → retried ✓');
    expect(row.title).toContain('a retry succeeded');
  });

  it('a successful completion keeps the ✓ (unchanged)', () => {
    renderHud([hudEntry({ outcome: 'success', elapsedMs: 3900 })]);
    const row = container.querySelector<HTMLElement>('[data-outcome="success"]')!;
    expect(row.textContent).toContain('3.9s ✓');
  });

  it('a legacy completed row without outcome still renders ✓ (deploy-skew safe)', () => {
    renderHud([hudEntry({})]);
    const row = container.querySelector<HTMLElement>('[data-outcome="success"]')!;
    expect(row.textContent).toContain('✓');
  });

  const traceEntry = (over: Partial<TraceEntry>): TraceEntry => ({
    id: 1,
    type: 'discovery',
    label: 'Lake Athena',
    text: 'body',
    question: 1,
    step: 12,
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

  it('a failed step gets the failed class and renders the error line as an ESCAPED body (hostile string)', () => {
    const hostile = 'Error: <img src=x onerror="window.__pwned=1"> & <script>alert(1)</script> boom';
    renderTrace([traceEntry({ outcome: 'failed', text: hostile })]);
    const entry = container.querySelector<HTMLElement>('.trace-entry.failed')!;
    expect(entry).toBeTruthy();
    expect(entry.dataset.outcome).toBe('failed');
    // No element injection: react-markdown renders raw HTML nodes inert.
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();
    // The surrounding error text still renders as text.
    const body = entry.querySelector('.trace-body')!;
    expect(body.textContent).toContain('Error:');
    expect(body.textContent).toContain('boom');
  });

  it('an empty step gets the empty class and an explicit "No data returned" body', () => {
    renderTrace([traceEntry({ outcome: 'empty', text: 'No data returned' })]);
    const entry = container.querySelector<HTMLElement>('.trace-entry.empty')!;
    expect(entry).toBeTruthy();
    expect(entry.querySelector('.trace-body')!.textContent).toContain('No data returned');
  });

  it('a retry step renders the subtle retry badge; non-retry steps do not', () => {
    renderTrace([traceEntry({ id: 1, retry: true }), traceEntry({ id: 2, step: 13 })]);
    const entries = container.querySelectorAll('.trace-entry');
    expect(entries[0].querySelector('.trace-retry-badge')?.textContent).toBe('retry');
    expect(entries[1].querySelector('.trace-retry-badge')).toBeNull();
  });

  it('a recovered failed step says that its later retry succeeded', () => {
    renderTrace([traceEntry({ outcome: 'failed', recovered: true })]);
    expect(container.querySelector('.trace-retry-badge')?.textContent).toBe('retried ✓');
  });
});
