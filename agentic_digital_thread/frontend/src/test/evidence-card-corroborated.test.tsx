// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Wave-5 source-side capture rendering in the Tier-3 expander
 * (<EvidenceCard/> — F2 additive fields + the F3 corroborated display
 * state). Pins:
 * - a redemption carrying the F2 fields renders entity table/columns,
 *   "rows: N", statement count, AND the corroborated badge as TEXT NODES;
 * - LEGACY response (no additive keys) ⇒ the pre-Wave-5 rendering exactly:
 *   no source-capture node, no corroborated badge;
 * - 404 ⇒ the honest "Technical detail not available" line unchanged;
 * - system_id MISMATCH (or non-'system-agent' origin) ⇒ NO corroborated
 *   state — the F2 lines may still render, the badge may not;
 * - row_count: null renders the honest "row count not captured";
 * - an API-read entry (no query text, the store's API-read note) shows the
 *   note and "1 read" / "N reads", never a row count or "statements
 *   executed"; a one-statement SQL entry still shows no count line;
 * - an unverified record (a turn the integrity gate rejected stays one
 *   whatever was captured) is never shown as corroborated;
 * - hostile entity strings render as LITERAL text (zero-innerHTML). */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { API_READ_NOTE } from '../api/provenance';
import { EvidenceCard } from '../chat/EvidenceCard';
import type { ProvenanceRecord } from '../chat/provenance';
import type { Citation } from '../chat/turn-model';
import { jsonResponse } from './fetch-stub';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let fetchFn: ReturnType<typeof vi.fn>;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  window.currentSessionId = 'session-test-w5';
  fetchFn = vi.fn();
  vi.stubGlobal('fetch', fetchFn);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  delete window.currentSessionId;
  vi.restoreAllMocks();
});

function render(el: React.ReactElement) {
  act(() => {
    root.render(el);
  });
}

function mkCitation(): Citation {
  return {
    tool: 'erp___query_erp',
    label: 'ERP',
    nodeId: 'sys-erp',
    num: 1,
    findingNodeId: null,
  };
}

function mkRecord(over: Partial<ProvenanceRecord> = {}): ProvenanceRecord {
  return {
    citation_num: 1,
    tool_id: 'toolu_w5',
    source_system: 'erp',
    captured_at: '2026-08-04T17:00:00Z',
    verification: 'partial',
    query_ref: 'q-0123456789ab',
    ...over,
  };
}

/** The seven frozen D5 keys — the LEGACY response shape (no F2 keys). */
const legacyDetail = (over: Record<string, unknown> = {}) => ({
  query_ref: 'q-0123456789ab',
  session_id: 'session-test-w5',
  system_id: 'erp',
  tool_name: 'query_erp',
  executed_query: 'SELECT part_no FROM orders',
  kg_artifacts: [],
  captured_at: '2026-08-04T17:00:00Z',
  ...over,
});

const f2Detail = (over: Record<string, unknown> = {}) =>
  legacyDetail({
    entity: { table: 'orders', columns: ['part_no', 'qty'] },
    row_count: 7,
    statement_count: 2,
    origin: 'system-agent',
    ...over,
  });

async function openTier3() {
  const details = container.querySelector<HTMLDetailsElement>('details.evidence-tier3')!;
  expect(details).not.toBeNull();
  await act(async () => {
    details.open = true;
    details.dispatchEvent(new Event('toggle', { bubbles: true }));
  });
}

describe('<EvidenceCard/> Wave-5 source-side capture (F2 + corroborated)', () => {
  it('renders entity/rows/statements + corroborated badge from an F2 redemption', async () => {
    fetchFn.mockResolvedValue(jsonResponse(200, f2Detail()));
    render(<EvidenceCard record={mkRecord()} citation={mkCitation()} onClose={() => {}} />);
    await openTier3();

    const badge = container.querySelector('.evidence-corroborated');
    expect(badge).not.toBeNull();
    expect(badge!.textContent).toBe('corroborated by source-side capture');

    const lines = [...container.querySelectorAll('.evidence-source-capture-line')].map(
      (n) => n.textContent,
    );
    expect(lines).toEqual(['orders (part_no, qty)', 'rows: 7', '2 statements executed']);
    // SQL still renders (Tier-3-only, inside the expander)
    expect(container.querySelector('pre.evidence-executed-query')!.textContent).toBe(
      'SELECT part_no FROM orders',
    );
  });

  it('LEGACY response (no additive keys) renders the pre-Wave-5 expander exactly', async () => {
    fetchFn.mockResolvedValue(jsonResponse(200, legacyDetail()));
    render(<EvidenceCard record={mkRecord()} citation={mkCitation()} onClose={() => {}} />);
    await openTier3();

    expect(container.querySelector('.evidence-source-capture')).toBeNull();
    expect(container.querySelector('.evidence-corroborated')).toBeNull();
    expect(container.querySelector('.evidence-source-capture-line')).toBeNull();
    expect(container.querySelector('pre.evidence-executed-query')!.textContent).toBe(
      'SELECT part_no FROM orders',
    );
  });

  it('404 renders the honest unavailable line unchanged', async () => {
    fetchFn.mockResolvedValue(jsonResponse(404, { detail: 'Unknown or expired query_ref' }));
    render(<EvidenceCard record={mkRecord()} citation={mkCitation()} onClose={() => {}} />);
    await openTier3();

    expect(container.querySelector('.evidence-tier3-unavailable')!.textContent).toBe(
      'Technical detail not available for this claim',
    );
    expect(container.querySelector('.evidence-corroborated')).toBeNull();
    expect(container.querySelector('.evidence-source-capture')).toBeNull();
  });

  it('system_id MISMATCH renders the F2 lines but NO corroborated badge', async () => {
    fetchFn.mockResolvedValue(jsonResponse(200, f2Detail({ system_id: 'mes' })));
    render(<EvidenceCard record={mkRecord()} citation={mkCitation()} onClose={() => {}} />);
    await openTier3();

    expect(container.querySelector('.evidence-corroborated')).toBeNull();
    expect(container.querySelector('.evidence-source-capture-line')).not.toBeNull();
  });

  it('non-system-agent origin never corroborates', async () => {
    fetchFn.mockResolvedValue(jsonResponse(200, f2Detail({ origin: 'orchestrator' })));
    render(<EvidenceCard record={mkRecord()} citation={mkCitation()} onClose={() => {}} />);
    await openTier3();
    expect(container.querySelector('.evidence-corroborated')).toBeNull();
  });

  it('row_count null renders the honest "row count not captured" line', async () => {
    fetchFn.mockResolvedValue(jsonResponse(200, f2Detail({ row_count: null })));
    render(<EvidenceCard record={mkRecord()} citation={mkCitation()} onClose={() => {}} />);
    await openTier3();

    const lines = [...container.querySelectorAll('.evidence-source-capture-line')].map(
      (n) => n.textContent,
    );
    expect(lines).toContain('row count not captured');
  });

  it("an API-read entry shows the store's note and its read count, never rows or statements", async () => {
    // A call that ran no SQL and read its source through the source's API:
    // no query text to show, so the note says why; points are not rows.
    fetchFn.mockResolvedValue(
      jsonResponse(
        200,
        legacyDetail({
          executed_query: '',
          note: API_READ_NOTE,
          entity: { table: '', columns: [] },
          statement_count: 3,
          origin: 'system-agent',
        }),
      ),
    );
    render(<EvidenceCard record={mkRecord()} citation={mkCitation()} onClose={() => {}} />);
    await openTier3();

    expect(container.querySelector('.evidence-query-absent')!.textContent).toBe(API_READ_NOTE);
    expect(container.querySelector('.evidence-corroborated')).not.toBeNull();
    const lines = [...container.querySelectorAll('.evidence-source-capture-line')].map(
      (n) => n.textContent,
    );
    expect(lines).toEqual(['3 reads']);
    expect(container.textContent).not.toContain('statements executed');
    expect(container.textContent).not.toContain('executed query not captured');
  });

  it('an over-bound SQL entry still counts its statements', async () => {
    fetchFn.mockResolvedValue(
      jsonResponse(200, f2Detail({ executed_query: '', note: 'query text exceeded bound' })),
    );
    render(<EvidenceCard record={mkRecord()} citation={mkCitation()} onClose={() => {}} />);
    await openTier3();

    const lines = [...container.querySelectorAll('.evidence-source-capture-line')].map(
      (n) => n.textContent,
    );
    expect(lines).toEqual(['orders (part_no, qty)', 'rows: 7', '2 statements executed']);
  });

  it("a one-read API entry says '1 read'", async () => {
    // The motivating case: one history call that returned 72 points.
    fetchFn.mockResolvedValue(
      jsonResponse(
        200,
        legacyDetail({
          executed_query: '',
          note: API_READ_NOTE,
          entity: { table: '', columns: [] },
          statement_count: 1,
          origin: 'system-agent',
        }),
      ),
    );
    render(<EvidenceCard record={mkRecord()} citation={mkCitation()} onClose={() => {}} />);
    await openTier3();

    const lines = [...container.querySelectorAll('.evidence-source-capture-line')].map(
      (n) => n.textContent,
    );
    expect(lines).toEqual(['1 read']);
  });

  it('a one-statement SQL entry keeps its wording: no count line', async () => {
    fetchFn.mockResolvedValue(jsonResponse(200, f2Detail({ statement_count: 1 })));
    render(<EvidenceCard record={mkRecord()} citation={mkCitation()} onClose={() => {}} />);
    await openTier3();

    const lines = [...container.querySelectorAll('.evidence-source-capture-line')].map(
      (n) => n.textContent,
    );
    expect(lines).toEqual(['orders (part_no, qty)', 'rows: 7']);
  });

  it('an unverified record is never shown as corroborated', async () => {
    // A record the integrity gate rejected stays unverified whatever entry
    // exists (round 3: the frame no longer marks rejection per record, so the
    // card goes by the record's own verification). Its banner says nothing
    // was captured, so Tier 3 must not call the capture a corroboration.
    fetchFn.mockResolvedValue(jsonResponse(200, f2Detail()));
    render(
      <EvidenceCard
        record={mkRecord({ verification: 'unverified' })}
        citation={mkCitation()}
        onClose={() => {}}
      />,
    );
    await openTier3();

    expect(container.querySelector('.evidence-unverified-banner')).not.toBeNull();
    expect(container.querySelector('.evidence-corroborated')).toBeNull();
    // The capture facts themselves still render.
    expect(container.querySelector('.evidence-source-capture-line')).not.toBeNull();
  });

  it('hostile entity strings render as LITERAL text (zero-innerHTML)', async () => {
    const hostile = '<img src=x onerror=alert(1)>orders';
    fetchFn.mockResolvedValue(
      jsonResponse(200, f2Detail({ entity: { table: hostile, columns: ['<b>c</b>'] } })),
    );
    render(<EvidenceCard record={mkRecord()} citation={mkCitation()} onClose={() => {}} />);
    await openTier3();

    expect(container.querySelector('.evidence-source-capture img')).toBeNull();
    expect(container.querySelector('.evidence-source-capture b')).toBeNull();
    const line = container.querySelector('.evidence-source-capture-line')!;
    expect(line.textContent).toBe(`${hostile} (<b>c</b>)`);
  });
});
