// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests for <EvidenceCard/> — the Tier-2
 * plain-language evidence card. Pins:
 * - full-record rendering (entity, scope sentences, result-shape honesty,
 *   KG route rules + value-mapping sentences, badge, no banner);
 * - D4 honest absence — null record opens the card with the FALLBACK label
 *   VERBATIM and the explicit "No captured evidence for this claim" banner;
 *   unknown/hostile verification values normalize to the same banner;
 * - untrusted-data inertness — hostile <img onerror>/<script>/SELECT strings
 *   in ANY record field render as LITERAL TEXT (React text nodes, zero
 *   innerHTML), never markup;
 * - NO SQL / no query_ref in Tier 2 — `query_ref` never appears in the card
 *   text even when present on the record;
 * - the Tier-3 "Technical detail" expander (Task 3.5, contract D5) —
 *   fetch-on-FIRST-open only (never before expand, never twice), fetched
 *   `executed_query` renders as LITERAL TEXT in a <pre> (hostile <script>
 *   strings inert), KG artifact ids as text chips, honest
 *   "Technical detail not available for this claim" line on 403/404/network
 *   failure/absent query_ref (never an empty spinner), over-bound `note`
 *   passthrough, and the caller-supplied `technicalDetail` override still
 *   wins over the built-in expander;
 * - onClose wiring. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { EvidenceCard } from '../chat/EvidenceCard';
import type { ProvenanceRecord } from '../chat/provenance';
import type { Citation } from '../chat/turn-model';
import { jsonResponse } from './fetch-stub';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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
  vi.restoreAllMocks();
});

function render(el: React.ReactElement) {
  act(() => {
    root.render(el);
  });
}

function mkCitation(over: Partial<Citation> = {}): Citation {
  return {
    tool: 'erp___query_erp',
    label: 'ERP',
    nodeId: 'sys-erp',
    num: 3,
    findingNodeId: null,
    ...over,
  };
}

function mkRecord(over: Partial<ProvenanceRecord> = {}): ProvenanceRecord {
  return {
    citation_num: 3,
    tool_id: 'toolu_abc123',
    source_system: 'erp',
    captured_at: '2026-08-03T19:00:00Z',
    verification: 'partial',
    ...over,
  };
}

const FULL_RECORD: ProvenanceRecord = mkRecord({
  tool_name: 'erp___query_erp',
  entity: { table: 'work_orders', columns: ['order_id', 'status'] },
  scope: {
    filters: ['status is one of open, in_progress'],
    window: { start: '2026-07-01T00:00:00Z', end: '2026-07-31T00:00:00Z' },
  },
  query_ref: 'q-3f9c2b81ab04',
  result_shape: { rows: 42, truncated: true, partial_reason: 'row_limit' },
  kg_route: {
    rules_applied: [{ id: 'subj-1', label: 'work order ids map to maintenance refs' }],
    value_mappings: ['equipment_id'],
  },
});

describe('<EvidenceCard/> full record', () => {
  it('renders entity, scope, shape, KG route, and badge from a full record', () => {
    render(<EvidenceCard record={FULL_RECORD} citation={mkCitation()} onClose={() => {}} />);
    const text = container.textContent ?? '';
    // Header: citation num + enriched chip label (system · table · window).
    expect(text).toContain('[3] ERP · work_orders · 2026-07-01 to 2026-07-31');
    // Entity line with columns.
    expect(text).toContain('Entity: work_orders (order_id, status)');
    // Plain-language scope sentences (T2.1 helpers), incl. "queried at".
    const scopeItems = [...container.querySelectorAll('.evidence-scope li')].map(
      (li) => li.textContent,
    );
    expect(scopeItems).toEqual([
      'rows where status is one of open, in_progress',
      'data observed 2026-07-01 to 2026-07-31',
      'queried at 2026-08-03T19:00:00Z',
    ]);
    // Result-shape honesty line (row_limit vocabulary).
    expect(container.querySelector('.evidence-shape')?.textContent).toBe(
      'first 42 rows of a larger result — result was capped at the row limit',
    );
    // KG route one-liners: rule label + value-mapping sentence.
    const kgItems = [...container.querySelectorAll('.evidence-kg-route li')].map(
      (li) => li.textContent,
    );
    expect(kgItems).toEqual([
      'work order ids map to maintenance refs',
      'translated equipment_id between systems',
    ]);
    // 'partial' badge, NO unverified banner.
    expect(container.querySelector('.evidence-badge')?.textContent).toBe('partial');
    expect(container.querySelector('.evidence-unverified-banner')).toBeNull();
  });

  it("renders a 'verified' badge without the banner", () => {
    render(
      <EvidenceCard
        record={mkRecord({ verification: 'verified' })}
        citation={mkCitation()}
        onClose={() => {}}
      />,
    );
    expect(container.querySelector('.evidence-badge')?.textContent).toBe('verified');
    expect(container.querySelector('.evidence-unverified-banner')).toBeNull();
  });

  it('omits entity/scope/shape/KG sections for a sparse record (honest absence)', () => {
    render(<EvidenceCard record={mkRecord()} citation={mkCitation()} onClose={() => {}} />);
    expect(container.querySelector('.evidence-entity')).toBeNull();
    expect(container.querySelector('.evidence-shape')).toBeNull();
    expect(container.querySelector('.evidence-kg-route')).toBeNull();
    // captured_at alone still yields the "queried at" scope line — honest.
    const scopeItems = [...container.querySelectorAll('.evidence-scope li')].map(
      (li) => li.textContent,
    );
    expect(scopeItems).toEqual(['queried at 2026-08-03T19:00:00Z']);
  });
});

describe('<EvidenceCard/> unverified banner (D4 honest absence)', () => {
  it('null record: card still opens with the fallback label VERBATIM + banner', () => {
    render(<EvidenceCard record={null} citation={mkCitation({ label: 'ERP' })} onClose={() => {}} />);
    const card = container.querySelector('.evidence-card');
    expect(card).not.toBeNull();
    expect(card!.textContent).toContain('[3] ERP');
    expect(container.querySelector('.evidence-badge')?.textContent).toBe('unverified');
    expect(container.querySelector('.evidence-unverified-banner')?.textContent).toBe(
      'No captured evidence for this claim',
    );
    // No fabricated sections for the missing record.
    expect(container.querySelector('.evidence-entity')).toBeNull();
    expect(container.querySelector('.evidence-scope')).toBeNull();
    expect(container.querySelector('.evidence-shape')).toBeNull();
    expect(container.querySelector('.evidence-kg-route')).toBeNull();
  });

  it.each(['rejected', 'VERIFIED', '<script>alert(1)</script>', ''])(
    'wire verification %j normalizes to the unverified banner',
    (verification) => {
      render(
        <EvidenceCard
          record={mkRecord({ verification })}
          citation={mkCitation()}
          onClose={() => {}}
        />,
      );
      expect(container.querySelector('.evidence-badge')?.textContent).toBe('unverified');
      expect(container.querySelector('.evidence-unverified-banner')?.textContent).toBe(
        'No captured evidence for this claim',
      );
    },
  );
});

describe('<EvidenceCard/> hostile-string inertness (untrusted data)', () => {
  const HOSTILE_IMG = '<img src=x onerror=alert(1)>';

  it('hostile markup in record fields renders as LITERAL TEXT, never elements', () => {
    const hostile = mkRecord({
      source_system: HOSTILE_IMG,
      entity: { table: '<script>alert(2)</script>', columns: ['<b>bold</b>'] },
      scope: { filters: [`field is ${HOSTILE_IMG}`] },
      kg_route: {
        rules_applied: [{ id: 'subj-x', label: `rule ${HOSTILE_IMG}` }],
        value_mappings: ['<iframe src=evil>'],
      },
    });
    render(<EvidenceCard record={hostile} citation={mkCitation()} onClose={() => {}} />);
    // No injected elements anywhere.
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('iframe')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
    // The hostile strings ARE visible — as literal text nodes.
    const text = container.textContent ?? '';
    expect(text).toContain(HOSTILE_IMG);
    expect(text).toContain('<script>alert(2)</script>');
    expect(text).toContain(`rows where field is ${HOSTILE_IMG}`);
    expect(text).toContain(`rule ${HOSTILE_IMG}`);
    expect(text).toContain('translated <iframe src=evil> between systems');
  });

  it('hostile markup in the fallback citation label renders inert too', () => {
    render(
      <EvidenceCard
        record={null}
        citation={mkCitation({ label: HOSTILE_IMG })}
        onClose={() => {}}
      />,
    );
    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toContain(`[3] ${HOSTILE_IMG}`);
  });

  it('a SELECT smuggled into scope.filters renders as text — and query_ref NEVER appears (no SQL in Tier 2)', () => {
    const smuggled = mkRecord({
      query_ref: 'q-deadbeef1234',
      scope: { filters: ["SELECT * FROM users WHERE name = '<script>x</script>'"] },
    });
    render(<EvidenceCard record={smuggled} citation={mkCitation()} onClose={() => {}} />);
    // The hostile filter is plain text inside the scope list, not markup.
    expect(container.querySelector('script')).toBeNull();
    expect(container.textContent).toContain(
      "rows where SELECT * FROM users WHERE name = '<script>x</script>'",
    );
    // The Tier-3 redemption handle is never user-facing in Tier 2.
    expect(container.textContent).not.toContain('q-deadbeef1234');
  });

  it('query_ref from a full record never reaches the card text or attributes', () => {
    render(<EvidenceCard record={FULL_RECORD} citation={mkCitation()} onClose={() => {}} />);
    expect(container.textContent).not.toContain('q-3f9c2b81ab04');
    expect(container.innerHTML).not.toContain('q-3f9c2b81ab04');
  });
});

describe('<EvidenceCard/> interactions and Wave-3 slot', () => {
  it('the close button fires onClose', () => {
    const onClose = vi.fn();
    render(<EvidenceCard record={FULL_RECORD} citation={mkCitation()} onClose={onClose} />);
    const btn = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Close evidence card"]',
    );
    expect(btn).not.toBeNull();
    act(() => btn!.click());
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('a supplied technicalDetail node OVERRIDES the built-in Tier-3 expander', () => {
    render(
      <EvidenceCard
        record={FULL_RECORD}
        citation={mkCitation()}
        onClose={() => {}}
        technicalDetail={<span data-testid="tier3">tier-3 goes here</span>}
      />,
    );
    const slot = container.querySelector('.evidence-technical-detail');
    expect(slot).not.toBeNull();
    expect(slot!.querySelector('[data-testid="tier3"]')?.textContent).toBe('tier-3 goes here');
    // The built-in expander yields to the override.
    expect(container.querySelector('details.evidence-tier3')).toBeNull();
  });

  it('renders the built-in Tier-3 expander (collapsed) when no override is supplied', () => {
    render(<EvidenceCard record={FULL_RECORD} citation={mkCitation()} onClose={() => {}} />);
    const details = container.querySelector<HTMLDetailsElement>(
      '.evidence-technical-detail details.evidence-tier3',
    );
    expect(details).not.toBeNull();
    expect(details!.open).toBe(false);
    expect(details!.querySelector('summary')?.textContent).toBe('Technical detail');
    // Collapsed ⇒ no body content, no fetch (fetch-on-expand pinned below).
    expect(container.querySelector('.evidence-executed-query')).toBeNull();
    expect(container.querySelector('.evidence-tier3-unavailable')).toBeNull();
  });

  it('exposes a dialog role labelled with the citation number', () => {
    render(<EvidenceCard record={null} citation={mkCitation({ num: 7 })} onClose={() => {}} />);
    const dialog = container.querySelector('[role="dialog"]');
    expect(dialog?.getAttribute('aria-label')).toBe('Evidence for citation 7');
    expect(dialog?.getAttribute('data-cite-num')).toBe('7');
  });
});

/* ── Tier-3 "Technical detail" expander (Task 3.5, contract D5) ──────────── */
describe('<EvidenceCard/> Tier-3 technical detail (D5 fetch-on-expand)', () => {
  const D5_URL =
    '/api/admin/agents/traces/queries/q-3f9c2b81ab04?session_id=session-test-tier3';

  const okDetail = (over: Record<string, unknown> = {}) => ({
    query_ref: 'q-3f9c2b81ab04',
    session_id: 'session-test-tier3',
    system_id: 'erp',
    tool_name: 'erp___query_erp',
    executed_query: "SELECT order_id, status FROM work_orders WHERE status = 'open'",
    kg_artifacts: ['subj-9', 'vm-learned-2'],
    captured_at: '2026-08-03T19:00:00Z',
    ...over,
  });

  // Thin aliases over the SHARED fetch stub (fetch-stub.ts — also used by
  // api-provenance.test.ts) keeping the call sites' ok/err intent readable.
  const okResponse = (body: unknown) => jsonResponse(200, body);

  const errResponse = (status: number, detail: string) => jsonResponse(status, { detail });

  let fetchFn: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    window.currentSessionId = 'session-test-tier3';
    fetchFn = vi.fn();
    vi.stubGlobal('fetch', fetchFn);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete window.currentSessionId;
  });

  /** Expand (or collapse) the built-in expander the way answer-sections.test
   * drives <details>: set .open and dispatch the native toggle event. */
  async function toggleTier3(open: boolean) {
    const details = container.querySelector<HTMLDetailsElement>('details.evidence-tier3')!;
    expect(details).not.toBeNull();
    await act(async () => {
      details.open = open;
      details.dispatchEvent(new Event('toggle', { bubbles: true }));
    });
    return details;
  }

  it('fires NO fetch until expanded, then fetches the D5 URL exactly once', async () => {
    fetchFn.mockResolvedValue(okResponse(okDetail()));
    render(<EvidenceCard record={FULL_RECORD} citation={mkCitation()} onClose={() => {}} />);
    expect(fetchFn).not.toHaveBeenCalled();

    await toggleTier3(true);
    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(fetchFn.mock.calls[0][0]).toBe(D5_URL);

    // Collapse and re-expand: the first-open fetch is never repeated.
    await toggleTier3(false);
    await toggleTier3(true);
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });

  it('renders the fetched executed_query as LITERAL TEXT in a <pre> (hostile string inert)', async () => {
    const hostileSql =
      "SELECT * FROM users -- <script>alert(1)</script><img src=x onerror=alert(2)>";
    fetchFn.mockResolvedValue(okResponse(okDetail({ executed_query: hostileSql })));
    render(<EvidenceCard record={FULL_RECORD} citation={mkCitation()} onClose={() => {}} />);
    await toggleTier3(true);

    const pre = container.querySelector('pre.evidence-executed-query');
    expect(pre).not.toBeNull();
    expect(pre!.textContent).toBe(hostileSql);
    // Literal text nodes only — the hostile markup never becomes elements.
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    // The redemption handle STILL never reaches the DOM.
    expect(container.innerHTML).not.toContain('q-3f9c2b81ab04');
  });

  it('splits a multi-statement executed_query into one <pre> per statement (QueryBlocks wiring)', async () => {
    const multi = 'SET search_path TO erp; SELECT 1; SELECT 2;';
    fetchFn.mockResolvedValue(okResponse(okDetail({ executed_query: multi })));
    render(<EvidenceCard record={FULL_RECORD} citation={mkCitation()} onClose={() => {}} />);
    await toggleTier3(true);

    const pres = [...container.querySelectorAll('pre.evidence-executed-query')];
    expect(pres.map((p) => p.textContent)).toEqual([
      'SET search_path TO erp;',
      'SELECT 1;',
      'SELECT 2;',
    ]);
  });

  it('renders KG artifact ids from the record rules AND the fetched kg_artifacts as text chips', async () => {
    fetchFn.mockResolvedValue(okResponse(okDetail()));
    render(<EvidenceCard record={FULL_RECORD} citation={mkCitation()} onClose={() => {}} />);
    await toggleTier3(true);

    const chips = [...container.querySelectorAll('.evidence-kg-artifact')].map(
      (el) => el.textContent,
    );
    // FULL_RECORD carries rule id 'subj-1'; the fetch adds two more.
    expect(chips).toEqual(['subj-1', 'subj-9', 'vm-learned-2']);
  });

  it.each([403, 404])(
    'HTTP %d renders the honest "not available" line — never an empty spinner',
    async (status) => {
      fetchFn.mockResolvedValue(errResponse(status, 'nope'));
      render(<EvidenceCard record={FULL_RECORD} citation={mkCitation()} onClose={() => {}} />);
      await toggleTier3(true);

      expect(container.querySelector('.evidence-tier3-unavailable')?.textContent).toBe(
        'Technical detail not available for this claim',
      );
      expect(container.querySelector('.evidence-executed-query')).toBeNull();
      expect(container.querySelector('.evidence-tier3-loading')).toBeNull();
      // The server's error detail is never echoed into the card.
      expect(container.textContent).not.toContain('nope');
    },
  );

  it('a network failure renders the same honest line', async () => {
    fetchFn.mockRejectedValue(new TypeError('network down'));
    render(<EvidenceCard record={FULL_RECORD} citation={mkCitation()} onClose={() => {}} />);
    await toggleTier3(true);
    expect(container.querySelector('.evidence-tier3-unavailable')?.textContent).toBe(
      'Technical detail not available for this claim',
    );
  });

  it('a record WITHOUT query_ref renders the honest line on expand — no fetch ever fires', async () => {
    render(<EvidenceCard record={mkRecord()} citation={mkCitation()} onClose={() => {}} />);
    await toggleTier3(true);
    expect(fetchFn).not.toHaveBeenCalled();
    expect(container.querySelector('.evidence-tier3-unavailable')?.textContent).toBe(
      'Technical detail not available for this claim',
    );
  });

  it('a NULL record renders the honest line on expand — no fetch ever fires', async () => {
    render(<EvidenceCard record={null} citation={mkCitation()} onClose={() => {}} />);
    await toggleTier3(true);
    expect(fetchFn).not.toHaveBeenCalled();
    expect(container.querySelector('.evidence-tier3-unavailable')).not.toBeNull();
  });

  it("an over-bound entry surfaces the store's honest note instead of query text", async () => {
    fetchFn.mockResolvedValue(
      okResponse(okDetail({ executed_query: '', note: 'query text exceeded bound' })),
    );
    render(<EvidenceCard record={FULL_RECORD} citation={mkCitation()} onClose={() => {}} />);
    await toggleTier3(true);

    expect(container.querySelector('.evidence-executed-query')).toBeNull();
    expect(container.querySelector('.evidence-query-absent')?.textContent).toBe(
      'query text exceeded bound',
    );
  });

  it('an empty executed_query WITHOUT a note renders the not-captured line (D6 honesty)', async () => {
    fetchFn.mockResolvedValue(okResponse(okDetail({ executed_query: '', kg_artifacts: [] })));
    render(<EvidenceCard record={mkRecord({ query_ref: 'q-3f9c2b81ab04' })} citation={mkCitation()} onClose={() => {}} />);
    await toggleTier3(true);

    expect(container.querySelector('.evidence-query-absent')?.textContent).toBe(
      'executed query not captured for this call',
    );
    expect(container.querySelector('.evidence-kg-artifacts')).toBeNull();
  });

  it('encodes hostile ref/session values into the fetch URL (no raw interpolation)', async () => {
    fetchFn.mockResolvedValue(errResponse(404, 'Unknown or expired query_ref'));
    window.currentSessionId = 'sess/../<evil>';
    render(
      <EvidenceCard
        record={mkRecord({ query_ref: 'q-../../etc' })}
        citation={mkCitation()}
        onClose={() => {}}
      />,
    );
    await toggleTier3(true);
    const url = fetchFn.mock.calls[0][0] as string;
    expect(url).toBe(
      '/api/admin/agents/traces/queries/q-..%2F..%2Fetc?session_id=sess%2F..%2F%3Cevil%3E',
    );
  });
});

describe('<EvidenceCard/> Wave-6 investigation trace wiring (W6-F6)', () => {
  let fetchFn: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    window.currentSessionId = 'session-test-w6';
    fetchFn = vi.fn();
    vi.stubGlobal('fetch', fetchFn);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete window.currentSessionId;
  });

  async function toggleInvestigation(open: boolean) {
    const details = container.querySelector<HTMLDetailsElement>(
      'details.evidence-investigation',
    )!;
    expect(details).not.toBeNull();
    await act(async () => {
      details.open = open;
      details.dispatchEvent(new Event('toggle', { bubbles: true }));
    });
    return details;
  }

  it('renders the collapsed section as the LAST card child and fetches only on expand', async () => {
    fetchFn.mockResolvedValue(
      jsonResponse(200, {
        query_ref: 'q-3f9c2b81ab04',
        session_id: 'session-test-w6',
        system_id: 'erp',
        origin: 'system-agent',
        question: 'How many open work orders?',
        steps: [],
        steps_dropped: 0,
        final_answer: 'Forty-two.',
        captured_at: '2026-08-04T18:00:00Z',
      }),
    );
    render(<EvidenceCard record={FULL_RECORD} citation={mkCitation()} onClose={() => {}} />);

    const card = container.querySelector('.evidence-card')!;
    const last = card.lastElementChild!;
    expect(last.matches('details.evidence-investigation')).toBe(true);
    expect(last.querySelector('summary')?.textContent).toBe('Investigation trace');
    // the pre-Wave-6 body above is intact (technical-detail block precedes it)
    expect(last.previousElementSibling?.classList.contains('evidence-technical-detail')).toBe(
      true,
    );
    expect(fetchFn).not.toHaveBeenCalled();

    await toggleInvestigation(true);
    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(String(fetchFn.mock.calls[0][0])).toBe(
      '/api/admin/agents/traces/investigations/q-3f9c2b81ab04?session_id=session-test-w6',
    );
    expect(container.textContent).toContain('Asked: How many open work orders?');
    expect(container.textContent).toContain('Responded: Forty-two.');
  });

  it('a record WITHOUT query_ref renders the honest line with zero fetches (legacy)', async () => {
    render(<EvidenceCard record={mkRecord()} citation={mkCitation()} onClose={() => {}} />);
    await toggleInvestigation(true);
    expect(fetchFn).not.toHaveBeenCalled();
    expect(container.textContent).toContain(
      'Investigation trace not available for this claim',
    );
  });

  it('a NULL record still renders the section with the honest line, zero fetches', async () => {
    render(<EvidenceCard record={null} citation={mkCitation()} onClose={() => {}} />);
    await toggleInvestigation(true);
    expect(fetchFn).not.toHaveBeenCalled();
    expect(container.textContent).toContain(
      'Investigation trace not available for this claim',
    );
  });
});
