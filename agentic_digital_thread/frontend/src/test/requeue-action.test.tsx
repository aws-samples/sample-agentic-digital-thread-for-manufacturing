// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — Requeue action on accepted proposal cards (probe-4 debt
 * disposition): the button renders on EVERY accepted card (next to the
 * task-2.3 lifecycle actions on identity rules), fires onRequeue with the
 * row's mapping_id, and the store action POSTs
 * /api/proposed-mappings/{id}/requeue then silently reconciles through the
 * existing reload path (proposal-consequence-order.test.tsx harness). */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ProposedMapping } from '../api/types/proposals';
import { clear as clearToasts, toastStore } from '../shared/ui/toast-store';
import { proposalsStore, requeueMapping } from '../views/architecture/proposals';
import { SlCard } from '../views/architecture/SlCards';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  clearToasts();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  clearToasts();
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

const acceptedVm: ProposedMapping = {
  mapping_id: 'm-vm',
  proposed_type: 'value_mapping',
  status: 'accepted',
  confidence: '0.9',
  from_system: 'mes-poc',
  from_field: 'ops.operator_id',
  to_system: 'cmms-main',
  to_field: 'techs.operator_id',
  neptune_node_id: 'subj-bbbbbbbbbbbbbbbb',
};

/* ── Card button ────────────────────────────────────────────────────────── */

describe('Requeue button on accepted cards', () => {
  it('renders on an accepted card and fires onRequeue with the mapping_id', () => {
    const onRequeue = vi.fn();
    render(<SlCard m={acceptedVm} onRequeue={onRequeue} />);
    const btn = container.querySelector('.sl-requeue');
    click(btn);
    expect(onRequeue).toHaveBeenCalledWith('m-vm');
  });

  it('renders NEXT TO the lifecycle actions on accepted identity rules', () => {
    const onRequeue = vi.fn();
    render(
      <SlCard
        m={{ ...acceptedVm, mapping_id: 'm-ir', proposed_type: 'identity_rule' }}
        onLifecycle={vi.fn()}
        onRequeue={onRequeue}
      />,
    );
    const actions = container.querySelector('.sl-card-actions')!;
    expect(actions.querySelector('.sl-lifecycle.deactivate')).not.toBeNull();
    click(actions.querySelector('.sl-requeue'));
    expect(onRequeue).toHaveBeenCalledWith('m-ir');
  });

  it('never renders on pending cards or without an onRequeue handler', () => {
    render(<SlCard m={{ ...acceptedVm, status: 'pending' }} onRequeue={vi.fn()} />);
    expect(container.querySelector('.sl-requeue')).toBeNull();
    render(<SlCard m={acceptedVm} />);
    expect(container.querySelector('.sl-requeue')).toBeNull();
  });
});

/* ── Store action: POST + silent reconcile (setRuleLifecycle idiom) ─────── */

describe('requeueMapping store action', () => {
  it('POSTs the requeue route and silently reconciles through the reload path', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ status: 'requeued', mapping_id: 'm-vm', graph_artifact: 'removed' }),
      )
      .mockResolvedValueOnce(jsonResponse({ mappings: [{ ...acceptedVm, status: 'pending' }] }));
    vi.stubGlobal('fetch', fetchMock);
    proposalsStore.set({ ...proposalsStore.get(), phase: 'ready' });

    await act(async () => {
      await requeueMapping('m-vm');
    });

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/proposed-mappings/m-vm/requeue', {
      method: 'POST',
      credentials: 'same-origin',
    });
    // Silent reconcile through the existing reload path — no loading blank.
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/proposed-mappings?status=pending', {
      credentials: 'same-origin',
    });
    expect(proposalsStore.get().phase).toBe('ready');
    expect(proposalsStore.get().mappings[0]?.status).toBe('pending');
  });

  it('an error result never triggers a reload and raises an error toast', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ error: 'Mapping m-vm is not accepted' }, 400));
    vi.stubGlobal('fetch', fetchMock);

    await act(async () => {
      await requeueMapping('m-vm');
    });

    expect(fetchMock).toHaveBeenCalledTimes(1); // POST only — no reconcile
    const toasts = toastStore.get().toasts;
    expect(toasts.some((t) => t.kind === 'error' && t.message.includes('Requeue failed'))).toBe(
      true,
    );
  });
});
