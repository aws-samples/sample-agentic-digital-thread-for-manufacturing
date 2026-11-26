// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — status-scoped proposals fetch (2026-09-28).
 *
 * Live: the unfiltered GET /api/proposed-mappings served 1,850 rows / 3.2 MB
 * (every lifecycle bucket) and the browser spent many seconds parsing and
 * reconciling before the tab painted. The store now holds ONLY the selected
 * bucket, fetched with ?status=<statusFilter>; a response for a bucket the
 * steward already left is discarded (load-generation token). */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ProposedMapping } from '../api/types/proposals';
import { clear as clearToasts } from '../shared/ui/toast-store';
import {
  acceptMapping,
  loadProposedMappings,
  proposalsStore,
  reopenMapping,
  setProposalStatusFilter,
} from '../views/architecture/proposals';
import { PendingBadge, ProposalsList } from '../views/architecture/ProposalsPanel';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const INITIAL = proposalsStore.get();

function row(id: string, status: string): ProposedMapping {
  return {
    mapping_id: id,
    proposed_type: 'value_mapping',
    status,
    confidence: '0.5',
    from_system: 'sys-a',
    from_field: 'tbl.col',
    from_value: 'X',
    to_system: 'sys-b',
    to_field: 'tbl2.col2',
    to_value: 'Y',
  };
}

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: '',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function deferred() {
  let resolve!: (r: Response) => void;
  const promise = new Promise<Response>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const statusOf = (url: string) => new URL(url, 'http://x').searchParams.get('status');
const getUrls = (fetchMock: ReturnType<typeof vi.fn>) =>
  fetchMock.mock.calls
    .filter(([, init]) => !init || !(init as RequestInit).method)
    .map(([url]) => String(url));
const flush = () => act(async () => new Promise((r) => setTimeout(r, 0)));

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
  proposalsStore.set(INITIAL);
});

describe('status-scoped proposals fetch', () => {
  it('(a) the default load requests ?status=pending only', async () => {
    const fetchMock = vi.fn(async () => ok({ mappings: [row('p1', 'pending')] }));
    vi.stubGlobal('fetch', fetchMock);

    await loadProposedMappings();

    expect(getUrls(fetchMock)).toEqual(['/api/proposed-mappings?status=pending']);
    expect(proposalsStore.get().mappings.map((m) => m.mapping_id)).toEqual(['p1']);
  });

  it('(b) switching the filter requests that status and replaces mappings', async () => {
    const fetchMock = vi.fn(async (url: string) =>
      ok({ mappings: [row(`${statusOf(url)}-1`, String(statusOf(url)))] }),
    );
    vi.stubGlobal('fetch', fetchMock);
    await loadProposedMappings();

    setProposalStatusFilter('superseded');
    // Non-silent: the previous bucket is cleared, never shown under the new tab.
    expect(proposalsStore.get()).toMatchObject({ phase: 'loading', mappings: [] });
    await flush();

    expect(getUrls(fetchMock)).toEqual([
      '/api/proposed-mappings?status=pending',
      '/api/proposed-mappings?status=superseded',
    ]);
    expect(proposalsStore.get()).toMatchObject({ phase: 'ready', statusFilter: 'superseded' });
    expect(proposalsStore.get().mappings.map((m) => m.mapping_id)).toEqual(['superseded-1']);
  });

  it('re-selecting the current filter does not refetch', async () => {
    const fetchMock = vi.fn(async () => ok({ mappings: [] }));
    vi.stubGlobal('fetch', fetchMock);
    await loadProposedMappings();
    setProposalStatusFilter('pending');
    await flush();
    expect(getUrls(fetchMock)).toHaveLength(1);
  });

  it('(c) a slow response for the previous status arriving after the switch is discarded', async () => {
    const slowPending = deferred();
    const fetchMock = vi.fn((url: string) =>
      statusOf(url) === 'pending'
        ? slowPending.promise
        : Promise.resolve(ok({ mappings: [row('a1', 'accepted')] })),
    );
    vi.stubGlobal('fetch', fetchMock);

    const pendingLoad = loadProposedMappings();
    setProposalStatusFilter('accepted');
    await flush();
    expect(proposalsStore.get().mappings.map((m) => m.mapping_id)).toEqual(['a1']);

    slowPending.resolve(ok({ mappings: [row('p1', 'pending'), row('p2', 'pending')] }));
    await pendingLoad;

    expect(proposalsStore.get()).toMatchObject({ phase: 'ready', statusFilter: 'accepted' });
    expect(proposalsStore.get().mappings.map((m) => m.mapping_id)).toEqual(['a1']);
  });

  it('a stale failure for the previous status does not flip the current tab to error', async () => {
    const slowPending = deferred();
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) =>
        statusOf(url) === 'pending'
          ? slowPending.promise
          : Promise.resolve(ok({ mappings: [row('r1', 'rejected')] })),
      ),
    );
    vi.spyOn(console, 'error').mockImplementation(() => {});

    const pendingLoad = loadProposedMappings();
    setProposalStatusFilter('rejected');
    await flush();
    slowPending.resolve({ ok: false, status: 503, statusText: '', json: async () => ({}),
      text: async () => '{}' } as unknown as Response);
    await pendingLoad;

    expect(proposalsStore.get()).toMatchObject({ phase: 'ready', error: '' });
  });

  it('(d) the silent reload after accept refetches the CURRENT status only', async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'POST') return ok({ status: 'accepted' });
      return ok({ mappings: statusOf(url) === 'pending' ? [row('p2', 'pending')] : [] });
    });
    vi.stubGlobal('fetch', fetchMock);
    proposalsStore.set({
      ...proposalsStore.get(),
      phase: 'ready',
      mappings: [row('p1', 'pending'), row('p2', 'pending')],
    });
    const phases: string[] = [];
    const unsubscribe = proposalsStore.subscribe(() => phases.push(proposalsStore.get().phase));

    await acceptMapping('p1');
    await flush();
    unsubscribe();

    expect(getUrls(fetchMock)).toEqual(['/api/proposed-mappings?status=pending']);
    expect(phases).not.toContain('loading'); // silent — never blanks the list
    // The accepted row left the pending bucket: it simply disappears.
    expect(proposalsStore.get().mappings.map((m) => m.mapping_id)).toEqual(['p2']);
    expect(proposalsStore.get().optimistic).toEqual({});
  });

  it('the silent reload after reopen keeps a non-default current status', async () => {
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) =>
      init?.method === 'POST' ? ok({ status: 'pending' }) : ok({ mappings: [] }),
    );
    vi.stubGlobal('fetch', fetchMock);
    proposalsStore.set({
      ...proposalsStore.get(),
      phase: 'ready',
      statusFilter: 'rejected',
      mappings: [row('r1', 'rejected')],
    });

    await reopenMapping('r1');
    await flush();

    expect(getUrls(fetchMock)).toEqual(['/api/proposed-mappings?status=rejected']);
    expect(proposalsStore.get().mappings).toEqual([]);
  });

  it('(e) PendingBadge counts the fetched list for the selected status', async () => {
    const accepted = [row('a1', 'accepted'), row('a2', 'accepted'), row('a3', 'accepted')];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) =>
        ok({
          mappings: statusOf(url) === 'accepted' ? accepted : [row('p1', 'pending')],
          status_counts: { pending: 1, accepted: 3, rejected: 0, superseded: 0, expired: 9 },
        }),
      ),
    );
    act(() => root.render(<PendingBadge />));
    const badge = () => container.querySelector('#self-learning-count')!.textContent;

    await act(async () => {
      await loadProposedMappings();
    });
    expect(badge()).toBe('1 pending');

    act(() => setProposalStatusFilter('accepted'));
    expect(badge()).toBe('… accepted'); // no stale count while the bucket loads
    await flush();
    expect(badge()).toBe(`${accepted.length} accepted`);
  });

  it('an empty bucket beside other buckets keeps its tab empty-state and chip counts', async () => {
    const counts = { pending: 4, accepted: 0, rejected: 2, superseded: 0, expired: 0 };
    vi.stubGlobal('fetch', vi.fn(async () => ok({ mappings: [], status_counts: counts })));
    proposalsStore.set({ ...proposalsStore.get(), statusFilter: 'accepted' });
    act(() => root.render(<ProposalsList />));
    await act(async () => {
      await loadProposedMappings();
    });
    const chip = (s: string) => container.querySelector(`[data-status-filter="${s}"]`)!.textContent;

    expect(container.querySelector('.sl-empty')!.textContent).toBe('No accepted discoveries.');
    expect([chip('pending'), chip('accepted'), chip('rejected')]).toEqual([
      'Pending 4', 'Accepted 0', 'Rejected 2',
    ]);
  });

  it('"No discoveries yet" only when every bucket is empty', async () => {
    const counts = { pending: 0, accepted: 0, rejected: 0, superseded: 0, expired: 0 };
    vi.stubGlobal('fetch', vi.fn(async () => ok({ mappings: [], status_counts: counts })));
    act(() => root.render(<ProposalsList />));
    await act(async () => {
      await loadProposedMappings();
    });
    expect(container.querySelector('.sl-empty')!.textContent).toMatch(/^No discoveries yet/);
  });
});
