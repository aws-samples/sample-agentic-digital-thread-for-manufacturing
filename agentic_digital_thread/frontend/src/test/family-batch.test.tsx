// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — ADR-0063 D4 family batch adjudication in the review list.
 *
 * Pins: pure grouping by the SERVER's family_key (never re-derived here), the
 * group action posting the EXPLICIT id list (no predicate/wildcard body and no
 * affordance to build one), honest per-id outcome rendering including partial
 * failure, and the single-row group staying action-free (its card already has
 * Accept/Reject). The card DOM/testids and the D2 trust chip are untouched.
 *
 * Engine-neutral: sys-a / sys-b, generic placeholder fields (ADR-0044). */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { BatchAdjudicationResult, ProposedMapping } from '../api/types/proposals';
import {
  FamilyBatchHeader,
  familyGroups,
  familySummary,
  type FamilyGroup,
} from '../views/architecture/proposal-families';

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

const FAMILY_A = 'value_mapping|semantic_similarity|table_a.col_a->table_b.col_b';
const FAMILY_B = 'value_mapping|hierarchy_position|table_a.col_a->table_b.col_b';

function row(id: string, over: Partial<ProposedMapping> = {}): ProposedMapping {
  return {
    mapping_id: id,
    proposed_type: 'value_mapping',
    status: 'pending',
    confidence: '0.9',
    from_system: 'sys-a',
    from_field: 'table_a.col_a',
    to_system: 'sys-b',
    to_field: 'table_b.col_b',
    method: 'semantic_similarity',
    family_key: FAMILY_A,
    family_count: 2,
    action_eligibility: {
      accept: { eligible: true },
      reject: { eligible: true },
    },
    ...over,
  };
}

describe('familyGroups (pure — server key only)', () => {
  it('groups by the server family_key and keeps distinct families apart', () => {
    const groups = familyGroups([
      row('m-1'),
      row('m-2', { family_key: FAMILY_B, method: 'hierarchy_position' }),
      row('m-3'),
    ]);
    expect(groups.map((g) => g.key)).toEqual([FAMILY_A, FAMILY_B]);
    expect(groups[0].members.map((m) => m.mapping_id)).toEqual(['m-1', 'm-3']);
    expect(groups[1].members.map((m) => m.mapping_id)).toEqual(['m-2']);
  });

  it('never invents a family for rows the server did not enrich', () => {
    const groups = familyGroups([
      row('m-1', { family_key: undefined }),
      row('m-2', { family_key: undefined }),
    ]);
    expect(groups).toHaveLength(2);
    expect(groups.every((g) => g.key === '')).toBe(true);
  });

  it('summarizes the grain from the members own fields', () => {
    expect(familySummary([row('m-1')])).toContain('table_a.col_a → table_b.col_b');
    expect(familySummary([row('m-1')])).toContain('semantic_similarity');
    expect(familySummary([])).toBe('');
  });
});

function renderHeader(
  group: FamilyGroup,
  onBatch?: (a: 'accept' | 'reject', ids: string[]) => Promise<BatchAdjudicationResult>,
) {
  act(() => {
    root.render(<FamilyBatchHeader group={group} onBatch={onBatch} />);
  });
}

const GROUP: FamilyGroup = { key: FAMILY_A, members: [row('m-1'), row('m-2'), row('m-3')] };

async function click(selector: string) {
  const button = container.querySelector<HTMLButtonElement>(selector);
  expect(button).not.toBeNull();
  await act(async () => {
    button?.click();
  });
}

describe('FamilyBatchHeader', () => {
  it('renders nothing for a single-row family (the card has its own actions)', () => {
    renderHeader({ key: FAMILY_A, members: [row('m-1')] });
    expect(container.querySelector('[data-testid="sl-family-head"]')).toBeNull();
  });

  it('renders nothing when the server sent no family key', () => {
    renderHeader({ key: '', members: [row('m-1'), row('m-2')] });
    expect(container.querySelector('[data-testid="sl-family-head"]')).toBeNull();
  });

  it('labels both group actions with the member count', () => {
    renderHeader(GROUP);
    const head = container.querySelector('[data-testid="sl-family-head"]');
    expect(head?.getAttribute('data-family-key')).toBe(FAMILY_A);
    expect(head?.textContent).toContain('3 in this family');
    expect(container.querySelector('.sl-family-reject')?.textContent).toBe(
      'Reject 3 eligible in this family',
    );
    expect(container.querySelector('.sl-family-accept')?.textContent).toBe(
      'Accept 3 eligible in this family',
    );
  });

  it('posts the EXPLICIT id list — reject and accept alike', async () => {
    const calls: Array<[string, string[]]> = [];
    const onBatch = async (action: 'accept' | 'reject', ids: string[]) => {
      calls.push([action, ids]);
      return { action, requested: ids.length, succeeded: ids.length, failed: 0, results: [] };
    };
    renderHeader(GROUP, onBatch);
    await click('.sl-family-reject');
    await click('.sl-family-accept');
    expect(calls).toEqual([
      ['reject', ['m-1', 'm-2', 'm-3']],
      ['accept', ['m-1', 'm-2', 'm-3']],
    ]);
  });

  it('keeps ineligible cards visible but excludes their ids and action counts', async () => {
    const group: FamilyGroup = {
      key: FAMILY_A,
      members: [
        row('m-1'),
        row('m-2', {
          action_eligibility: {
            accept: { eligible: false, code: 'specialized_accept_required' },
            reject: { eligible: true },
          },
        }),
        row('m-3', { action_eligibility: undefined }),
      ],
    };
    const calls: Array<[string, string[]]> = [];
    renderHeader(group, async (action, ids) => {
      calls.push([action, ids]);
      return { action, requested: ids.length, succeeded: ids.length, failed: 0, results: [] };
    });

    expect(container.querySelector('.sl-family-label')?.textContent).toContain('3 in this family');
    expect(container.querySelector('.sl-family-accept')?.textContent).toContain('1 eligible');
    expect(container.querySelector('.sl-family-reject')?.textContent).toContain('2 eligible');
    await click('.sl-family-accept');
    await click('.sl-family-reject');
    expect(calls).toEqual([
      ['accept', ['m-1']],
      ['reject', ['m-1', 'm-2']],
    ]);
  });

  it('sends only action + mapping_ids to the batch route (no predicate body)', async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify({ action: 'reject', requested: 3, succeeded: 3, failed: 0 }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);
    renderHeader(GROUP);
    await click('.sl-family-reject');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/proposed-mappings/adjudicate-batch');
    const body = JSON.parse(String(init.body)) as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(['action', 'mapping_ids']);
    expect(body.mapping_ids).toEqual(['m-1', 'm-2', 'm-3']);
    // The family key is a DISPLAY key: it is never sent as a selector.
    expect(String(init.body)).not.toContain(FAMILY_A);
  });

  it('renders the per-id outcomes honestly, naming each failure', async () => {
    const onBatch = async () => ({
      action: 'reject',
      requested: 3,
      succeeded: 2,
      failed: 1,
      failed_ids: ['m-2'],
      results: [
        { mapping_id: 'm-1', ok: true, status: 'rejected' },
        { mapping_id: 'm-2', ok: false, error_code: 'not_found' },
        { mapping_id: 'm-3', ok: true, status: 'rejected' },
      ],
    });
    renderHeader(GROUP, onBatch);
    await click('.sl-family-reject');

    const outcome = container.querySelector('[data-testid="sl-family-outcome"]');
    expect(outcome?.textContent).toContain('2 of 3 rejected');
    expect(outcome?.textContent).toContain('1 failed');
    expect(outcome?.textContent).toContain('m-2: Proposal is no longer available.');
    // A partial failure is never flattened into a single verdict.
    expect(outcome?.querySelectorAll('.sl-family-failure')).toHaveLength(1);
  });

  it('surfaces a transport failure instead of claiming success', async () => {
    const onBatch = async () => {
      throw new Error('network down');
    };
    renderHeader(GROUP, onBatch);
    await click('.sl-family-reject');
    const error = container.querySelector('[data-testid="sl-family-error"]')?.textContent ?? '';
    expect(error).toContain('Family action unavailable. Try again.');
    expect(error).not.toContain('network down');
    expect(container.querySelector('[data-testid="sl-family-outcome"]')).toBeNull();
  });
});
