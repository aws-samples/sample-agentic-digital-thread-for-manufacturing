// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — B2 impact-first review list + modify loop (task 2.3 pinned
 * acceptance): consequence-desc ordering of the pending tab (other tabs
 * unchanged), provenance-tier badge text per tier, the conflict banner
 * rendering the review_reason text carried in touches.conflict, the
 * Deactivate button POSTing the existing rule-lifecycle route with the row's
 * neptune_node_id, and the KG chip calling focusKgNode with the node id
 * (hoisted module mock — the kg-node-nav.test.ts pattern). */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { focusKgNodeMock } = vi.hoisted(() => ({ focusKgNodeMock: vi.fn(() => true) }));
vi.mock('../views/architecture/neptune-graph', () => ({ focusKgNode: focusKgNodeMock }));

import type { ProposedMapping } from '../api/types/proposals';
import { clear as clearToasts } from '../shared/ui/toast-store';
import {
  proposalsStore,
  setRuleLifecycle,
  sortMappingsByConsequence,
} from '../views/architecture/proposals';
import { ProposalsList } from '../views/architecture/ProposalsPanel';
import { SlCard } from '../views/architecture/SlCards';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type W = { switchArchTab?: (t: string) => void };

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  clearToasts();
  focusKgNodeMock.mockClear();
  focusKgNodeMock.mockReturnValue(true);
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  clearToasts();
  delete (window as W).switchArchTab;
  proposalsStore.set({
    phase: 'idle',
    mappings: [],
    error: '',
    statusFilter: 'pending',
    optimistic: {},
    ruleLifecycle: {},
    guidancePhase: 'idle',
    guidance: [],
    guidanceError: '',
    edit: null,
  });
});

function render(el: React.ReactElement) {
  act(() => {
    root.render(el);
  });
}

const click = (el: Element | null) => {
  expect(el).not.toBeNull();
  act(() => {
    (el as HTMLElement).click();
  });
};

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: '',
    headers: { get: () => 'application/json' },
    text: async () => JSON.stringify(body),
    json: async () => body,
  } as unknown as Response;
}

const base: ProposedMapping = {
  mapping_id: 'm-1',
  proposed_type: 'join_path',
  status: 'pending',
  confidence: '0.8',
  from_system: 'mes-poc',
  from_field: 'ops.operator_id',
  to_system: 'cmms-main',
  to_field: 'techs.operator_id',
};

/* ── Consequence ordering ───────────────────────────────────────────────── */

describe('sortMappingsByConsequence', () => {
  it('orders by consequence desc, missing scores last in input order', () => {
    const rows: ProposedMapping[] = [
      { ...base, mapping_id: 'low', consequence: 1.2 },
      { ...base, mapping_id: 'none' },
      { ...base, mapping_id: 'high', consequence: 9.9 },
      { ...base, mapping_id: 'bad', consequence: Number('nope') },
    ];
    expect(sortMappingsByConsequence(rows).map((m) => m.mapping_id)).toEqual([
      'high',
      'low',
      'none',
      'bad',
    ]);
  });

  it('is a byte-identical pass-through for pre-enrichment payloads (no consequence)', () => {
    const rows: ProposedMapping[] = [
      { ...base, mapping_id: 'a' },
      { ...base, mapping_id: 'b' },
      { ...base, mapping_id: 'c' },
    ];
    expect(sortMappingsByConsequence(rows)).toEqual(rows);
  });

  it('breaks ties by input order (stable)', () => {
    const rows: ProposedMapping[] = [
      { ...base, mapping_id: 'first', consequence: 3.0 },
      { ...base, mapping_id: 'second', consequence: 3.0 },
    ];
    expect(sortMappingsByConsequence(rows).map((m) => m.mapping_id)).toEqual(['first', 'second']);
  });
});

describe('pending tab impact-first ordering', () => {
  it('renders pending cards consequence-desc regardless of confidence', () => {
    proposalsStore.set({
      ...proposalsStore.get(),
      phase: 'ready',
      statusFilter: 'pending',
      mappings: [
        { ...base, mapping_id: 'conf-hi', confidence: '0.95', consequence: 0.4 },
        { ...base, mapping_id: 'conf-lo', confidence: '0.10', consequence: 7.5 },
        { ...base, mapping_id: 'no-score', confidence: '0.50' },
      ],
    });
    render(<ProposalsList />);
    const ids = [...container.querySelectorAll('.sl-card')].map((c) =>
      c.getAttribute('data-mapping-id'),
    );
    expect(ids).toEqual(['conf-lo', 'conf-hi', 'no-score']);
  });

  it('leaves non-pending tabs confidence-ordered (unchanged)', () => {
    proposalsStore.set({
      ...proposalsStore.get(),
      phase: 'ready',
      statusFilter: 'accepted',
      mappings: [
        { ...base, mapping_id: 'acc-lo', status: 'accepted', confidence: '0.2', consequence: 9.0 },
        { ...base, mapping_id: 'acc-hi', status: 'accepted', confidence: '0.9', consequence: 0.1 },
      ],
    });
    render(<ProposalsList />);
    const ids = [...container.querySelectorAll('.sl-card')].map((c) =>
      c.getAttribute('data-mapping-id'),
    );
    expect(ids).toEqual(['acc-hi', 'acc-lo']);
  });
});

/* ── Provenance-tier badge ──────────────────────────────────────────────── */

describe('provenance-tier badge', () => {
  it.each([
    [0, 'Steward'],
    [1, 'Accepted'],
    [2, 'AI-proposed'],
  ])('tier %i renders "%s" on a pending card', (tier, label) => {
    render(<SlCard m={{ ...base, provenance_tier: tier }} />);
    expect(container.querySelector('.sl-tier')!.textContent).toBe(label);
  });

  it('renders no badge when the tier is absent or unknown', () => {
    render(<SlCard m={base} />);
    expect(container.querySelector('.sl-tier')).toBeNull();
    render(<SlCard m={{ ...base, provenance_tier: 7 }} />);
    expect(container.querySelector('.sl-tier')).toBeNull();
  });
});

/* ── Conflict banner ────────────────────────────────────────────────────── */

describe('conflict banner (touches.conflict)', () => {
  const reason = 'cross-site link vouched only by unverified authority ar-cmms-operator';

  it('renders the review_reason text carried in touches.conflict', () => {
    render(<SlCard m={{ ...base, touches: { conflict: reason } }} />);
    const banner = container.querySelector('.sl-conflict')!;
    expect(banner.textContent).toContain('⚠ Conflict');
    expect(banner.querySelector('.sl-review-reason')!.textContent).toBe(reason);
  });

  it('dedupes when the conflict text equals the row review_reason (review banner already shows it)', () => {
    render(<SlCard m={{ ...base, review_reason: reason, touches: { conflict: reason } }} />);
    expect(container.querySelector('.sl-conflict')).toBeNull();
    // The existing review banner carries the text exactly once.
    const reasons = [...container.querySelectorAll('.sl-review-reason')];
    expect(reasons).toHaveLength(1);
    expect(reasons[0].textContent).toBe(reason);
  });

  it('renders nothing when conflict is empty', () => {
    render(<SlCard m={{ ...base, touches: { conflict: '' } }} />);
    expect(container.querySelector('.sl-conflict')).toBeNull();
  });
});

/* ── Modify loop: rule lifecycle ────────────────────────────────────────── */

const acceptedRule: ProposedMapping = {
  ...base,
  mapping_id: 'm-acc',
  proposed_type: 'identity_rule',
  status: 'accepted',
  neptune_node_id: 'subj-aaaaaaaaaaaaaaaa',
};

describe('rule lifecycle buttons (accepted identity rules)', () => {
  it('renders Deactivate and fires onLifecycle with the graph node id', () => {
    const onLifecycle = vi.fn();
    render(<SlCard m={acceptedRule} onLifecycle={onLifecycle} />);
    const btn = container.querySelector('.sl-lifecycle.deactivate');
    click(btn);
    expect(onLifecycle).toHaveBeenCalledWith('subj-aaaaaaaaaaaaaaaa', 'deactivate');
  });

  it('a deactivated mark flips the button to Reactivate', () => {
    const onLifecycle = vi.fn();
    render(<SlCard m={acceptedRule} lifecycle="deactivated" onLifecycle={onLifecycle} />);
    expect(container.querySelector('.sl-lifecycle.deactivate')).toBeNull();
    const btn = container.querySelector('.sl-lifecycle.reactivate');
    click(btn);
    expect(onLifecycle).toHaveBeenCalledWith('subj-aaaaaaaaaaaaaaaa', 'reactivate');
  });

  it('renders no lifecycle button on pending, non-identity, or node-less cards', () => {
    render(<SlCard m={{ ...acceptedRule, status: 'pending' }} />);
    expect(container.querySelector('.sl-lifecycle')).toBeNull();
    render(<SlCard m={{ ...acceptedRule, proposed_type: 'join_path' }} />);
    expect(container.querySelector('.sl-lifecycle')).toBeNull();
    render(<SlCard m={{ ...acceptedRule, neptune_node_id: undefined }} />);
    expect(container.querySelector('.sl-lifecycle')).toBeNull();
  });

  it('setRuleLifecycle POSTs the existing lifecycle route and marks the node', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'deactivated' }))
      .mockResolvedValueOnce(jsonResponse({ mappings: [acceptedRule] }));
    vi.stubGlobal('fetch', fetchMock);

    await act(async () => {
      await setRuleLifecycle('subj-aaaaaaaaaaaaaaaa', 'deactivate');
    });

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/thread/rules/subj-aaaaaaaaaaaaaaaa/deactivate', {
      method: 'POST',
      credentials: 'same-origin',
    });
    // Silent reconcile through the existing reload path.
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/proposed-mappings?status=pending', {
      credentials: 'same-origin',
    });
    expect(proposalsStore.get().ruleLifecycle).toEqual({
      'subj-aaaaaaaaaaaaaaaa': 'deactivated',
    });
  });

  it('setRuleLifecycle reactivate clears the mark; failures leave it untouched', async () => {
    proposalsStore.set({
      ...proposalsStore.get(),
      ruleLifecycle: { 'subj-aaaaaaaaaaaaaaaa': 'deactivated' },
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'reactivated' }))
      .mockResolvedValueOnce(jsonResponse({ mappings: [] }))
      .mockResolvedValueOnce(jsonResponse({ detail: 'graph offline' }, 503));
    vi.stubGlobal('fetch', fetchMock);

    await act(async () => {
      await setRuleLifecycle('subj-aaaaaaaaaaaaaaaa', 'reactivate');
    });
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/thread/rules/subj-aaaaaaaaaaaaaaaa/reactivate', {
      method: 'POST',
      credentials: 'same-origin',
    });
    expect(proposalsStore.get().ruleLifecycle).toEqual({});

    await act(async () => {
      await setRuleLifecycle('subj-aaaaaaaaaaaaaaaa', 'deactivate');
    });
    expect(proposalsStore.get().ruleLifecycle).toEqual({}); // failed POST → no mark
  });
});

/* ── KG focus chip ──────────────────────────────────────────────────────── */

describe('KG focus chip', () => {
  it('activates the neptune tab and calls focusKgNode with the node id', async () => {
    vi.useFakeTimers();
    const switchArchTab = vi.fn();
    (window as W).switchArchTab = switchArchTab;

    render(<SlCard m={acceptedRule} />);
    const chip = container.querySelector<HTMLAnchorElement>('.sl-kg-link')!;
    expect(chip.dataset.nodeId).toBe('subj-aaaaaaaaaaaaaaaa');
    click(chip);

    expect(switchArchTab).toHaveBeenCalledWith('neptune');
    expect(focusKgNodeMock).not.toHaveBeenCalled(); // deferred behind the tab transition
    await act(async () => {
      await vi.runAllTimersAsync(); // focus timeout + dynamic import
    });
    expect(focusKgNodeMock).toHaveBeenCalledTimes(1);
    expect(focusKgNodeMock).toHaveBeenCalledWith('subj-aaaaaaaaaaaaaaaa');
  });

  it('renders no chip without a graph node id', () => {
    render(<SlCard m={base} />);
    expect(container.querySelector('.sl-kg-link')).toBeNull();
  });
});
