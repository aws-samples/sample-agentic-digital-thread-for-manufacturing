// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests for the Tier-3 "Routing queries" affordance (<EvidenceCard/> —
 * additive top-level graph_queries redemption). Pins:
 * - ABSENT key ⇒ today's expander verbatim: no Routing-queries node, no
 *   fetch (legacy-safe by construction);
 * - NEVER auto-fetches: rendering the card AND opening the expander fire
 *   no request — only the explicit per-row click does, once, against the
 *   SAME D5 redemption URL family (percent-encoded ref);
 * - hostile executed_query renders as a LITERAL text node (zero-innerHTML —
 *   no script/img elements materialize);
 * - honest failure: 403/404/network collapse to the SAME Tier-3
 *   "Technical detail not available for this claim" line;
 * - hostile-shaped rows (non-string/empty query_ref) drop out. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { EvidenceCard } from '../chat/EvidenceCard';
import type { GraphQueryRef, ProvenanceRecord } from '../chat/provenance';
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
  window.currentSessionId = 'session-test-gq';
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
    tool: 'mes___query',
    label: 'MES',
    nodeId: 'sys-mes',
    num: 1,
    findingNodeId: null,
  };
}

/** Record WITHOUT query_ref — the routing list is independent of the
 * expander's own fetch, and this keeps the expander itself silent. */
function mkRecord(over: Partial<ProvenanceRecord> = {}): ProvenanceRecord {
  return {
    citation_num: 1,
    tool_id: 'toolu_gq',
    source_system: 'mes',
    captured_at: '2026-08-04T16:00:00Z',
    verification: 'partial',
    ...over,
  };
}

const REF = 'q-0123456789ab';
const ROW: GraphQueryRef = { query_ref: REF, tool_name: 'explore_graph' };
const QUERY_URL = `/api/admin/agents/traces/queries/${REF}?session_id=session-test-gq`;

const okDetail = (over: Record<string, unknown> = {}) => ({
  query_ref: REF,
  session_id: 'session-test-gq',
  system_id: 'kg',
  tool_name: 'explore_graph',
  executed_query: 'MATCH (n:Property) RETURN n LIMIT 5',
  kg_artifacts: [],
  captured_at: '2026-08-04T16:00:00Z',
  ...over,
});

/** Open the Tier-3 expander the way evidence-card.test.tsx drives <details>. */
async function openTier3() {
  const details = container.querySelector<HTMLDetailsElement>('details.evidence-tier3')!;
  expect(details).not.toBeNull();
  await act(async () => {
    details.open = true;
    details.dispatchEvent(new Event('toggle', { bubbles: true }));
  });
}

async function clickRoutingQuery() {
  const button = container.querySelector<HTMLButtonElement>('button.evidence-routing-query-view')!;
  expect(button).not.toBeNull();
  await act(async () => {
    button.click();
  });
}

describe('<EvidenceCard/> Tier-3 routing queries (graph_queries)', () => {
  it('ABSENT key ⇒ no Routing-queries node, no fetch — legacy expander verbatim', async () => {
    render(<EvidenceCard record={mkRecord()} citation={mkCitation()} onClose={() => {}} />);
    await openTier3();
    expect(container.querySelector('.evidence-routing-queries')).toBeNull();
    expect(container.querySelector('button.evidence-routing-query-view')).toBeNull();
    expect(container.textContent).toContain('Technical detail not available for this claim');
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it('never auto-fetches: render + expand fire nothing; only the click fetches, once', async () => {
    fetchFn.mockResolvedValue(jsonResponse(200, okDetail()));
    render(
      <EvidenceCard
        record={mkRecord()}
        citation={mkCitation()}
        graphQueries={[ROW]}
        onClose={() => {}}
      />,
    );
    expect(fetchFn).not.toHaveBeenCalled();

    await openTier3();
    expect(fetchFn).not.toHaveBeenCalled(); // list visible, still silent
    expect(container.textContent).toContain('Routing queries');
    const button = container.querySelector('button.evidence-routing-query-view');
    expect(button?.textContent).toBe('explore_graph');

    await clickRoutingQuery();
    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(fetchFn.mock.calls[0][0]).toBe(QUERY_URL);
    expect(container.textContent).toContain('MATCH (n:Property) RETURN n LIMIT 5');
  });

  it('percent-encodes hostile ref values in the fetch path', async () => {
    fetchFn.mockResolvedValue(jsonResponse(404, { detail: 'Unknown or expired query_ref' }));
    render(
      <EvidenceCard
        record={mkRecord()}
        citation={mkCitation()}
        graphQueries={[{ query_ref: 'q-../evil?x=1', tool_name: 'explore_graph' }]}
        onClose={() => {}}
      />,
    );
    await openTier3();
    await clickRoutingQuery();
    expect(fetchFn.mock.calls[0][0]).toBe(
      '/api/admin/agents/traces/queries/q-..%2Fevil%3Fx%3D1?session_id=session-test-gq',
    );
  });

  it('renders a hostile executed_query as LITERAL text (zero-innerHTML inert)', async () => {
    fetchFn.mockResolvedValue(
      jsonResponse(
        200,
        okDetail({ executed_query: '<img src=x onerror=alert(1)><script>alert(2)</script>' }),
      ),
    );
    render(
      <EvidenceCard
        record={mkRecord()}
        citation={mkCitation()}
        graphQueries={[ROW]}
        onClose={() => {}}
      />,
    );
    await openTier3();
    await clickRoutingQuery();

    const pre = container.querySelector('pre.evidence-routing-query-text')!;
    expect(pre).not.toBeNull();
    expect(pre.textContent).toBe('<img src=x onerror=alert(1)><script>alert(2)</script>');
    // Zero-innerHTML: the hostile markup never becomes elements.
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
  });

  it.each([403, 404])(
    'HTTP %d collapses to the SAME honest Tier-3 line — never an empty spinner',
    async (status) => {
      fetchFn.mockResolvedValue(jsonResponse(status, { detail: 'nope' }));
      render(
        <EvidenceCard
          record={mkRecord()}
          citation={mkCitation()}
          graphQueries={[ROW]}
          onClose={() => {}}
        />,
      );
      await openTier3();
      await clickRoutingQuery();
      const line = container.querySelector('.evidence-routing-query-unavailable')!;
      expect(line).not.toBeNull();
      expect(line.textContent).toBe('Technical detail not available for this claim');
      expect(container.querySelector('.evidence-routing-query-loading')).toBeNull();
    },
  );

  it('network failure renders the same honest line', async () => {
    fetchFn.mockRejectedValue(new TypeError('network down'));
    render(
      <EvidenceCard
        record={mkRecord()}
        citation={mkCitation()}
        graphQueries={[ROW]}
        onClose={() => {}}
      />,
    );
    await openTier3();
    await clickRoutingQuery();
    expect(container.querySelector('.evidence-routing-query-unavailable')?.textContent).toBe(
      'Technical detail not available for this claim',
    );
  });

  it('over-bound entry surfaces the store note; empty query falls back to the honest line', async () => {
    fetchFn.mockResolvedValue(
      jsonResponse(200, okDetail({ executed_query: '', note: 'query text exceeded bound' })),
    );
    render(
      <EvidenceCard
        record={mkRecord()}
        citation={mkCitation()}
        graphQueries={[ROW]}
        onClose={() => {}}
      />,
    );
    await openTier3();
    await clickRoutingQuery();
    expect(container.querySelector('.evidence-query-absent')?.textContent).toBe(
      'query text exceeded bound',
    );
    expect(container.querySelector('pre.evidence-routing-query-text')).toBeNull();
  });

  it('hostile-shaped rows drop out; valid siblings still render', async () => {
    const hostile = [
      null,
      { query_ref: '', tool_name: 'x' },
      { query_ref: 42, tool_name: 'x' },
      { query_ref: 'q-ok', tool_name: 7 },
      ROW,
    ] as unknown as GraphQueryRef[];
    render(
      <EvidenceCard
        record={mkRecord()}
        citation={mkCitation()}
        graphQueries={hostile}
        onClose={() => {}}
      />,
    );
    await openTier3();
    const buttons = container.querySelectorAll('button.evidence-routing-query-view');
    expect(buttons).toHaveLength(1);
    expect(buttons[0].textContent).toBe('explore_graph');
    expect(fetchFn).not.toHaveBeenCalled();
  });
});
