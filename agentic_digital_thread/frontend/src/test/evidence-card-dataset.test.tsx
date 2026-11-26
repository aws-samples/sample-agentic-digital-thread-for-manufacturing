// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests for the Wave-4 "View full data" affordance in the Tier-3 expander
 * (<EvidenceCard/> — claim-check dataset_ref redemption). Pins:
 * - ABSENT refs ⇒ today's expander verbatim: no affordance node, no dataset
 *   fetch, the honest "Technical detail not available" line unchanged;
 * - NEVER auto-fetches: rendering the card AND opening the expander fire no
 *   dataset request — only the explicit "View full data" click does, once;
 * - exact URL construction with percent-encoding (hostile ref values cannot
 *   break the path);
 * - hostile-payload inertness: fetched series values render as LITERAL text
 *   cells (zero-innerHTML — no script/img elements materialize);
 * - honest error lines per status: 403 admin-gated, 404 unknown/expired,
 *   network failure — never an empty spinner;
 * - bounded display: rows past the client cap collapse to an honest
 *   "showing first N of M points" line. */
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
let fetchFn: ReturnType<typeof vi.fn>;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  window.currentSessionId = 'session-test-w4';
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
    tool: 'sitewise___get_asset_history',
    label: 'SiteWise',
    nodeId: 'sys-sitewise',
    num: 2,
    findingNodeId: null,
  };
}

function mkRecord(over: Partial<ProvenanceRecord> = {}): ProvenanceRecord {
  return {
    citation_num: 2,
    tool_id: 'toolu_w4',
    source_system: 'sitewise',
    captured_at: '2026-08-03T19:00:00Z',
    verification: 'partial',
    // No query_ref: the dataset affordance is independent of the query fetch.
    ...over,
  };
}

const REF = 'tel-0123456789ab';
const DATASET_URL = `/api/admin/agents/traces/datasets/${REF}?session_id=session-test-w4`;

const okDataset = (over: Record<string, unknown> = {}) => ({
  dataset_ref: REF,
  stored_at: 1754300000,
  point_count: 2,
  series: [
    { timestamp: '2026-04-30T07:00:00Z', value: 10.5, quality: 'GOOD' },
    { timestamp: '2026-04-30T07:01:00Z', value: 11, quality: 'GOOD' },
  ],
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

async function clickViewFullData() {
  const button = container.querySelector<HTMLButtonElement>('button.evidence-dataset-view')!;
  expect(button).not.toBeNull();
  await act(async () => {
    button.click();
  });
}

describe('<EvidenceCard/> Wave-4 dataset view', () => {
  it('ABSENT refs ⇒ no affordance, no fetch — today’s expander verbatim', async () => {
    render(<EvidenceCard record={mkRecord()} citation={mkCitation()} onClose={() => {}} />);
    await openTier3();
    expect(container.querySelector('.evidence-dataset')).toBeNull();
    expect(container.querySelector('button.evidence-dataset-view')).toBeNull();
    // No query_ref either, so the expander shows ONLY the honest line and
    // fires NO network request of any kind.
    expect(container.textContent).toContain('Technical detail not available for this claim');
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it('never auto-fetches: render + expand fire nothing; only the click fetches, once', async () => {
    fetchFn.mockResolvedValue(jsonResponse(200, okDataset()));
    render(
      <EvidenceCard
        record={mkRecord({ dataset_refs: [REF] })}
        citation={mkCitation()}
        onClose={() => {}}
      />,
    );
    expect(fetchFn).not.toHaveBeenCalled();

    await openTier3();
    expect(fetchFn).not.toHaveBeenCalled(); // affordance visible, still silent
    expect(container.querySelector('button.evidence-dataset-view')).not.toBeNull();

    await clickViewFullData();
    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(fetchFn.mock.calls[0][0]).toBe(DATASET_URL);
  });

  it('percent-encodes hostile ref values in the fetch path', async () => {
    fetchFn.mockResolvedValue(jsonResponse(404, { detail: 'Unknown or expired dataset_ref' }));
    render(
      <EvidenceCard
        record={mkRecord({ dataset_refs: ['tel-../evil?x=1'] })}
        citation={mkCitation()}
        onClose={() => {}}
      />,
    );
    await openTier3();
    await clickViewFullData();
    expect(fetchFn.mock.calls[0][0]).toBe(
      '/api/admin/agents/traces/datasets/tel-..%2Fevil%3Fx%3D1?session_id=session-test-w4',
    );
  });

  it('renders fetched points as LITERAL text cells (hostile payload inert)', async () => {
    fetchFn.mockResolvedValue(
      jsonResponse(
        200,
        okDataset({
          series: [
            {
              timestamp: '<img src=x onerror=alert(1)>',
              value: '<script>alert(2)</script>',
              quality: 'GOOD',
            },
          ],
          point_count: 1,
        }),
      ),
    );
    render(
      <EvidenceCard
        record={mkRecord({ dataset_refs: [REF] })}
        citation={mkCitation()}
        onClose={() => {}}
      />,
    );
    await openTier3();
    await clickViewFullData();

    const cells = [...container.querySelectorAll('.evidence-dataset-table td')].map(
      (td) => td.textContent,
    );
    expect(cells).toEqual(['<img src=x onerror=alert(1)>', '<script>alert(2)</script>', 'GOOD']);
    // Zero-innerHTML: the hostile markup never becomes elements.
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
  });

  it.each([
    [403, 'Full data requires admin access'],
    [404, 'Full data expired or no longer available'],
  ])('HTTP %d renders its honest line — never an empty spinner', async (status, line) => {
    fetchFn.mockResolvedValue(jsonResponse(status as number, { detail: 'nope' }));
    render(
      <EvidenceCard
        record={mkRecord({ dataset_refs: [REF] })}
        citation={mkCitation()}
        onClose={() => {}}
      />,
    );
    await openTier3();
    await clickViewFullData();
    expect(container.querySelector('.evidence-dataset-error')?.textContent).toBe(line);
    expect(container.querySelector('.evidence-dataset-loading')).toBeNull();
    expect(container.querySelector('.evidence-dataset-table')).toBeNull();
  });

  it('network failure renders the generic honest line', async () => {
    fetchFn.mockRejectedValue(new TypeError('network down'));
    render(
      <EvidenceCard
        record={mkRecord({ dataset_refs: [REF] })}
        citation={mkCitation()}
        onClose={() => {}}
      />,
    );
    await openTier3();
    await clickViewFullData();
    expect(container.querySelector('.evidence-dataset-error')?.textContent).toBe(
      'Full data could not be loaded',
    );
  });

  it('caps displayed rows with an honest count line', async () => {
    const points = Array.from({ length: 501 }, (_, i) => ({
      timestamp: `t-${i}`,
      value: i,
      quality: 'GOOD',
    }));
    fetchFn.mockResolvedValue(
      jsonResponse(200, okDataset({ series: points, point_count: 501 })),
    );
    render(
      <EvidenceCard
        record={mkRecord({ dataset_refs: [REF] })}
        citation={mkCitation()}
        onClose={() => {}}
      />,
    );
    await openTier3();
    await clickViewFullData();
    expect(container.querySelectorAll('.evidence-dataset-table tbody tr')).toHaveLength(500);
    expect(container.querySelector('.evidence-dataset-truncated')?.textContent).toBe(
      'showing first 500 of 501 points',
    );
  });

  it('renders one affordance per handle and non-string entries drop out', async () => {
    render(
      <EvidenceCard
        record={mkRecord({
          dataset_refs: [REF, 'tel-ffffffffffff', 42 as unknown as string, 'q-nope'],
        })}
        citation={mkCitation()}
        onClose={() => {}}
      />,
    );
    await openTier3();
    expect(container.querySelectorAll('button.evidence-dataset-view')).toHaveLength(2);
    expect(fetchFn).not.toHaveBeenCalled();
  });
});
