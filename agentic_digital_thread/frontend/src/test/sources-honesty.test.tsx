// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Sources honesty (test event 32, 2026-10-15).
 *
 * 1. Sources listed failed calls as if they had answered. Chips are MARKED,
 *    never dropped (dropping one would renumber the inline citations, which
 *    are correct): a call whose own trace outcome is failed or timeout gets a
 *    plain "no answer" suffix, an empty one "no data", plus `data-outcome`.
 * 2. A follow-up answered from an earlier turn's results showed no Sources
 *    and left copied citation numbers as literal text. The runtime now labels
 *    earlier results [E<k>] and sends `intelligence_summary.earlier_sources`
 *    (names only); the footer shows a chip for each label the answer cites,
 *    and the inline [E<k>] becomes a badge.
 *
 * Server halves: tests/unit/test_sources_honesty.py. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ChatThread } from '../chat/ChatThread';
import { injectCitationLinks } from '../chat/citations';
import { citationOutcome, citedEarlierCitations } from '../chat/provenance';
import { createThreadStore, type ThreadStore } from '../chat/thread-store';
import type { Citation } from '../chat/turn-model';
import { setLang } from '../lib/i18n';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let store: ThreadStore;

beforeEach(() => {
  const ls = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => ls.get(k) ?? null,
    setItem: (k: string, v: string) => void ls.set(k, String(v)),
    removeItem: (k: string) => void ls.delete(k),
  });
  setLang('en');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  store = createThreadStore();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

const render = () => act(() => root.render(<ChatThread store={store} />));
const chips = () => [...container.querySelectorAll<HTMLAnchorElement>('a.source-chip')];

function call(tool: string, id: string, summary: string, elapsedMs = 1_000) {
  store.applyEvent('tool_start', { tool, tool_id: id, input: { question: 'q' } });
  store.applyEvent('tool_result', { tool, tool_id: id, input: 'x', summary, elapsed_ms: elapsedMs });
  store.applyEvent('tool_end', { tool, tool_id: id });
}

describe('citationOutcome (pure)', () => {
  const pills = [
    { tool: 'explore_graph', toolId: 'g' },
    { tool: 'erp___query_erp', toolId: 'a' },
    { tool: 'erp___query_erp', toolId: 'b' },
    { tool: 'mes___query_mes', toolId: 'c' },
    { tool: 'mes___query_mes', toolId: 'd', ambiguousWireId: true },
    { tool: 'mes___query_mes', toolId: 'e' },
  ];
  const traces = new Map([
    ['a', { outcome: 'failed' as const }],
    ['b', { outcome: 'timeout' as const }],
    ['c', { outcome: 'empty' as const }],
    ['d', { outcome: 'failed' as const }],
    ['e', { outcome: 'success' as const }],
  ]);

  it('returns the cited call own failed / timeout / empty outcome, else null', () => {
    expect([1, 2, 3, 4, 5, 6].map((n) => citationOutcome(pills, traces, n))).toEqual([
      'failed',
      'timeout',
      'empty',
      null, // an ambiguous wire id never binds (fail closed)
      null, // success is plain
      null, // no such citation
    ]);
  });
});

describe('failed calls are marked in Sources, not dropped', () => {
  it('three failed calls and one success: three "no answer" chips, the success plain', () => {
    act(() => {
      store.startTurn('current values?');
      call('explore_graph', 'g1', 'Found 2 systems.');
      call('hub___query_hub', 't1', 'Error: service unavailable');
      call('hub___query_hub', 't2', 'Error: service unavailable');
      call('hub___query_hub', 't3', 'Error: service unavailable');
      call('telemetry___query_telemetry', 't4', '3 records found\n| a |\n|---|\n| 1 |');
      store.applyEvent('token', { text: 'The hub failed [1][2][3]; the latest recorded values [4].' });
      store.applyEvent('done', { status: 'complete' });
    });
    render();
    const all = chips();
    expect(all.map((c) => c.dataset.citeNum)).toEqual(['1', '2', '3', '4']);
    for (const chip of all.slice(0, 3)) {
      expect(chip.textContent).toMatch(/^\[\d\] .+ · no answer$/);
      expect(chip.dataset.outcome).toBe('failed');
    }
    expect(all[3].textContent).not.toContain('·');
    expect(all[3].dataset.outcome).toBeUndefined();
  });

  it('a timed-out call says "no answer", an empty one "no data"', () => {
    act(() => {
      store.startTurn('q');
      call('erp___query_erp', 't1', 'Error: gateway gave up', 61_000);
      call('mes___query_mes', 't2', 'No records found.');
      store.applyEvent('token', { text: 'Nothing [1][2].' });
      store.applyEvent('done', { status: 'complete' });
    });
    render();
    const [timedOut, empty] = chips();
    expect(timedOut.textContent).toMatch(/ · no answer$/);
    expect(timedOut.dataset.outcome).toBe('timeout');
    expect(empty.textContent).toMatch(/ · no data$/);
    expect(empty.dataset.outcome).toBe('empty');
  });

  it("a system agent's prose answer carrying the engine no-data line is marked", () => {
    act(() => {
      store.startTurn('q');
      call(
        'hub___query_hub',
        't1',
        'The source returned a service error on each attempt.\n\n' +
          '[engine] No query this call ran returned data (transport_error).',
      );
      store.applyEvent('token', { text: 'It failed [1].' });
      store.applyEvent('done', { status: 'complete' });
    });
    render();
    expect(chips()[0].dataset.outcome).toBe('failed');
    expect(chips()[0].textContent).toMatch(/ · no answer$/);
  });
});

describe('results reused from an earlier question', () => {
  const earlierTurn = (text: string, earlier: unknown) =>
    act(() => {
      store.startTurn('which of them are still open?');
      call('explore_graph', 'g1', 'Found 1 system.');
      store.applyEvent('token', { text });
      store.applyEvent('metadata', {
        intelligence_summary: { total_tool_calls: 1, earlier_sources: earlier },
      });
      store.applyEvent('done', { status: 'complete' });
    });

  it('zero cited calls + earlier_sources + an answer citing [E1]: one earlier chip and a badge', () => {
    earlierTurn('Two are still open [E1].', [
      { label: 'E1', tool: 'erp___query_erp' },
      { label: 'E2', tool: 'mes___query_mes' },
    ]);
    render();
    const all = chips();
    expect(all).toHaveLength(1);
    expect(all[0].textContent).toBe('[E1] ERP · earlier in this chat');
    expect(all[0].dataset.citeTarget).toBe('system-erp___query_erp');
    expect(all[0].dataset.citeNum).toBeUndefined();
    const badge = container.querySelector<HTMLAnchorElement>('.answer-body a.cite-badge')!;
    expect(badge.textContent).toBe('E1');
    expect(badge.dataset.citeTarget).toBe('system-erp___query_erp');
    expect(container.querySelector('.answer-body')!.textContent).not.toContain('[E1]');
  });

  it('this turn keeps its own numbered chips next to the earlier one', () => {
    act(() => {
      store.startTurn('q');
      call('mes___query_mes', 't1', '3 records found\n| a |\n|---|\n| 1 |');
      store.applyEvent('token', { text: 'Open [E1], checked again [1].' });
      store.applyEvent('metadata', {
        intelligence_summary: { earlier_sources: [{ label: 'E1', tool: 'erp___query_erp' }] },
      });
      store.applyEvent('done', { status: 'complete' });
    });
    render();
    expect(chips().map((c) => c.textContent)).toEqual([
      '[1] MES',
      '[E1] ERP · earlier in this chat',
    ]);
  });

  it('ignores malformed earlier entries and uncited labels', () => {
    earlierTurn('Nothing cited here [E9] [E1].', [
      { label: 'E1', tool: 'erp___query_erp' },
      { label: 'E100', tool: 'erp___query_erp' },
      { label: 'X2', tool: 'erp___query_erp' },
      { label: 'E3', tool: 7 },
      'E4',
    ]);
    render();
    expect(chips().map((c) => c.textContent)).toEqual(['[E1] ERP · earlier in this chat']);
    // [E9] has no entry: stays literal text, never a badge.
    expect(container.querySelector('.answer-body')!.textContent).toContain('[E9]');
  });
});

describe('injectCitationLinks with earlier labels', () => {
  const current: Citation = { tool: 'mes___query_mes', label: 'MES', nodeId: 'system-mes___query_mes', num: 1, findingNodeId: null };
  const earlier: Citation = { ...current, tool: 'erp___query_erp', label: 'ERP', nodeId: 'system-erp___query_erp', earlier: true };

  it('links [E1] to the earlier citation and [1] to this turn, never crossing them', () => {
    const out = injectCitationLinks('A [1], B [E1], C [E2].', [current, earlier]);
    expect(out).toBe(
      'A [1](<#cite-system-mes___query_mes> "MES"), B [E1](<#cite-system-erp___query_erp> "ERP"), C [E2].',
    );
    expect(injectCitationLinks('Only [1].', [earlier])).toBe('Only [1].');
    expect(injectCitationLinks('Code `[E1]` stays.', [earlier])).toBe('Code `[E1]` stays.');
  });

  it('citedEarlierCitations returns only the labels the answer cites, in label order', () => {
    const e2: Citation = { ...earlier, num: 2, tool: 'mes___query_mes' };
    expect(citedEarlierCitations([current, e2, earlier], 'X [E2] and [E1] [E1].')).toEqual([earlier, e2]);
    expect(citedEarlierCitations([current, earlier], 'no labels [1]')).toEqual([]);
  });
});

/* A "no data" chip and its evidence card agree. The call reported no data,
 * while its provenance record counts the rows its captured evidence holds
 * (in the cases seen, another query the same call ran). The card states both
 * facts instead of a bare row count that reads as a contradiction of the
 * chip, and claims nothing about why they differ. */
describe('evidence card of a "no data" source', () => {
  const record = (rows: number) => ({
    citation_num: 1,
    tool_id: 't1',
    source_system: 'ledger',
    captured_at: '2026-09-02T08:15:00Z',
    verification: 'partial',
    entity: { table: 'consignments', columns: ['consignment_ref'] },
    result_shape: { rows, truncated: false, partial_reason: '' },
  });

  const openCard = (summary: string, rows: number) => {
    act(() => {
      store.startTurn('where is consignment C-77?');
      call('ledger___query_ledger', 't1', summary);
      store.applyEvent('token', { text: 'Not found [1].' });
      store.applyEvent('metadata', { provenance: { records: [record(rows)] } });
      store.applyEvent('done', { status: 'complete' });
    });
    act(() => store.openEvidence(1));
    render();
    return container.querySelector<HTMLElement>('.evidence-card')!;
  };

  it('says the source reported no data and its captured evidence holds the counted rows', () => {
    const card = openCard('No record of C-77 in this system: the lookup returned no rows.', 50);
    expect(chips()[0].textContent).toMatch(/ · no data$/);
    expect(card.getAttribute('aria-label')).toBe('Evidence for citation 1');
    expect(card.querySelector('.evidence-shape')?.textContent).toBe(
      'This source reported no data, but its captured evidence holds 50 rows.',
    );
    // The entity and the query time stay.
    expect(card.querySelector('.evidence-entity')?.textContent).toBe('Entity: consignments (consignment_ref)');
    const scope = [...card.querySelectorAll('.evidence-scope li')].map((li) => li.textContent);
    expect(scope).toContain('queried at 2026-09-02T08:15:00Z');
  });

  it('keeps the plain row count for a call that answered', () => {
    const card = openCard('3 records found\n| a |\n|---|\n| 1 |', 3);
    expect(chips()[0].dataset.outcome).toBeUndefined();
    expect(card.querySelector('.evidence-shape')?.textContent).toBe('3 rows returned');
  });
});
