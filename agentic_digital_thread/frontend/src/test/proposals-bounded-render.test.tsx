// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — bounded proposals render. A live queue reached 1,216 pending rows
 * (6.6 MB) and took >30 s to paint; each section now renders
 * SECTION_RENDER_STEP cards plus a "Show more" control, and family groups are
 * never split across the boundary. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { ProposedMapping } from '../api/types/proposals';
import { proposalsStore } from '../views/architecture/proposals';
import {
  limitFamilyGroups,
  ProposalsList,
  SECTION_RENDER_STEP,
} from '../views/architecture/ProposalsPanel';

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
  proposalsStore.set({
    ...proposalsStore.get(),
    phase: 'idle',
    mappings: [],
    statusFilter: 'pending',
    optimistic: {},
  });
});

function rows(n: number, status: ProposedMapping['status'] = 'pending'): ProposedMapping[] {
  return Array.from({ length: n }, (_, i) => ({
    mapping_id: `m-${i}`,
    proposed_type: 'value_mapping',
    status,
    confidence: '0.8',
    from_system: 'erp',
    from_field: `orders.f${i}`,
    from_value: `A${i}`,
    to_system: 'mes',
    to_field: `runs.g${i}`,
    to_value: `B${i}`,
  }));
}

function renderReady(mappings: ProposedMapping[], statusFilter = 'pending') {
  proposalsStore.set({ ...proposalsStore.get(), phase: 'ready', mappings, statusFilter } as never);
  act(() => root.render(<ProposalsList />));
}

const cardCount = () => container.querySelectorAll('.sl-card').length;
const showMore = () => container.querySelector<HTMLButtonElement>('.sl-show-more');

describe('bounded proposals render', () => {
  it('renders one step of cards, then reveals the next step on click', () => {
    renderReady(rows(SECTION_RENDER_STEP * 2 + 5));
    expect(cardCount()).toBe(SECTION_RENDER_STEP);
    expect(showMore()?.textContent).toContain(`${SECTION_RENDER_STEP + 5} not shown`);
    act(() => showMore()!.click());
    expect(cardCount()).toBe(SECTION_RENDER_STEP * 2);
    act(() => showMore()!.click());
    expect(cardCount()).toBe(SECTION_RENDER_STEP * 2 + 5);
    expect(showMore()).toBeNull();
  });

  it('renders no control when a section fits in one step', () => {
    renderReady(rows(3));
    expect(cardCount()).toBe(3);
    expect(showMore()).toBeNull();
  });

  it('bounds non-pending tabs too', () => {
    renderReady(rows(SECTION_RENDER_STEP + 1, 'accepted'), 'accepted');
    expect(cardCount()).toBe(SECTION_RENDER_STEP);
    expect(showMore()).not.toBeNull();
  });
});

describe('limitFamilyGroups', () => {
  const g = (n: number) => ({ members: Array.from({ length: n }) });

  it('keeps whole groups until the limit is reached', () => {
    expect(limitFamilyGroups([g(3), g(3), g(3)], 4).map((x) => x.members.length)).toEqual([3, 3]);
    expect(limitFamilyGroups([g(5), g(1)], 2).map((x) => x.members.length)).toEqual([5]);
    expect(limitFamilyGroups([g(1), g(1)], 10)).toHaveLength(2);
    expect(limitFamilyGroups([], 10)).toEqual([]);
  });
});
