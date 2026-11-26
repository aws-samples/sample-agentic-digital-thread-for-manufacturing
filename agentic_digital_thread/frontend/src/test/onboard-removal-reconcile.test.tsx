// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* A failed removal recorded in this tab yields to newer catalog truth.
 *
 * When a removal job fails, this tab records it (removingStore: `error`, and
 * the removal's mode) and the card offers only "Remove again", fixed to that
 * mode. If the system is then recovered elsewhere (another tab finished the
 * removal or re-onboarded it, or Reset All put it back to Registered), the
 * catalog no longer says `removal_failed`, but the old record kept winning: the
 * card still offered only "Remove again", fixed to full, which could fully
 * remove a re-onboarded system and schedule its credentials for deletion.
 *
 * A catalog read that started after the failure was recorded now clears it
 * when the system is not `removal_failed` on the server. A read that started
 * before the failure (its answer predates it) never clears it. */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import type { CatalogSystem } from '../api/types/onboard';
import { CatalogList } from '../views/onboard/components/CatalogList';
import { ObModals } from '../views/onboard/components/ObModals';
import { removingStore } from '../views/onboard/flow';
import { flowStore } from '../views/onboard/flow-store';
import { ob } from '../views/onboard/state';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let queryClient: ReturnType<typeof createQueryClient>;

const SYSTEM: CatalogSystem = {
  id: 'quality-lab',
  name: 'Quality Lab',
  system_type: 'LIMS',
  plant: 'Plant A',
  connection_type: 'PostgreSQL',
  status: 'onboarded',
  connection_origin: 'user',
};

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function card(): HTMLElement {
  return document.getElementById('card-quality-lab') as HTMLElement;
}

function buttons(): string[] {
  return Array.from(card().querySelectorAll('button'))
    .map((b) => (b.textContent || '').trim())
    .filter((t) => t && !t.startsWith('connection'));
}

async function render(fetchImpl: typeof fetch) {
  vi.stubGlobal('fetch', fetchImpl);
  await act(async () => {
    root.render(
      <QueryClientProvider client={queryClient}>
        <CatalogList />
        <ObModals />
      </QueryClientProvider>,
    );
  });
  await vi.waitFor(() => expect(document.getElementById('card-quality-lab')).not.toBeNull());
}

function failedFull(): void {
  removingStore.set({
    ids: { 'quality-lab': 'error' },
    errors: { 'quality-lab': 'graph delete failed' },
    modes: { 'quality-lab': { full: true, registration: false } },
  });
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  queryClient = createQueryClient();
  queryClient.setDefaultOptions({ queries: { retry: false } });
  ob.adminChecked = true;
  ob.isAdmin = true;
  removingStore.set({ ids: {}, errors: {}, modes: {} });
});

afterEach(() => {
  act(() => root.unmount());
  queryClient.clear();
  container.remove();
  removingStore.set({ ids: {}, errors: {}, modes: {} });
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('a failed removal recorded in this tab', () => {
  it('is cleared by a newer catalog that says the system is registered', async () => {
    failedFull();
    await render(
      vi.fn(async () => response({ systems: [{ ...SYSTEM, status: 'registered' }] })) as
        unknown as typeof fetch,
    );
    await vi.waitFor(() => expect(buttons()).toContain('Remove registration'));
    expect(buttons()).not.toContain('Remove again');
    expect(removingStore.get().ids['quality-lab']).toBeUndefined();
    expect(removingStore.get().modes?.['quality-lab']).toBeUndefined();
  });

  it('is cleared by a newer catalog that says the system is onboarded', async () => {
    failedFull();
    const posts: string[] = [];
    await render(
      vi.fn(async (url: string, init?: RequestInit) => {
        if (typeof url === 'string' && url.includes('/reset/') && init?.method === 'POST') {
          posts.push(url);
          return response({ status: 'started', system_id: 'quality-lab' });
        }
        if (typeof url === 'string' && url.includes('/catalog')) {
          return response({ systems: [SYSTEM] });
        }
        return response({ state: 'running' });
      }) as unknown as typeof fetch,
    );
    await vi.waitFor(() => expect(buttons()).toContain('Remove system'));
    expect(buttons()).not.toContain('Remove again');
    act(() => {
      Array.from(card().querySelectorAll('button'))
        .find((b) => b.textContent?.trim() === 'Remove system')!
        .click();
    });
    // A fresh removal: the old full mode no longer applies.
    expect(flowStore.get().modal).toMatchObject({ kind: 'disconnect', systemId: 'quality-lab' });
    expect((flowStore.get().modal as { retryFull?: boolean }).retryFull).toBeUndefined();
    await vi.waitFor(() => expect(document.querySelector('#ob-disc-forget')).not.toBeNull());
    const forget = document.querySelector('#ob-disc-forget') as HTMLInputElement;
    expect(forget.checked).toBe(false);
    expect(forget.disabled).toBe(false);
  });

  it('is kept while the catalog still says removal_failed', async () => {
    failedFull();
    await render(
      vi.fn(async () => response({ systems: [{ ...SYSTEM, status: 'removal_failed' }] })) as
        unknown as typeof fetch,
    );
    await vi.waitFor(() => expect(buttons()).toEqual(['Remove again']));
    expect(removingStore.get().ids['quality-lab']).toBe('error');
    expect(removingStore.get().modes?.['quality-lab']?.full).toBe(true);
  });

  it(
    'is not cleared by a catalog read that started before the failure',
    async () => {
      // A removal started in this tab; while its job runs, a catalog read
      // starts and is answered only after the job reports the failure.
      let catalogReads = 0;
      let releaseLate!: (r: Response) => void;
      const late = new Promise<Response>((resolve) => {
        releaseLate = resolve;
      });
      let failed = false;
      await render(
        vi.fn(async (url: string, init?: RequestInit) => {
          if (typeof url === 'string' && url.includes('/reset/') && init?.method === 'POST') {
            return response({ status: 'started', system_id: 'quality-lab' });
          }
          if (typeof url === 'string' && url.endsWith('/status')) {
            failed = true;
            return response({ state: 'error', error: 'graph delete failed', full: true });
          }
          if (typeof url === 'string' && url.includes('/catalog')) {
            catalogReads += 1;
            if (catalogReads === 2) return late;
            return response({ systems: [SYSTEM] });
          }
          return response({});
        }) as unknown as typeof fetch,
      );
      const { obDisconnectConfirmed } = await import('../views/onboard/flow');
      expect(await obDisconnectConfirmed('quality-lab', true)).toBeNull();
      void queryClient.refetchQueries({ queryKey: queryKeys.catalog() });
      await vi.waitFor(() => expect(catalogReads).toBe(2));
      await vi.waitFor(() => expect(failed).toBe(true), { timeout: 6000 });
      await vi.waitFor(() => expect(removingStore.get().ids['quality-lab']).toBe('error'));
      // The early read now answers "onboarded", but it predates the failure.
      await act(async () => {
        releaseLate(response({ systems: [SYSTEM] }));
      });
      await vi.waitFor(() => expect(buttons()).toEqual(['Remove again']));
      expect(removingStore.get().ids['quality-lab']).toBe('error');
      expect(removingStore.get().modes?.['quality-lab']?.full).toBe(true);
    },
    10_000,
  );
});
